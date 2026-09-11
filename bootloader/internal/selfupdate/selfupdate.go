// Package selfupdate replaces the bootloader with a newer version of itself.
//
// A container cannot replace itself: removing it kills the process doing the
// removing, halfway through. So the running bootloader spawns a ONE-SHOT child
// from the new image, and that child does the work from outside -- the same
// shape orchestrator-agent uses in tools/upgrade_self.py, which is proven in
// production.
//
// The runtime container is never touched. A bootloader update must not
// interrupt a running PLC: losing the ability to manage a device is a bad
// afternoon, stopping its plant is a different category of problem. That is
// also why the failure mode is acceptable -- if the new bootloader will not
// start, Docker's restart policy keeps trying while the runtime carries on.
//
// The child reproduces the parent's configuration from the RUNNING container
// rather than from defaults. An operator may have installed with extra mounts
// or a non-standard port, and a self-update that quietly dropped them would
// leave a device subtly wrong in a way nobody would connect to "the bootloader
// updated itself".
package selfupdate

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/dockerapi"
)

// Environment the parent sets on the child. Their presence is what puts the
// child into swap mode instead of ordinary bootloader operation.
const (
	EnvMode      = "OPENPLC_BOOTLOADER_SELFUPDATE"
	EnvTarget    = "OPENPLC_SELFUPDATE_TARGET"
	EnvNewImage  = "OPENPLC_SELFUPDATE_IMAGE"
	EnvChildName = "OPENPLC_SELFUPDATE_CHILD"

	// ModeValue must match exactly. A single well-known value means an
	// accidental environment variable cannot put a bootloader into a mode
	// where its first act is to delete another container.
	ModeValue = "replace-parent"
)

// DefaultRepository is where bootloader images live.
const DefaultRepository = "ghcr.io/autonomy-logic/openplc-runtime-bootloader"

// settleDelay is how long the helper waits before removing the parent, so the
// parent can finish answering the request that started the update and the
// editor sees a reply rather than a dropped connection. A variable so tests do
// not spend it.
var settleDelay = 3 * time.Second

// DockerClient is the slice of the Docker API a self-update needs.
type DockerClient interface {
	InspectContainer(ctx context.Context, name string) (*dockerapi.ContainerInspect, error)
	CreateContainer(ctx context.Context, name string, spec any) (*dockerapi.CreateContainerResponse, error)
	StartContainer(ctx context.Context, name string) error
	StopContainer(ctx context.Context, name string, grace time.Duration) error
	RemoveContainer(ctx context.Context, name string, force bool) error
	RenameContainer(ctx context.Context, name, newName string) error
	InspectImage(ctx context.Context, ref string) (*dockerapi.ImageInfo, error)
	PullImage(ctx context.Context, ref string, onProgress func(dockerapi.PullProgress)) error
}

// IsChild reports whether this process was started to replace its parent.
func IsChild() bool {
	return os.Getenv(EnvMode) == ModeValue
}

// Start pulls the target bootloader image and launches the child that will
// perform the swap. It returns as soon as the child is running -- this process
// is about to be stopped by it.
func Start(ctx context.Context, docker DockerClient, repository, version string, log *slog.Logger) error {
	if version == "" {
		return errors.New("a bootloader version is required")
	}
	if repository == "" {
		repository = DefaultRepository
	}
	newImage := repository + ":" + version

	self, err := findSelf(ctx, docker)
	if err != nil {
		return err
	}
	log.Info("self-update starting", "container", self.Name, "to", newImage)

	// Pull before touching anything. If the image cannot be fetched, nothing
	// has changed and the bootloader carries on as it was.
	if _, err := docker.InspectImage(ctx, newImage); err != nil {
		if !dockerapi.IsNotFound(err) {
			return fmt.Errorf("checking for %s: %w", newImage, err)
		}
		log.Info("pulling the new bootloader image", "image", newImage)
		if err := docker.PullImage(ctx, newImage, nil); err != nil {
			return fmt.Errorf("downloading %s: %w", newImage, err)
		}
	}

	childName := strings.TrimPrefix(self.Name, "/") + "-selfupdate"
	// A leftover child from an interrupted attempt would block this one on a
	// name conflict, and it has nothing worth keeping.
	if err := docker.RemoveContainer(ctx, childName, true); err != nil {
		return fmt.Errorf("clearing a previous self-update helper: %w", err)
	}

	// The child needs the docker socket and nothing else. Deliberately NOT the
	// parent's mounts: it does not read the spec, serve an API or touch runtime
	// data -- it stops one container and creates another.
	child := map[string]any{
		"Image": newImage,
		"Env": []string{
			EnvMode + "=" + ModeValue,
			EnvTarget + "=" + strings.TrimPrefix(self.Name, "/"),
			EnvNewImage + "=" + newImage,
			EnvChildName + "=" + childName,
		},
		"HostConfig": map[string]any{
			"Binds": []string{"/var/run/docker.sock:/var/run/docker.sock"},
			// Never restart: this is a one-shot. A restart policy on a
			// container whose job is to delete its parent would re-run the
			// swap on every daemon start, forever.
			"RestartPolicy": map[string]any{"Name": "no"},
			// Removed by the next self-update rather than automatically, so
			// its logs survive long enough to explain a failed swap.
			"AutoRemove": false,
		},
	}

	if _, err := docker.CreateContainer(ctx, childName, child); err != nil {
		return fmt.Errorf("creating the self-update helper: %w", err)
	}
	if err := docker.StartContainer(ctx, childName); err != nil {
		return fmt.Errorf("starting the self-update helper: %w", err)
	}

	log.Info("self-update helper started; this bootloader will be replaced shortly",
		"helper", childName)
	return nil
}

// Execute is the child's side: replace the parent and exit.
//
// Idempotent by design. A parent that is already gone -- because a previous
// attempt got that far before dying -- is not an error; the goal is that a
// bootloader on the new image is running when this finishes.
func Execute(ctx context.Context, docker DockerClient, log *slog.Logger) error {
	target := os.Getenv(EnvTarget)
	newImage := os.Getenv(EnvNewImage)
	if target == "" || newImage == "" {
		return fmt.Errorf("self-update helper started without %s and %s", EnvTarget, EnvNewImage)
	}
	log.Info("replacing the bootloader", "container", target, "image", newImage)

	// Capture the parent's configuration BEFORE removing it. Everything after
	// this point depends on having it, and once the container is gone it
	// cannot be recovered.
	parent, err := docker.InspectContainer(ctx, target)
	if err != nil && !dockerapi.IsNotFound(err) {
		return fmt.Errorf("inspecting %s: %w", target, err)
	}

	var spec map[string]any
	if parent != nil {
		spec = replacementSpec(parent, newImage)
	} else {
		// Nothing to copy from. Refusing here would leave a device with no
		// bootloader at all, which is worse than a conventional one.
		log.Warn("the bootloader container is already gone; recreating from defaults",
			"container", target)
		spec = defaultSpec(newImage)
	}

	// Give the parent a moment to finish answering the request that started
	// this, so the editor sees a reply rather than a dropped connection.
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(settleDelay):
	}

	// Create the replacement FIRST, under a temporary name.
	//
	// Removing the parent first meant a rejected create -- an invalid
	// HostConfig on an older daemon, a full disk, an image pruned between the
	// pull and the create -- left the device with no bootloader at all, on
	// hardware this feature exists because it has no SSH. The helper runs with
	// RestartPolicy: no, so nothing would have come back for it.
	staging := target + "-next"
	// A leftover from an interrupted attempt would take the name.
	if err := docker.RemoveContainer(ctx, staging, true); err != nil {
		return fmt.Errorf("clearing a previous staged bootloader: %w", err)
	}
	if _, err := docker.CreateContainer(ctx, staging, spec); err != nil {
		return fmt.Errorf("creating the new bootloader: %w", err)
	}

	if parent != nil {
		// Force: the parent's restart policy would otherwise bring it back
		// between the stop and the remove, and the name would still be taken.
		if err := docker.RemoveContainer(ctx, target, true); err != nil {
			// The staged container is removed so a retry starts clean; the
			// parent is still there and still running.
			_ = docker.RemoveContainer(ctx, staging, true)
			return fmt.Errorf("removing the old bootloader: %w", err)
		}
		log.Info("old bootloader removed", "container", target)
	}

	// Take over the real name, then start. A rename is metadata only, so the
	// window where neither container holds the name is as small as it can be.
	if err := docker.RenameContainer(ctx, staging, target); err != nil {
		return fmt.Errorf("renaming the new bootloader to %s: %w", target, err)
	}
	if err := docker.StartContainer(ctx, target); err != nil {
		return fmt.Errorf("starting the new bootloader: %w", err)
	}

	log.Info("new bootloader started", "container", target, "image", newImage)
	return nil
}

// replacementSpec rebuilds the parent's create payload with the new image.
//
// The parent's own environment is carried over except the self-update
// variables: leaving those in would put the NEW bootloader straight back into
// child mode on start-up, and it would immediately try to replace itself in a
// loop.
func replacementSpec(parent *dockerapi.ContainerInspect, newImage string) map[string]any {
	env := make([]string, 0, len(parent.Config.Env))
	for _, entry := range parent.Config.Env {
		if strings.HasPrefix(entry, EnvMode+"=") ||
			strings.HasPrefix(entry, EnvTarget+"=") ||
			strings.HasPrefix(entry, EnvNewImage+"=") ||
			strings.HasPrefix(entry, EnvChildName+"=") {
			continue
		}
		// PATH and similar come from the image, and copying an old image's
		// values onto a new one is how a replacement ends up running with
		// stale defaults.
		if strings.HasPrefix(entry, "PATH=") {
			continue
		}
		env = append(env, entry)
	}

	restart := parent.HostConfig.RestartPolicy.Name
	if restart == "" || restart == "no" {
		// A bootloader that does not come back at boot is not a bootloader.
		// If the parent somehow had no policy, the replacement gets the one
		// install.sh would have given it.
		restart = "always"
	}

	spec := map[string]any{
		"Image": newImage,
		"Env":   env,
		"HostConfig": map[string]any{
			"Binds":         parent.HostConfig.Binds,
			"NetworkMode":   parent.HostConfig.NetworkMode,
			"Privileged":    parent.HostConfig.Privileged,
			"RestartPolicy": map[string]any{"Name": restart},
		},
	}
	// Command-line flags -- the state directory and port an operator chose at
	// install time. Dropping them would silently move the API to 8445 and the
	// spec to its default path.
	if len(parent.Config.Cmd) > 0 {
		spec["Cmd"] = parent.Config.Cmd
	}
	return spec
}

// defaultSpec is the last-resort configuration when the parent has vanished.
func defaultSpec(newImage string) map[string]any {
	return map[string]any{
		"Image": newImage,
		"HostConfig": map[string]any{
			"Binds": []string{
				"/var/run/docker.sock:/var/run/docker.sock",
				"/var/lib/openplc-bootloader:/var/lib/openplc-bootloader",
				"/var/lib/openplc-runtime:/var/lib/openplc-runtime:ro",
			},
			"NetworkMode":   "host",
			"RestartPolicy": map[string]any{"Name": "always"},
		},
	}
}

// findSelf identifies the container this process is running in.
//
// HOSTNAME is the container's short id under Docker's defaults, which is the
// most direct answer. It can be overridden (--hostname), so a miss falls back
// to the name install.sh uses -- and a miss on both is reported rather than
// guessed at, because every caller of this is about to delete whatever it
// names.
func findSelf(ctx context.Context, docker DockerClient) (*dockerapi.ContainerInspect, error) {
	if hostname := os.Getenv("HOSTNAME"); hostname != "" {
		if found, err := docker.InspectContainer(ctx, hostname); err == nil {
			return found, nil
		}
	}
	const conventional = "openplc-bootloader"
	found, err := docker.InspectContainer(ctx, conventional)
	if err != nil {
		return nil, fmt.Errorf(
			"could not identify this bootloader's own container (tried $HOSTNAME and %q): %w",
			conventional, err)
	}
	return found, nil
}
