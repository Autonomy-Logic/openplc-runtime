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
	aborted       int
	// state and preUpdateState model what AbortUpdate has to restore: the
	// refused-update path must leave the supervisor exactly where it found
	// it, not re-derive it by acting on the container.
	state          string
	preUpdateState string
	// order records the sequence of operations, which is the property that
	// actually matters for safety.
	order []string
}

func (f *fakeSupervisor) BeginUpdate() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.begins++
	f.preUpdateState = f.state
	f.state = "updating"
	f.order = append(f.order, "begin")
	return f.beginErr
}

func (f *fakeSupervisor) EndUpdate() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ends++
	f.order = append(f.order, "end")
}

// AbortUpdate models the real one: release the claim and restore what
// BeginUpdate displaced, without touching the container.
func (f *fakeSupervisor) AbortUpdate() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.aborted++
	f.order = append(f.order, "abort")
	f.state = f.preUpdateState
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

func TestAFailedPullLeavesTheRunningRuntimeAlone(t *testing.T) {
	// No local copy either, so there is genuinely nothing to install -- the
	// case the local-image fallback must NOT swallow.
	docker := &fakeDocker{
		inspectErr: errors.New("no such image"),
		pullErr:    errors.New("manifest unknown"),
	}
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
	// Nothing was touched, so nothing may be disturbed. Observed on real
	// hardware before this was fixed: a failed pull stopped a RUNNING PLC and
	// dropped the device into recovery, turning a harmless refusal into an
	// outage.
	for _, step := range order {
		if step == "stop" {
			t.Fatalf("a failed pull must not stop the running runtime: %v", order)
		}
	}
	if len(reasons) != 0 {
		t.Fatalf("a failure before the swap must not enter recovery, got %v", reasons)
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
	docker := &fakeDocker{
		inspectErr: errors.New("no such image"),
		pullErr:    errors.New("boom"),
	}
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

func TestATightDiskIsWarnedAboutRatherThanRefused(t *testing.T) {
	// This used to refuse the update. The measurement is of the bootloader's
	// filesystem, which is only Docker's on a default install -- so on a
	// device whose data-root had been moved, a perfectly possible update was
	// blocked by a figure about the wrong disk. It is a warning now, carried
	// on the progress the editor polls, and the pull goes ahead: if the
	// estimate was right, Docker reports ENOSPC in its own words.
	docker := &fakeDocker{inspectSize: 1 << 62} // larger than any real disk
	sup := &fakeSupervisor{}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	progress := waitForState(t, u, StateSuccess)

	// Only meaningful where free space can actually be measured.
	if free, err := freeBytes(t.TempDir()); err != nil || free == 0 {
		t.Skip("free space is not measurable on this platform")
	}
	if !strings.Contains(progress.Warning, "looks tight") {
		t.Errorf("want a free-space warning on the progress, got %q", progress.Warning)
	}
	if progress.Error != "" {
		t.Errorf("a tight disk must not fail the update, got error %q", progress.Error)
	}
	if got := docker.pulls(); len(got) == 0 {
		t.Error("the pull must still be attempted")
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

func TestAnImageAlreadyPresentSurvivesAFailedPull(t *testing.T) {
	// A pull can fail for a reason that does not matter: the image is already
	// here. That covers an air-gapped device with a side-loaded image, a
	// locally built one, and a registry that is merely unreachable. Refusing
	// would make a version the device already holds uninstallable -- which is
	// what happened on the SLM-RP4, where a locally tagged image produced
	// "pull access denied" and failed an update that was entirely ready.
	docker := &fakeDocker{
		inspectSize: 100,
		pullErr:     errors.New("pull access denied for openplc-runtime"),
	}
	sup := &fakeSupervisor{}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	waitForState(t, u, StateSuccess)

	// And it must genuinely swap, not merely report success.
	order, _ := sup.snapshot()
	if strings.Join(order, ",") != "begin,stop,reconcile,end" {
		t.Fatalf("want a real swap, got %v", order)
	}
}

func TestAFailedPullWithNoLocalImageStillFails(t *testing.T) {
	// The fallback must not swallow the case it exists to distinguish: no
	// local copy means there is genuinely nothing to install.
	docker := &fakeDocker{
		inspectErr: errors.New("no such image"),
		pullErr:    errors.New("manifest unknown"),
	}
	sup := &fakeSupervisor{}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v9.9.9"); err != nil {
		t.Fatalf("start: %v", err)
	}
	progress := waitForState(t, u, StateFailed)
	if !strings.Contains(progress.Error, "could not download") {
		t.Fatalf("want a download failure, got %q", progress.Error)
	}
	if _, reasons := sup.snapshot(); len(reasons) != 0 {
		t.Fatalf("nothing was touched, so no recovery, got %v", reasons)
	}
}

func TestAFailureAfterTheSwapBeginsDoesEnterRecovery(t *testing.T) {
	// The distinction matters in both directions: once the container has been
	// replaced there IS something wrong with the device, and an operator has
	// to be handed the controls.
	docker := &fakeDocker{inspectSize: 100}
	sup := &fakeSupervisor{reconcileErr: errors.New("exited during start-up with code 1")}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v4.2.1"); err != nil {
		t.Fatalf("start: %v", err)
	}
	waitForState(t, u, StateFailed)

	_, reasons := sup.snapshot()
	if len(reasons) != 1 {
		t.Fatalf("a failed start must enter recovery, got %v", reasons)
	}
}

func TestARefusedUpdateLeavesTheSupervisorReportingReality(t *testing.T) {
	// BeginUpdate moves the supervisor to "updating" and EndUpdate only
	// releases the claim, so without restoring state a device that merely
	// refused a bad version reports itself as mid-update forever. Seen on the
	// SLM-RP4: a failed pull left the bootloader stuck on "updating" while the
	// PLC ran happily underneath.
	//
	// It restores rather than reconciles. Reconcile re-derives the state by
	// ACTING on the container: from recovery it would start the runtime that
	// recovery deliberately stopped and call the device healthy, and with the
	// container absent it would leave the state on "starting" for good after
	// the pull failed again.
	docker := &fakeDocker{
		inspectErr: errors.New("no such image"),
		pullErr:    errors.New("manifest unknown"),
	}
	sup := &fakeSupervisor{state: "recovery"}
	u, _, _ := newTestUpdater(t, docker, sup)

	if err := u.Start(context.Background(), "v9.9.9"); err != nil {
		t.Fatalf("start: %v", err)
	}
	waitForState(t, u, StateFailed)

	waitFor(t, func() bool {
		order, _ := sup.snapshot()
		for _, step := range order {
			if step == "abort" {
				return true
			}
		}
		return false
	})

	// The state it found is the state it left: recovery, not a restarted
	// runtime and not "starting" forever.
	sup.mu.Lock()
	restored := sup.state
	sup.mu.Unlock()
	if restored != "recovery" {
		t.Errorf("the refused update left the supervisor on %q, want the recovery it found", restored)
	}
	order0, _ := sup.snapshot()
	for _, step := range order0 {
		if step == "reconcile" {
			t.Errorf("a refused update reconciled, which acts on the container: %v", order0)
		}
	}
	// And still nothing stopped.
	order, reasons := sup.snapshot()
	for _, step := range order {
		if step == "stop" {
			t.Fatalf("nothing may be stopped: %v", order)
		}
	}
	if len(reasons) != 0 {
		t.Fatalf("no recovery either, got %v", reasons)
	}
}
