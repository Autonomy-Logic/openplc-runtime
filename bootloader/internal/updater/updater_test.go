package updater

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/dockerapi"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/runtimespec"
)

// --- fakes ---------------------------------------------------------------

type fakeDocker struct {
	mu sync.Mutex

	pullErr     error
	pulled      []string
	removed     []string
	removeErr   error
	inspectSize int64
	inspectErr  error
	// pullSteps are progress reports the fake emits before finishing.
	pullSteps []dockerapi.PullProgress
}

func (f *fakeDocker) PullImage(_ context.Context, ref string, onProgress func(dockerapi.PullProgress)) error {
	f.mu.Lock()
	f.pulled = append(f.pulled, ref)
	steps, err := f.pullSteps, f.pullErr
	f.mu.Unlock()

	for _, step := range steps {
		if onProgress != nil {
			onProgress(step)
		}
	}
	return err
}

func (f *fakeDocker) InspectImage(context.Context, string) (*dockerapi.ImageInfo, error) {
	if f.inspectErr != nil {
		return nil, f.inspectErr
	}
	return &dockerapi.ImageInfo{Size: f.inspectSize}, nil
}

func (f *fakeDocker) RemoveImage(_ context.Context, ref string, _ bool) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removed = append(f.removed, ref)
	return f.removeErr
}

func (f *fakeDocker) pulls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.pulled...)
}

func (f *fakeDocker) removals() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.removed...)
}

type fakeSupervisor struct {
	mu sync.Mutex

	beginErr      error
	reconcileErr  error
	begins        int
	ends          int
	stops         int
	reconciles    int
	recoveryCalls []string
	// order records the sequence of operations, which is the property that
	// actually matters for safety.
	order []string
}

func (f *fakeSupervisor) BeginUpdate() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.begins++
	f.order = append(f.order, "begin")
	return f.beginErr
}

func (f *fakeSupervisor) EndUpdate() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ends++
	f.order = append(f.order, "end")
}

func (f *fakeSupervisor) Stop(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.stops++
	f.order = append(f.order, "stop")
	return nil
}

func (f *fakeSupervisor) Reconcile(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reconciles++
	f.order = append(f.order, "reconcile")
	return f.reconcileErr
}

func (f *fakeSupervisor) EnterRecovery(_ context.Context, reason string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.recoveryCalls = append(f.recoveryCalls, reason)
	f.order = append(f.order, "recovery")
}

func (f *fakeSupervisor) snapshot() ([]string, []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.order...), append([]string(nil), f.recoveryCalls...)
}

func newTestUpdater(t *testing.T, docker DockerClient, sup Supervisor) (*Updater, *runtimespec.Config, string) {
	t.Helper()
	dir := t.TempDir()
	specPath := filepath.Join(dir, "runtime-spec.json")
	if err := os.WriteFile(specPath, []byte(`{"version":"v4.2.0"}`), 0o600); err != nil {
		t.Fatalf("seeding spec: %v", err)
	}
	spec, err := runtimespec.Load(specPath)
	if err != nil {
		t.Fatalf("loading spec: %v", err)
	}
	u := New(Config{
		Docker:     docker,
		Supervisor: sup,
		Spec:       spec,
		SpecPath:   specPath,
		StateDir:   dir,
		Log:        slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	return u, spec, specPath
}

// waitFor polls until cond holds or the deadline passes. The update runs in a
// goroutine, so tests observe it rather than drive it.
func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("timed out waiting for the update to reach the expected state")
}

func waitForState(t *testing.T, u *Updater, want State) Progress {
	t.Helper()
	waitFor(t, func() bool { return u.Progress().State == want })
	return u.Progress()
}

// --- happy path ----------------------------------------------------------

func TestASuccessfulUpdateFollowsTheSafeOrder(t *testing.T) {
	// The order IS the safety property: pull before stopping anything, and
	// remove the old image only after the new one is confirmed running. Any
	// other order leaves a window where the device has no usable image.
	docker := &fakeDocker{inspectSize: 100}
	sup := &fakeSupervisor{}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	waitForState(t, u, StateSuccess)

	order, _ := sup.snapshot()
	// begin, stop, reconcile, end -- the pull happens between begin and stop.
	want := []string{"begin", "stop", "reconcile", "end"}
	if strings.Join(order, ",") != strings.Join(want, ",") {
		t.Fatalf("want order %v, got %v", want, order)
	}
	if got := docker.pulls(); len(got) != 1 || !strings.HasSuffix(got[0], ":v4.2.1") {
		t.Fatalf("want a single pull of the target, got %v", got)
	}
	if got := docker.removals(); len(got) != 1 || !strings.HasSuffix(got[0], ":v4.2.0") {
		t.Fatalf("want the previous image removed, got %v", got)
	}
}

func TestTheNewVersionIsRecordedBeforeTheContainerIsRecreated(t *testing.T) {
	// A power cut mid-swap must leave the device booting the version it was
	// moving to -- the image for which is already on disk by then -- rather
	// than silently reverting to one the operator was told was replaced.
	docker := &fakeDocker{inspectSize: 100}
	sup := &fakeSupervisor{}
	u, _, specPath := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	waitForState(t, u, StateSuccess)

	reloaded, err := runtimespec.Load(specPath)
	if err != nil {
		t.Fatalf("reloading spec: %v", err)
	}
	if reloaded.Version != "v4.2.1" {
		t.Fatalf("the spec must persist the new version, got %q", reloaded.Version)
	}
}

func TestProgressIsReportedDuringThePull(t *testing.T) {
	fifty := 50
	docker := &fakeDocker{
		inspectSize: 100,
		pullSteps: []dockerapi.PullProgress{
			{Phase: "Downloading", Percent: &fifty},
		},
	}
	u, _, _ := newTestUpdater(t, docker, &fakeSupervisor{})

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	waitForState(t, u, StateSuccess)

	// The final snapshot clears the percentage, so this checks the labels
	// that survive: from and to.
	final := u.Progress()
	if final.From != "v4.2.0" || final.To != "v4.2.1" {
		t.Fatalf("want from v4.2.0 to v4.2.1, got %q -> %q", final.From, final.To)
	}
	if final.FinishedAt == nil {
		t.Fatal("a finished update must carry a finish time")
	}
}

func TestReinstallingTheCurrentVersionIsAllowed(t *testing.T) {
	// Re-pulling the running version is the only repair an operator can
	// perform from the editor when an image is damaged, so refusing it would
	// remove a genuinely useful action.
	docker := &fakeDocker{inspectSize: 100}
	sup := &fakeSupervisor{}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v4.2.0"); err != nil {
		t.Fatalf("reinstalling the current version must be allowed: %v", err)
	}
	waitForState(t, u, StateSuccess)

	// Nothing may be removed: the "previous" image IS the running one.
	if got := docker.removals(); len(got) != 0 {
		t.Fatalf("a reinstall must not delete the image it just installed, got %v", got)
	}
}

// --- failures ------------------------------------------------------------

func TestAFailedPullEntersRecoveryAndTouchesNothing(t *testing.T) {
	docker := &fakeDocker{inspectSize: 100, pullErr: errors.New("manifest unknown")}
	sup := &fakeSupervisor{}
	u, _, specPath := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v9.9.9"); err != nil {
		t.Fatalf("start: %v", err)
	}
	progress := waitForState(t, u, StateFailed)

	if !strings.Contains(progress.Error, "manifest unknown") {
		t.Fatalf("the underlying cause must reach the operator, got %q", progress.Error)
	}
	order, reasons := sup.snapshot()
	// The runtime must not have been stopped: the pull never succeeded, so
	// there was never a reason to interrupt a working PLC.
	for _, step := range order {
		if step == "stop" {
			t.Fatalf("a failed pull must not stop the running runtime: %v", order)
		}
	}
	if len(reasons) != 1 {
		t.Fatalf("want one recovery call, got %v", reasons)
	}
	// And the recorded version must be unchanged.
	reloaded, err := runtimespec.Load(specPath)
	if err != nil {
		t.Fatalf("reloading spec: %v", err)
	}
	if reloaded.Version != "v4.2.0" {
		t.Fatalf("a failed pull must not change the recorded version, got %q", reloaded.Version)
	}
}

func TestANewVersionThatWillNotStartEntersRecoveryWithTheOldImageIntact(t *testing.T) {
	// This is the case the pull-first ordering exists for: the operator is
	// handed a device in recovery that still has the previous image on disk,
	// so reinstalling it needs no network.
	docker := &fakeDocker{inspectSize: 100}
	sup := &fakeSupervisor{reconcileErr: errors.New("exited during start-up with code 1")}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	progress := waitForState(t, u, StateFailed)

	if !strings.Contains(progress.Error, "did not start") {
		t.Fatalf("want a start failure, got %q", progress.Error)
	}
	if got := docker.removals(); len(got) != 0 {
		t.Fatalf("the previous image must survive a failed start, got %v", got)
	}
	_, reasons := sup.snapshot()
	if len(reasons) != 1 || !strings.Contains(reasons[0], "v4.2.1") {
		t.Fatalf("recovery must name the version that failed, got %v", reasons)
	}
}

func TestFailingToRemoveTheOldImageDoesNotFailTheUpdate(t *testing.T) {
	// The new version is running; disk was simply not reclaimed. Rolling back
	// a working runtime over that would be absurd.
	docker := &fakeDocker{inspectSize: 100, removeErr: errors.New("image is in use")}
	sup := &fakeSupervisor{}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	progress := waitForState(t, u, StateSuccess)
	if progress.Error != "" {
		t.Fatalf("a failed cleanup must not surface as an update error: %q", progress.Error)
	}
	_, reasons := sup.snapshot()
	if len(reasons) != 0 {
		t.Fatalf("must not enter recovery, got %v", reasons)
	}
}

func TestTheSupervisorClaimIsAlwaysReleased(t *testing.T) {
	// Leaking the claim would suppress crash accounting forever, so a runtime
	// that started crash-looping after a failed update would never reach
	// recovery.
	docker := &fakeDocker{inspectSize: 100, pullErr: errors.New("boom")}
	sup := &fakeSupervisor{}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	waitForState(t, u, StateFailed)
	waitFor(t, func() bool {
		sup.mu.Lock()
		defer sup.mu.Unlock()
		return sup.ends == sup.begins && sup.begins == 1
	})
}

// --- single flight -------------------------------------------------------

func TestASecondConcurrentUpdateIsRefused(t *testing.T) {
	// Two concurrent swaps of one container is not a state worth trying to
	// make safe.
	release := make(chan struct{})
	docker := &blockingDocker{release: release}
	u, _, _ := newTestUpdater(t, docker, &fakeSupervisor{})

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("first start: %v", err)
	}
	waitForState(t, u, StatePulling)

	if err := u.Start(context.Background(), "v4.2.2"); !errors.Is(err, ErrInProgress) {
		t.Fatalf("want ErrInProgress, got %v", err)
	}
	close(release)
	waitForState(t, u, StateSuccess)
}

func TestARefusedClaimFromTheSupervisorIsSurfaced(t *testing.T) {
	// The supervisor is the other party that can say no -- for instance if it
	// is already mid-update from another path.
	sup := &fakeSupervisor{beginErr: errors.New("an update is already in progress")}
	u, _, _ := newTestUpdater(t, &fakeDocker{inspectSize: 100}, sup)

	if err := u.Start(context.Background(), "v4.2.1"); err == nil {
		t.Fatal("want the supervisor's refusal to propagate")
	}
	if got := u.Progress().State; got != StateIdle {
		t.Fatalf("a refused start must leave the state idle, got %q", got)
	}
}

type blockingDocker struct {
	release chan struct{}
}

func (b *blockingDocker) PullImage(_ context.Context, _ string, _ func(dockerapi.PullProgress)) error {
	<-b.release
	return nil
}
func (b *blockingDocker) InspectImage(context.Context, string) (*dockerapi.ImageInfo, error) {
	return &dockerapi.ImageInfo{Size: 100}, nil
}
func (b *blockingDocker) RemoveImage(context.Context, string, bool) error { return nil }

// --- disk pre-check ------------------------------------------------------

func TestAnImpossiblyLargeImageIsRefusedBeforeAnythingIsTouched(t *testing.T) {
	// Refusing up front with a number is far better than a half-finished
	// pull and an ENOSPC an operator has to interpret.
	docker := &fakeDocker{inspectSize: 1 << 62} // larger than any real disk
	sup := &fakeSupervisor{}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	progress := waitForState(t, u, StateFailed)

	// Only meaningful where free space can actually be measured.
	if free, err := freeBytes(t.TempDir()); err != nil || free == 0 {
		t.Skip("free space is not measurable on this platform")
	}
	if !strings.Contains(progress.Error, "not enough free space") {
		t.Fatalf("want a free-space refusal, got %q", progress.Error)
	}
	if got := docker.pulls(); len(got) != 0 {
		t.Fatalf("nothing may be pulled after the pre-check fails, got %v", got)
	}
}

func TestNoLocalImageSkipsTheDiskPreCheck(t *testing.T) {
	// A first install has nothing to estimate from, and refusing on that
	// basis would block the very first deployment.
	docker := &fakeDocker{inspectErr: errors.New("no such image")}
	u, _, _ := newTestUpdater(t, docker, &fakeSupervisor{})

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	waitForState(t, u, StateSuccess)
}

// --- version validation --------------------------------------------------

func TestAVersionThatCouldRedirectThePullIsRefused(t *testing.T) {
	// The reference is built as repository + ":" + version, so a slash, colon
	// or '@' in the version could point the pull at another repository,
	// another registry, or a digest entirely.
	u, _, _ := newTestUpdater(t, &fakeDocker{inspectSize: 100}, &fakeSupervisor{})
	for _, version := range []string{
		"v1/../../evil",
		"evil.example.com/openplc:v1",
		"v4.2.1@sha256:deadbeef",
		"v4.2.1 --privileged",
		"",
		".leading-dot",
		"-leading-dash",
	} {
		if err := u.Start(context.Background(), version); err == nil {
			t.Fatalf("version %q must be refused", version)
		}
	}
}

func TestOrdinaryVersionTagsAreAccepted(t *testing.T) {
	for _, version := range []string{"v4.2.1", "v4.1.0-rc.1", "latest", "v4.1.10"} {
		if err := validateVersion(version); err != nil {
			t.Fatalf("version %q must be accepted: %v", version, err)
		}
	}
}

// --- helpers -------------------------------------------------------------

func TestHumanBytesReadsLikeAnErrorMessage(t *testing.T) {
	cases := map[int64]string{
		512:                    "512 B",
		1536:                   "1.5 KiB",
		974 * 1024 * 1024:      "974.0 MiB",
		3 * 1024 * 1024 * 1024: "3.0 GiB",
	}
	for input, want := range cases {
		if got := humanBytes(input); got != want {
			t.Errorf("humanBytes(%d) = %q, want %q", input, got, want)
		}
	}
}
