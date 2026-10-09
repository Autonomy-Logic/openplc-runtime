// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Package updater changes which runtime version a device runs.
// Flow: pull new -> stop old -> start new -> health-gate -> remove old.
// Pull before stop so a mid-pull failure or an image that will not
// start leaves the old image on disk. Upgrade and downgrade are the
// same operation; no version floor. No automatic rollback — a failure
// stops in recovery mode and waits for an operator.
package updater

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/dockerapi"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/runtimespec"
)

// State is where an update has got to.
type State string

const (
	StateIdle      State = "idle"
	StatePulling   State = "pulling"
	StateSwapping  State = "swapping"
	StateVerifying State = "verifying"
	StateSuccess   State = "success"
	StateFailed    State = "failed"
)

// Progress is the update's externally visible state, polled by the editor.
type Progress struct {
	State State `json:"state"`
	// From and To are image tags, so the editor can label the operation
	// without having to remember what it asked for.
	From string `json:"from,omitempty"`
	To   string `json:"to,omitempty"`
	// Phase is the daemon's own wording during a pull.
	Phase   string `json:"phase,omitempty"`
	Percent *int   `json:"percent,omitempty"`
	// Error is written for a person: what failed and, where there is one,
	// what to do about it.
	Error string `json:"error,omitempty"`
	// Warning is a concern that did not stop the update -- currently a tight
	// disk measurement. Shown alongside progress rather than instead of it.
	Warning    string     `json:"warning,omitempty"`
	StartedAt  time.Time  `json:"startedAt,omitempty"`
	FinishedAt *time.Time `json:"finishedAt,omitempty"`
}

// ErrInProgress is returned when an update is already running.
var ErrInProgress = errors.New("an update is already in progress")

// DockerClient is the slice of the Docker API an update needs.
type DockerClient interface {
	PullImage(ctx context.Context, ref string, onProgress func(dockerapi.PullProgress)) error
	InspectImage(ctx context.Context, ref string) (*dockerapi.ImageInfo, error)
	RemoveImage(ctx context.Context, ref string, force bool) error
}

// Supervisor is what an update needs of the runtime container's owner.
type Supervisor interface {
	// BeginUpdate claims the supervisor and suppresses crash accounting for
	// the stop that is about to happen.
	BeginUpdate() error
	EndUpdate()
	// AbortUpdate releases the claim and restores the state BeginUpdate
	// displaced, for an attempt that changed nothing on the device.
	AbortUpdate()
	Stop(ctx context.Context) error
	Reconcile(ctx context.Context) error
	EnterRecovery(ctx context.Context, reason string)
}

// Config wires an Updater.
type Config struct {
	Docker     DockerClient
	Supervisor Supervisor
	Spec       *runtimespec.Config
	SpecPath   string
	// StateDir is measured for the disk pre-check.
	StateDir string
	Log      *slog.Logger
}

// Updater performs one version change at a time.
type Updater struct {
	cfg Config

	mu       sync.Mutex
	progress Progress
	running  bool
}

// New builds an Updater.
func New(cfg Config) *Updater {
	return &Updater{cfg: cfg, progress: Progress{State: StateIdle}}
}

// Progress returns a snapshot for the editor to poll.
func (u *Updater) Progress() Progress {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.progress
}

// Start begins a version change and returns immediately.
//
// Asynchronous because a pull routinely runs for minutes on a plant link --
// far longer than any sensible HTTP timeout. The caller polls Progress.
func (u *Updater) Start(ctx context.Context, targetVersion string) error {
	if err := validateVersion(targetVersion); err != nil {
		return err
	}

	u.mu.Lock()
	if u.running {
		u.mu.Unlock()
		// Two concurrent swaps of one container is not a state worth trying
		// to make safe, so it is refused with a clear message rather than
		// queued.
		return ErrInProgress
	}
	// Claim the supervisor before the goroutine starts, so a second caller
	// cannot slip between the check and the claim.
	if err := u.cfg.Supervisor.BeginUpdate(); err != nil {
		u.mu.Unlock()
		return err
	}
	u.running = true
	u.progress = Progress{
		State:     StatePulling,
		From:      u.cfg.Spec.DesiredVersion(),
		To:        targetVersion,
		StartedAt: time.Now(),
	}
	u.mu.Unlock()

	// Detached from the request context on purpose: an editor that closes its
	// connection mid-update must not abort a swap that is already underway
	// and leave the device between versions.
	go u.run(context.WithoutCancel(ctx), targetVersion)
	return nil
}

func (u *Updater) run(ctx context.Context, targetVersion string) {
	previousVersion := u.cfg.Spec.DesiredVersion()
	defer u.cfg.Supervisor.EndUpdate()

	err := u.execute(ctx, previousVersion, targetVersion)

	u.mu.Lock()
	u.running = false
	finished := time.Now()
	u.progress.FinishedAt = &finished
	if err != nil {
		u.progress.State = StateFailed
		u.progress.Error = err.Error()
	} else {
		u.progress.State = StateSuccess
		u.progress.Percent = nil
		u.progress.Phase = ""
	}
	u.mu.Unlock()

	if err != nil {
		u.cfg.Log.Error("update failed", "from", previousVersion, "to", targetVersion, "error", err)

		// Failures that never touched the container leave the running
		// runtime intact; recovery would be an unforced plant outage.
		var beforeSwap errBeforeSwap
		if errors.As(err, &beforeSwap) {
			u.cfg.Log.Info("nothing was changed; leaving the runtime alone",
				"version", previousVersion)
			u.cfg.Supervisor.AbortUpdate()
			return
		}

		// Recovery, not rollback: the operator decides what to install next.
		u.cfg.Supervisor.EnterRecovery(ctx, fmt.Sprintf(
			"update from %s to %s failed: %v", previousVersion, targetVersion, err))
		return
	}
	u.cfg.Log.Info("update complete", "from", previousVersion, "to", targetVersion)
}

// errBeforeSwap marks a failure that happened while the runtime was still
// untouched, so the caller knows not to enter recovery.
type errBeforeSwap struct{ err error }

func (e errBeforeSwap) Error() string { return e.err.Error() }
func (e errBeforeSwap) Unwrap() error { return e.err }

func (u *Updater) execute(ctx context.Context, previousVersion, targetVersion string) error {
	if previousVersion == targetVersion {
		// Not an error: re-installing the running version is a legitimate way
		// to recover a damaged image, and refusing would remove the only
		// repair an operator can perform from the editor.
		u.cfg.Log.Info("reinstalling the current version", "version", targetVersion)
	}

	targetRef := u.cfg.Spec.ImageRefFor(targetVersion)
	previousRef := u.cfg.Spec.ImageRefFor(previousVersion)

	u.checkDiskSpace(ctx, targetRef)

	// 1. Pull. Non-destructive: the running version stays on disk.
	u.setPhase(StatePulling, "", nil)
	err := u.cfg.Docker.PullImage(ctx, targetRef, func(p dockerapi.PullProgress) {
		u.setPhase(StatePulling, p.Phase, p.Percent)
	})
	if err != nil {
		// A pull failure is excused only when the image is already present
		// locally (air-gapped, side-loaded, offline registry).
		if _, inspectErr := u.cfg.Docker.InspectImage(ctx, targetRef); inspectErr != nil {
			// The full chain goes to the log; the operator gets one sentence.
			u.cfg.Log.Error("pull failed", "image", targetRef, "error", err)
			return errBeforeSwap{errors.New(describePullFailure(
				u.cfg.Spec.Repository, targetRef, err))}
		}
		u.cfg.Log.Warn("pull failed but the image is already present; continuing",
			"image", targetRef, "error", err)
	}

	// 2. Swap. The spec is written BEFORE the container is recreated, so a
	// power cut mid-swap leaves the device booting the version it was moving
	// to rather than silently reverting -- and the image for it is already on
	// disk by now.
	u.setPhase(StateSwapping, "", nil)
	u.cfg.Spec.SetDesiredVersion(targetVersion)
	if err := u.cfg.Spec.Save(u.cfg.SpecPath); err != nil {
		u.cfg.Spec.SetDesiredVersion(previousVersion)
		return errBeforeSwap{fmt.Errorf("could not record the new version: %w", err)}
	}

	if err := u.cfg.Supervisor.Stop(ctx); err != nil {
		u.cfg.Log.Warn("stopping the old runtime", "error", err)
	}

	// 3. Start and health-gate. Reconcile removes the old container, creates
	// one from the new image and waits for it to report healthy.
	u.setPhase(StateVerifying, "", nil)
	if err := u.cfg.Supervisor.Reconcile(ctx); err != nil {
		// Leave the spec pointing at the target: the operator is about to be
		// shown recovery mode, and reverting the file behind their back would
		// make the next boot disagree with what the editor just told them.
		return fmt.Errorf("%s did not start: %w", targetRef, err)
	}

	// 4. Only now retire the old image. Doing this last is what makes the
	// whole sequence recoverable.
	if previousVersion != targetVersion {
		if err := u.cfg.Docker.RemoveImage(ctx, previousRef, false); err != nil {
			// Not a failure of the update: the new version is running. Disk
			// was not reclaimed, which is worth a log and not a rollback.
			u.cfg.Log.Warn("could not remove the previous image",
				"image", previousRef, "error", err)
		} else {
			u.cfg.Log.Info("removed the previous image", "image", previousRef)
		}
	}
	return nil
}

// checkDiskSpace is advisory: a tight measurement becomes a progress
// warning, and the pull proceeds. The estimate measures the bootloader's
// own disk, which only matches Docker's data-root on a default install.
func (u *Updater) checkDiskSpace(ctx context.Context, targetRef string) {
	free, err := freeBytes(u.cfg.StateDir)
	if err != nil {
		u.cfg.Log.Warn("could not measure free space", "error", err)
		return
	}
	if free == 0 {
		return // measurement unavailable on this platform
	}

	// Estimate the target's size from the image already installed: successive
	// runtime versions are within a few percent of each other, and there is
	// no way to ask a registry for a decompressed size before pulling.
	var estimate int64
	if info, err := u.cfg.Docker.InspectImage(ctx, u.cfg.Spec.ImageRef()); err == nil {
		estimate = info.Size
	}
	if estimate == 0 {
		u.cfg.Log.Info("no local image to estimate from; skipping the disk pre-check",
			"free", free)
		return
	}

	if free < estimate {
		warning := fmt.Sprintf(
			"free space looks tight for %s: about %s needed, %s available on %s. "+
				"Continuing; the download will report a clear error if it does not fit.",
			targetRef, humanBytes(estimate), humanBytes(free), u.cfg.StateDir)
		u.cfg.Log.Warn("disk pre-check is tight", "detail", warning)
		u.mu.Lock()
		u.progress.Warning = warning
		u.mu.Unlock()
		return
	}
	u.cfg.Log.Info("disk pre-check passed",
		"free", humanBytes(free), "estimate", humanBytes(estimate))
}

func (u *Updater) setPhase(state State, phase string, percent *int) {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.progress.State = state
	u.progress.Phase = phase
	u.progress.Percent = percent
}

// validateVersion rejects a tag the daemon would refuse. The reference
// is built as repository + ":" + version, so a slash or colon in the
// version could redirect the pull to a different repository.
func validateVersion(version string) error {
	if version == "" {
		return errors.New("a version is required")
	}
	if len(version) > 128 {
		return errors.New("version is too long to be an image tag")
	}
	if strings.ContainsAny(version, " \t\n/:@") {
		return fmt.Errorf(
			"%q is not a valid version tag: it must not contain spaces, slashes, "+
				"colons or '@'", version)
	}
	// Docker's own tag grammar: [A-Za-z0-9_][A-Za-z0-9._-]*
	for i, r := range version {
		valid := (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') ||
			(r >= '0' && r <= '9') || r == '_' || r == '.' || r == '-'
		if !valid {
			return fmt.Errorf("%q is not a valid version tag", version)
		}
		if i == 0 && (r == '.' || r == '-') {
			return fmt.Errorf("%q is not a valid version tag: it may not start with %q",
				version, string(r))
		}
	}
	return nil
}

// humanBytes renders a size the way an error message should read.
func humanBytes(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	value := float64(n)
	units := []string{"KiB", "MiB", "GiB", "TiB"}
	for _, suffix := range units {
		value /= unit
		if value < unit {
			return fmt.Sprintf("%.1f %s", value, suffix)
		}
	}
	return fmt.Sprintf("%.1f PiB", value/unit)
}

// describePullFailure turns a failed pull into an operator-actionable
// sentence. The special case is an unqualified repository (no registry
// host), which Docker silently routes to Docker Hub.
func describePullFailure(repository, ref string, err error) string {
	reason := dockerapi.Reason(err)
	if isUnqualifiedRepository(repository) {
		return fmt.Sprintf(
			"could not download %s: %s. This device is configured to use the image "+
				"repository %q, which has no registry host, so the download went to "+
				"Docker Hub instead of the OpenPLC registry.",
			ref, reason, repository)
	}
	return fmt.Sprintf("could not download %s: %s", ref, reason)
}

// isUnqualifiedRepository reports whether Docker would resolve repository
// against Docker Hub. The daemon's rule: the part before the first slash is a
// registry only when it contains a dot or a colon, or is exactly "localhost".
func isUnqualifiedRepository(repository string) bool {
	slash := strings.Index(repository, "/")
	if slash < 0 {
		return true
	}
	host := repository[:slash]
	return host != "localhost" && !strings.ContainsAny(host, ".:")
}
