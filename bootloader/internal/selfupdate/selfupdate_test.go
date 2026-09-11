package selfupdate

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/dockerapi"
)

type fakeDocker struct {
	mu sync.Mutex

	containers   map[string]*dockerapi.ContainerInspect
	imagePresent bool
	pullErr      error

	created   map[string]any
	started   []string
	removed   []string
	pulls     []string
	startErr  error
	renamed   []string
	renameErr error
	createErr error
}

func newFake() *fakeDocker {
	return &fakeDocker{
		containers: map[string]*dockerapi.ContainerInspect{},
		created:    map[string]any{},
	}
}

func (f *fakeDocker) InspectContainer(_ context.Context, name string) (*dockerapi.ContainerInspect, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if found, ok := f.containers[name]; ok {
		return found, nil
	}
	return nil, &dockerapi.APIError{Status: http.StatusNotFound, Path: "/containers/" + name + "/json"}
}

func (f *fakeDocker) CreateContainer(_ context.Context, name string, spec any) (*dockerapi.CreateContainerResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.createErr != nil {
		return nil, f.createErr
	}
	f.created[name] = spec
	return &dockerapi.CreateContainerResponse{ID: "created-" + name}, nil
}

func (f *fakeDocker) StartContainer(_ context.Context, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.started = append(f.started, name)
	return f.startErr
}

// RenameContainer models the metadata move: the spec follows the new name, so
// assertions about what was created under `target` still hold.
func (f *fakeDocker) RenameContainer(_ context.Context, name, newName string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.renameErr != nil {
		return f.renameErr
	}
	f.renamed = append(f.renamed, name+"->"+newName)
	if spec, ok := f.created[name]; ok {
		f.created[newName] = spec
		delete(f.created, name)
	}
	return nil
}

func (f *fakeDocker) StopContainer(context.Context, string, time.Duration) error { return nil }

func (f *fakeDocker) RemoveContainer(_ context.Context, name string, _ bool) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removed = append(f.removed, name)
	delete(f.containers, name)
	return nil
}

func (f *fakeDocker) InspectImage(_ context.Context, ref string) (*dockerapi.ImageInfo, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.imagePresent {
		return nil, &dockerapi.APIError{Status: http.StatusNotFound, Path: "/images/" + ref + "/json"}
	}
	return &dockerapi.ImageInfo{ID: "sha256:x"}, nil
}

func (f *fakeDocker) PullImage(_ context.Context, ref string, _ func(dockerapi.PullProgress)) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.pulls = append(f.pulls, ref)
	if f.pullErr != nil {
		return f.pullErr
	}
	f.imagePresent = true
	return nil
}

func quietLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// The helper's settle delay exists so a real parent can finish answering an
// HTTP request. There is no request here, so tests skip the wait.
func init() { settleDelay = time.Millisecond }

// parentContainer is a bootloader installed the way install.sh installs one,
// plus an operator's non-default port and an extra mount.
func parentContainer() *dockerapi.ContainerInspect {
	inspect := &dockerapi.ContainerInspect{ID: "parentid", Name: "/openplc-bootloader"}
	inspect.Config.Image = "ghcr.io/autonomy-logic/openplc-runtime-bootloader:bootloader-v1.0.0"
	inspect.Config.Env = []string{"PATH=/usr/bin", "TZ=America/New_York"}
	inspect.Config.Cmd = []string{"-state-dir", "/opt/openplc-bootloader", "-port", "9445"}
	inspect.HostConfig = dockerapi.ContainerHostConfig{
		Binds: []string{
			"/var/run/docker.sock:/var/run/docker.sock",
			"/opt/openplc-bootloader:/opt/openplc-bootloader",
			"/var/lib/openplc-runtime:/var/lib/openplc-runtime:ro",
		},
		NetworkMode:   "host",
		RestartPolicy: dockerapi.RestartPolicy{Name: "always"},
	}
	return inspect
}

func hostConfig(t *testing.T, spec any) map[string]any {
	t.Helper()
	asMap, ok := spec.(map[string]any)
	if !ok {
		t.Fatalf("spec is not a map: %T", spec)
	}
	host, ok := asMap["HostConfig"].(map[string]any)
	if !ok {
		t.Fatalf("spec has no HostConfig: %v", asMap)
	}
	return host
}

// --- parent side ---------------------------------------------------------

func TestStartPullsAndLaunchesAHelper(t *testing.T) {
	docker := newFake()
	docker.containers["openplc-bootloader"] = parentContainer()
	t.Setenv("HOSTNAME", "openplc-bootloader")

	if err := Start(context.Background(), docker, DefaultRepository, "bootloader-v1.1.0", quietLogger()); err != nil {
		t.Fatalf("start: %v", err)
	}

	if len(docker.pulls) != 1 || !strings.HasSuffix(docker.pulls[0], ":bootloader-v1.1.0") {
		t.Fatalf("want the target image pulled, got %v", docker.pulls)
	}
	spec, ok := docker.created["openplc-bootloader-selfupdate"]
	if !ok {
		t.Fatalf("want a helper container created, got %v", docker.created)
	}

	// The helper must not restart: a container whose job is to delete its
	// parent would re-run the swap on every daemon start, forever.
	host := hostConfig(t, spec)
	if host["RestartPolicy"].(map[string]any)["Name"] != "no" {
		t.Errorf("the helper must never restart, got %v", host["RestartPolicy"])
	}
	// It needs the socket and nothing else -- it does not read the spec,
	// serve an API, or touch runtime data.
	binds := host["Binds"].([]string)
	if len(binds) != 1 || !strings.Contains(binds[0], "docker.sock") {
		t.Errorf("the helper should mount only the docker socket, got %v", binds)
	}
}

func TestStartDoesNothingWhenTheImageCannotBeFetched(t *testing.T) {
	// Nothing has changed yet at this point, so a failed pull must leave the
	// running bootloader entirely alone.
	docker := newFake()
	docker.containers["openplc-bootloader"] = parentContainer()
	docker.pullErr = errors.New("manifest unknown")
	t.Setenv("HOSTNAME", "openplc-bootloader")

	err := Start(context.Background(), docker, DefaultRepository, "bootloader-v9.9.9", quietLogger())
	if err == nil {
		t.Fatal("a failed pull must surface an error")
	}
	if len(docker.created) != 0 {
		t.Fatalf("no helper may be created after a failed pull, got %v", docker.created)
	}
	if len(docker.removed) != 0 {
		t.Fatalf("nothing may be removed after a failed pull, got %v", docker.removed)
	}
}

func TestStartRefusesWithoutAVersion(t *testing.T) {
	docker := newFake()
	if err := Start(context.Background(), docker, DefaultRepository, "", quietLogger()); err == nil {
		t.Fatal("a self-update needs a version")
	}
}

func TestStartFailsClearlyWhenItCannotIdentifyItself(t *testing.T) {
	// Every caller of this is about to delete whatever it names, so a guess
	// would be the wrong kind of helpful.
	docker := newFake()
	t.Setenv("HOSTNAME", "not-a-container")

	err := Start(context.Background(), docker, DefaultRepository, "bootloader-v1.1.0", quietLogger())
	if err == nil {
		t.Fatal("want an error when the container cannot be identified")
	}
	if !strings.Contains(err.Error(), "own container") {
		t.Fatalf("the error should say what it could not find, got %v", err)
	}
}

func TestStartFallsBackToTheConventionalName(t *testing.T) {
	// --hostname overrides HOSTNAME, so the id lookup can miss on a perfectly
	// ordinary install.
	docker := newFake()
	docker.containers["openplc-bootloader"] = parentContainer()
	t.Setenv("HOSTNAME", "some-custom-hostname")

	if err := Start(context.Background(), docker, DefaultRepository, "bootloader-v1.1.0", quietLogger()); err != nil {
		t.Fatalf("start: %v", err)
	}
	if _, ok := docker.created["openplc-bootloader-selfupdate"]; !ok {
		t.Fatalf("want the helper created via the fallback name, got %v", docker.created)
	}
}

// --- child side ----------------------------------------------------------

func setChildEnv(t *testing.T, target, image string) {
	t.Helper()
	t.Setenv(EnvMode, ModeValue)
	t.Setenv(EnvTarget, target)
	t.Setenv(EnvNewImage, image)
}

func TestIsChildOnlyOnTheExactMarker(t *testing.T) {
	// One well-known value, so a stray environment variable cannot put a
	// bootloader into a mode whose first act is to delete another container.
	t.Setenv(EnvMode, "true")
	if IsChild() {
		t.Fatal("only the exact marker may select child mode")
	}
	t.Setenv(EnvMode, ModeValue)
	if !IsChild() {
		t.Fatal("the exact marker must select child mode")
	}
}

func TestTheChildReplacesTheParentPreservingItsConfiguration(t *testing.T) {
	docker := newFake()
	docker.containers["openplc-bootloader"] = parentContainer()
	setChildEnv(t, "openplc-bootloader", "ghcr.io/x/bootloader:bootloader-v1.1.0")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := Execute(ctx, docker, quietLogger()); err != nil {
		t.Fatalf("execute: %v", err)
	}

	// The staged name is cleared first (a leftover from an interrupted
	// attempt would hold it), then the parent goes.
	var removedParent bool
	for _, name := range docker.removed {
		if name == "openplc-bootloader" {
			removedParent = true
		}
	}
	if !removedParent {
		t.Fatalf("want the parent removed, got %v", docker.removed)
	}
	spec, ok := docker.created["openplc-bootloader"]
	if !ok {
		t.Fatalf("want the bootloader recreated under its own name, got %v", docker.created)
	}
	asMap := spec.(map[string]any)

	if asMap["Image"] != "ghcr.io/x/bootloader:bootloader-v1.1.0" {
		t.Errorf("want the new image, got %v", asMap["Image"])
	}
	// An operator's chosen state directory and port live in Cmd. Dropping
	// them would silently move the API and the spec back to defaults.
	cmd := asMap["Cmd"].([]string)
	if strings.Join(cmd, " ") != "-state-dir /opt/openplc-bootloader -port 9445" {
		t.Errorf("the parent's flags must be preserved, got %v", cmd)
	}
	host := hostConfig(t, spec)
	if len(host["Binds"].([]string)) != 3 {
		t.Errorf("the parent's mounts must be preserved, got %v", host["Binds"])
	}
	if host["NetworkMode"] != "host" {
		t.Errorf("want host networking preserved, got %v", host["NetworkMode"])
	}
	if host["RestartPolicy"].(map[string]any)["Name"] != "always" {
		t.Errorf("the replacement must come back at boot, got %v", host["RestartPolicy"])
	}
}

func TestTheReplacementDoesNotInheritSelfUpdateEnvironment(t *testing.T) {
	// Otherwise the new bootloader starts in child mode and immediately tries
	// to replace itself, forever.
	docker := newFake()
	parent := parentContainer()
	parent.Config.Env = append(parent.Config.Env,
		EnvMode+"="+ModeValue,
		EnvTarget+"=openplc-bootloader",
		EnvNewImage+"=old",
	)
	docker.containers["openplc-bootloader"] = parent
	setChildEnv(t, "openplc-bootloader", "ghcr.io/x/bootloader:bootloader-v1.1.0")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := Execute(ctx, docker, quietLogger()); err != nil {
		t.Fatalf("execute: %v", err)
	}

	env := docker.created["openplc-bootloader"].(map[string]any)["Env"].([]string)
	for _, entry := range env {
		if strings.HasPrefix(entry, EnvMode) || strings.HasPrefix(entry, EnvTarget) ||
			strings.HasPrefix(entry, EnvNewImage) {
			t.Fatalf("self-update environment leaked into the replacement: %v", env)
		}
	}
	// The operator's own environment must survive.
	var sawTZ bool
	for _, entry := range env {
		if entry == "TZ=America/New_York" {
			sawTZ = true
		}
	}
	if !sawTZ {
		t.Errorf("the parent's own environment must be preserved, got %v", env)
	}
	// PATH comes from the image; carrying the old one forward is how a
	// replacement ends up running with stale defaults.
	for _, entry := range env {
		if strings.HasPrefix(entry, "PATH=") {
			t.Errorf("PATH must come from the new image, not the old container: %v", env)
		}
	}
}

func TestTheChildRecreatesEvenIfTheParentIsAlreadyGone(t *testing.T) {
	// A previous attempt may have got as far as removing the parent before
	// dying. Refusing here would leave a device with no bootloader at all,
	// which is worse than a conventional one.
	docker := newFake()
	setChildEnv(t, "openplc-bootloader", "ghcr.io/x/bootloader:bootloader-v1.1.0")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := Execute(ctx, docker, quietLogger()); err != nil {
		t.Fatalf("a missing parent must not be fatal: %v", err)
	}

	spec, ok := docker.created["openplc-bootloader"]
	if !ok {
		t.Fatal("a bootloader must exist when this finishes")
	}
	host := hostConfig(t, spec)
	if host["RestartPolicy"].(map[string]any)["Name"] != "always" {
		t.Error("the fallback must still come back at boot")
	}
}

func TestTheChildRefusesWithoutItsInstructions(t *testing.T) {
	docker := newFake()
	t.Setenv(EnvMode, ModeValue)
	t.Setenv(EnvTarget, "")
	t.Setenv(EnvNewImage, "")

	if err := Execute(context.Background(), docker, quietLogger()); err == nil {
		t.Fatal("the helper must refuse to act without a target and an image")
	}
}

func TestAParentWithNoRestartPolicyIsGivenOne(t *testing.T) {
	// A bootloader that does not come back at boot is not a bootloader.
	docker := newFake()
	parent := parentContainer()
	parent.HostConfig.RestartPolicy = dockerapi.RestartPolicy{Name: "no"}
	docker.containers["openplc-bootloader"] = parent
	setChildEnv(t, "openplc-bootloader", "ghcr.io/x/bootloader:bootloader-v1.1.0")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := Execute(ctx, docker, quietLogger()); err != nil {
		t.Fatalf("execute: %v", err)
	}
	host := hostConfig(t, docker.created["openplc-bootloader"])
	if host["RestartPolicy"].(map[string]any)["Name"] != "always" {
		t.Fatalf("want a restart policy applied, got %v", host["RestartPolicy"])
	}
}

func TestTheRuntimeContainerIsNeverTouched(t *testing.T) {
	// A bootloader update must not interrupt a running PLC: losing the ability
	// to manage a device is a bad afternoon, stopping its plant is not.
	docker := newFake()
	docker.containers["openplc-bootloader"] = parentContainer()
	runtime := &dockerapi.ContainerInspect{ID: "runtimeid", Name: "/openplc-runtime"}
	docker.containers["openplc-runtime"] = runtime
	setChildEnv(t, "openplc-bootloader", "ghcr.io/x/bootloader:bootloader-v1.1.0")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := Execute(ctx, docker, quietLogger()); err != nil {
		t.Fatalf("execute: %v", err)
	}

	for _, name := range docker.removed {
		if name == "openplc-runtime" {
			t.Fatal("the runtime container must never be removed by a self-update")
		}
	}
	if _, recreated := docker.created["openplc-runtime"]; recreated {
		t.Fatal("the runtime container must never be recreated by a self-update")
	}
	if docker.containers["openplc-runtime"] == nil {
		t.Fatal("the runtime container must still exist")
	}
}

// A create that fails must leave the device with the bootloader it has.
//
// The parent used to be force-removed first. If the create was then rejected
// -- an invalid HostConfig on an older daemon, a full disk, an image pruned
// between the pull and the create -- the helper exited with RestartPolicy: no
// and the device had no bootloader at all, on hardware that by this feature's
// own framing has no SSH.
func TestAFailedCreateLeavesTheOldBootloaderRunning(t *testing.T) {
	docker := newFake()
	docker.containers["openplc-bootloader"] = parentContainer()
	docker.createErr = errors.New("invalid HostConfig")
	setChildEnv(t, "openplc-bootloader", "ghcr.io/x/bootloader:bootloader-v1.1.0")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	err := Execute(ctx, docker, quietLogger())
	if err == nil {
		t.Fatal("a rejected create must be reported, not swallowed")
	}

	for _, name := range docker.removed {
		if name == "openplc-bootloader" {
			t.Fatalf("the running bootloader was removed before the replacement existed: %v",
				docker.removed)
		}
	}
	if len(docker.started) != 0 {
		t.Errorf("nothing should have been started, got %v", docker.started)
	}
}
