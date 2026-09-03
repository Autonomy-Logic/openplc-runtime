package supervisor

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

// --- fakes ---------------------------------------------------------------

// fakeDocker is a scriptable stand-in for the Docker daemon. Container state
// is a small struct rather than a full inspect so a test can say "running and
// healthy" in one line.
type fakeDocker struct {
	mu sync.Mutex

	exists  bool
	running bool
	health  string // "", "starting", "healthy", "unhealthy"
	exit    int

	created  int
	started  int
	stopped  int
	removed  int
	startErr error

	// startMakesHealthy models the normal case: starting the container brings
	// the webserver up.
	startMakesHealthy bool

	// imagePresent models the local image store. false means the supervisor
	// has to pull before it can create anything -- the fresh-install case.
	imagePresent bool
	pullErr      error
	pulls        []string
	// onPullProgress runs from inside the pull's progress callback, so a test
	// can observe supervisor state at that instant rather than afterwards.
	onPullProgress func()
	observed       Status
}

func (f *fakeDocker) Ping(context.Context) error { return nil }

func (f *fakeDocker) InspectContainer(_ context.Context, _ string) (*dockerapi.ContainerInspect, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.exists {
		return nil, &dockerapi.APIError{Status: http.StatusNotFound, Path: "/containers/x/json"}
	}
	inspect := &dockerapi.ContainerInspect{ID: "deadbeef"}
	inspect.State.Running = f.running
	inspect.State.ExitCode = f.exit
	if f.health != "" {
		inspect.State.Health = &struct {
			Status string `json:"Status"`
		}{Status: f.health}
	}
	return inspect, nil
}

func (f *fakeDocker) CreateContainer(_ context.Context, _ string, _ any) (*dockerapi.CreateContainerResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.created++
	f.exists = true
	f.running = false
	return &dockerapi.CreateContainerResponse{ID: "deadbeef"}, nil
}

func (f *fakeDocker) StartContainer(context.Context, string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.started++
	if f.startErr != nil {
		return f.startErr
	}
	f.running = true
	if f.startMakesHealthy {
		f.health = "healthy"
	}
	return nil
}

func (f *fakeDocker) StopContainer(context.Context, string, time.Duration) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.stopped++
	f.running = false
	return nil
}

func (f *fakeDocker) RemoveContainer(context.Context, string, bool) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removed++
	f.exists = false
	f.running = false
	return nil
}

func (f *fakeDocker) StreamEvents(ctx context.Context, _ string, _ func(dockerapi.Event)) error {
	<-ctx.Done()
	return ctx.Err()
}

func (f *fakeDocker) InspectImage(_ context.Context, ref string) (*dockerapi.ImageInfo, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.imagePresent {
		return nil, &dockerapi.APIError{Status: http.StatusNotFound, Path: "/images/" + ref + "/json"}
	}
	return &dockerapi.ImageInfo{ID: "sha256:image", Size: 1000}, nil
}

func (f *fakeDocker) PullImage(_ context.Context, ref string, onProgress func(dockerapi.PullProgress)) error {
	f.mu.Lock()
	f.pulls = append(f.pulls, ref)
	err := f.pullErr
	f.mu.Unlock()

	if err != nil {
		return err
	}
	if onProgress != nil {
		half := 50
		onProgress(dockerapi.PullProgress{Phase: "Downloading", Percent: &half})
		if f.onPullProgress != nil {
			f.onPullProgress()
		}
	}
	f.mu.Lock()
	f.imagePresent = true
	f.mu.Unlock()
	return nil
}

func (f *fakeDocker) pullCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.pulls)
}

func (f *fakeDocker) counts() (created, started, stopped int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.created, f.started, f.stopped
}

type fakeSpec struct{}

func (fakeSpec) ContainerSpec(string) any { return map[string]string{"Image": "test:1"} }
func (fakeSpec) ImageRef() string         { return "test:1" }

type fakeProbe struct {
	mu  sync.Mutex
	err error
}

func (p *fakeProbe) Probe(context.Context) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.err
}

func (p *fakeProbe) set(err error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.err = err
}

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// newTestSupervisor wires a supervisor with fast timings so tests do not sleep
// through real start-up windows.
func newTestSupervisor(docker DockerClient, probe HealthProber) *Supervisor {
	return New(docker, fakeSpec{}, probe, Config{
		ContainerName:    "test-runtime",
		StartTimeout:     200 * time.Millisecond,
		StopGrace:        time.Second,
		RestartDelayBase: time.Millisecond,
	}, quietLogger())
}

func dieEvent(code string) dockerapi.Event {
	event := dockerapi.Event{Type: "container", Action: "die"}
	event.Actor.Attributes = map[string]string{"name": "test-runtime", "exitCode": code}
	return event
}

// --- reconcile -----------------------------------------------------------

func TestReconcileCreatesAndStartsAMissingContainer(t *testing.T) {
	docker := &fakeDocker{startMakesHealthy: true, imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})

	if err := sup.Reconcile(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	created, started, _ := docker.counts()
	if created != 1 || started != 1 {
		t.Fatalf("want 1 create and 1 start, got create=%d start=%d", created, started)
	}
	if got := sup.Status().State; got != StateHealthy {
		t.Fatalf("want %q, got %q", StateHealthy, got)
	}
}

func TestReconcileAdoptsAHealthyRunningContainer(t *testing.T) {
	// The bootloader restarts far more often than the runtime does -- its own
	// crash, a self-update. A reconcile that recreated or bounced a working
	// runtime would turn a bootloader hiccup into a plant outage.
	docker := &fakeDocker{exists: true, running: true, health: "healthy", imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})

	if err := sup.Reconcile(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	created, started, stopped := docker.counts()
	if created != 0 || started != 0 || stopped != 0 {
		t.Fatalf("adoption must not touch the container, got create=%d start=%d stop=%d",
			created, started, stopped)
	}
	if got := sup.Status().State; got != StateHealthy {
		t.Fatalf("want %q, got %q", StateHealthy, got)
	}
}

func TestReconcileStartsAnExistingStoppedContainer(t *testing.T) {
	docker := &fakeDocker{exists: true, running: false, startMakesHealthy: true, imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})

	if err := sup.Reconcile(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	created, started, _ := docker.counts()
	if created != 0 {
		t.Fatalf("an existing container must be reused, not recreated (created=%d)", created)
	}
	if started != 1 {
		t.Fatalf("want 1 start, got %d", started)
	}
}

func TestReconcileFallsBackToTheProbeWhenTheImageHasNoHealthcheck(t *testing.T) {
	// Images built before the HEALTHCHECK landed report no health status. They
	// must still be supervised rather than assumed fine.
	docker := &fakeDocker{exists: true, running: true, health: "", imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})

	if err := sup.Reconcile(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	status := sup.Status()
	if status.State != StateHealthy {
		t.Fatalf("want %q, got %q", StateHealthy, status.State)
	}
	if status.HealthSource != "api probe" {
		t.Fatalf("want health from the api probe, got %q", status.HealthSource)
	}
}

// --- crash accounting ----------------------------------------------------

func TestAnExpectedStopIsNotCountedAsACrash(t *testing.T) {
	// This is the bug that would make the first successful update look like a
	// crash-loop: the runtime exits because we asked it to, and if that counts,
	// a perfectly healthy device drops into recovery.
	docker := &fakeDocker{exists: true, running: true, health: "healthy", startMakesHealthy: true, imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})
	ctx := context.Background()

	if err := sup.Stop(ctx); err != nil {
		t.Fatalf("stop: %v", err)
	}
	sup.handleEvent(ctx, dieEvent("143")) // SIGTERM

	if got := sup.Status().CrashCount; got != 0 {
		t.Fatalf("a stop we asked for must not count as a crash, got %d", got)
	}
	if got := sup.Status().State; got == StateRecovery {
		t.Fatal("an expected stop must never enter recovery")
	}
}

func TestRepeatedUnexpectedExitsEnterRecovery(t *testing.T) {
	docker := &fakeDocker{exists: true, running: true, health: "healthy", startMakesHealthy: true, imagePresent: true}
	sup := New(docker, fakeSpec{}, &fakeProbe{}, Config{
		ContainerName:    "test-runtime",
		MaxCrashes:       3,
		CrashWindow:      5 * time.Minute,
		StartTimeout:     50 * time.Millisecond,
		StopGrace:        time.Second,
		RestartDelayBase: time.Millisecond,
	}, quietLogger())
	ctx := context.Background()

	for i := 0; i < 3; i++ {
		sup.handleEvent(ctx, dieEvent("1"))
	}

	status := sup.Status()
	if status.State != StateRecovery {
		t.Fatalf("want %q after 3 crashes, got %q", StateRecovery, status.State)
	}
	if status.Reason == "" {
		t.Fatal("recovery must carry a reason an operator can read")
	}
}

func TestRecoveryStopsTheRuntimeSoDiscoveryStaysExclusive(t *testing.T) {
	// Only one service on the host may answer the UDP discovery broadcast.
	// Recovery is defined as "the runtime is not running", which is what lets
	// the bootloader's responder switch on without ever racing the runtime's.
	docker := &fakeDocker{exists: true, running: true, health: "healthy", imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})

	sup.EnterRecovery(context.Background(), "test")

	if _, _, stopped := docker.counts(); stopped != 1 {
		t.Fatalf("entering recovery must stop the runtime, stop calls=%d", stopped)
	}
	if docker.running {
		t.Fatal("runtime must not be running in recovery")
	}
}

func TestASuccessfulRestartDoesNotEraseTheCrashHistory(t *testing.T) {
	// The common crash-loop shape is: die, come back up fine, die again -- a
	// program that faults on load lets the webserver start before it takes the
	// process down. If a healthy start cleared the count, the evidence would be
	// zeroed between every crash, the threshold could never be reached, and the
	// supervisor would restart forever instead of handing the device over.
	docker := &fakeDocker{exists: true, running: true, health: "healthy", startMakesHealthy: true, imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})
	ctx := context.Background()

	sup.handleEvent(ctx, dieEvent("1"))

	if got := sup.Status().State; got != StateHealthy {
		t.Fatalf("the runtime came back up, want %q, got %q", StateHealthy, got)
	}
	if got := sup.Status().CrashCount; got != 1 {
		t.Fatalf("the crash must still be on record after a healthy restart, got %d", got)
	}
}

func TestTheCrashHistoryIsForgottenByAgeNotByRecovery(t *testing.T) {
	// Forgetting still has to happen, or crashes weeks apart would accumulate
	// into a false loop. The sliding window does it by aging entries out, which
	// is the only forgetting that is wanted.
	docker := &fakeDocker{exists: true, running: true, health: "healthy", startMakesHealthy: true, imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})
	now := time.Now()
	sup.crashes.now = func() time.Time { return now }

	sup.handleEvent(context.Background(), dieEvent("1"))
	if got := sup.Status().CrashCount; got != 1 {
		t.Fatalf("want 1 crash on record, got %d", got)
	}

	now = now.Add(DefaultCrashWindow + time.Minute)
	if got := sup.Status().CrashCount; got != 0 {
		t.Fatalf("a crash older than the window must be forgotten, got %d", got)
	}
}

func TestAnUnhealthyEventStopsTheWedgedRuntime(t *testing.T) {
	// A hung webserver never emits a die event, so without acting on the
	// healthcheck the supervisor would idle forever beside a dead runtime.
	// Stopping it converts the hang into a die, which then flows through the
	// ordinary crash accounting.
	docker := &fakeDocker{exists: true, running: true, health: "healthy", imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})

	event := dockerapi.Event{Type: "container", Action: "health_status: unhealthy"}
	event.Actor.Attributes = map[string]string{"name": "test-runtime"}
	sup.handleEvent(context.Background(), event)

	if _, _, stopped := docker.counts(); stopped != 1 {
		t.Fatalf("an unhealthy runtime must be stopped, stop calls=%d", stopped)
	}
}

func TestEventsForOtherContainersAreIgnored(t *testing.T) {
	docker := &fakeDocker{exists: true, running: true, health: "healthy", imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})

	event := dockerapi.Event{Type: "container", Action: "die"}
	event.Actor.Attributes = map[string]string{"name": "somebody-elses-plc", "exitCode": "1"}
	sup.handleEvent(context.Background(), event)

	if got := sup.Status().CrashCount; got != 0 {
		t.Fatalf("another container's death is not ours to count, got %d", got)
	}
}

// --- update claim --------------------------------------------------------

func TestBeginUpdateRefusesASecondConcurrentUpdate(t *testing.T) {
	docker := &fakeDocker{exists: true, running: true, health: "healthy", imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})

	if err := sup.BeginUpdate(); err != nil {
		t.Fatalf("first BeginUpdate: %v", err)
	}
	if err := sup.BeginUpdate(); err == nil {
		t.Fatal("a second concurrent update must be refused")
	}
}

func TestExitsDuringAnUpdateAreNotCrashes(t *testing.T) {
	docker := &fakeDocker{exists: true, running: true, health: "healthy", imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})

	if err := sup.BeginUpdate(); err != nil {
		t.Fatalf("BeginUpdate: %v", err)
	}
	sup.handleEvent(context.Background(), dieEvent("143"))

	if got := sup.Status().CrashCount; got != 0 {
		t.Fatalf("an exit during an update is expected, got crashCount=%d", got)
	}
}

// --- crash window -------------------------------------------------------

func TestCrashWindowForgetsEntriesThatAgedOut(t *testing.T) {
	now := time.Now()
	window := newCrashWindow(3, 5*time.Minute)
	window.now = func() time.Time { return now }

	if looping := window.record(); looping {
		t.Fatal("one crash is not a loop")
	}
	if looping := window.record(); looping {
		t.Fatal("two crashes is not a loop")
	}

	// Six minutes later both have aged out, so the next crash starts over.
	now = now.Add(6 * time.Minute)
	if looping := window.record(); looping {
		t.Fatal("crashes outside the window must not count")
	}
	if got := window.count(); got != 1 {
		t.Fatalf("want 1 crash in window, got %d", got)
	}
}

func TestCrashWindowTripsAtTheThreshold(t *testing.T) {
	window := newCrashWindow(3, 5*time.Minute)
	if window.record() || window.record() {
		t.Fatal("must not trip before the threshold")
	}
	if !window.record() {
		t.Fatal("must trip on the third crash inside the window")
	}
}

func TestRestartDelayBacksOffAndIsCapped(t *testing.T) {
	base := DefaultRestartDelay
	if got := restartDelay(base, 0); got != 0 {
		t.Fatalf("no failures means no delay, got %s", got)
	}
	if got := restartDelay(base, 1); got != base {
		t.Fatalf("want %s, got %s", base, got)
	}
	if restartDelay(base, 2) <= restartDelay(base, 1) {
		t.Fatal("delay must grow with consecutive failures")
	}
	if got := restartDelay(base, 50); got != MaxRestartDelay {
		t.Fatalf("delay must be capped at %s, got %s", MaxRestartDelay, got)
	}
}

// --- start-up failures ---------------------------------------------------

func TestStartTimeoutIsReportedRatherThanHanging(t *testing.T) {
	// A container that never becomes healthy emits no event to wait for, so a
	// timeout is the only way to notice.
	docker := &fakeDocker{exists: true, running: false, health: "starting", imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{err: errors.New("connection refused")})

	err := sup.Reconcile(context.Background())
	if err == nil {
		t.Fatal("a runtime that never becomes healthy must surface an error")
	}
}

func TestRunEntersRecoveryWhenTheRuntimeCannotStart(t *testing.T) {
	// Recovery must be reachable precisely when the runtime will not come up:
	// that is the case RTOP-283 exists for.
	docker := &fakeDocker{startErr: errors.New("no such image"), imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{err: errors.New("down")})

	ctx, cancel := context.WithTimeout(context.Background(), 400*time.Millisecond)
	defer cancel()
	_ = sup.Run(ctx)

	if got := sup.Status().State; got != StateRecovery {
		t.Fatalf("want %q when the runtime cannot start, got %q", StateRecovery, got)
	}
}

// --- image acquisition ---------------------------------------------------

func TestAFreshInstallPullsTheRuntimeImage(t *testing.T) {
	// The bootloader is what fetches the runtime on a new device: install.sh
	// writes the spec and starts the bootloader without pulling anything.
	// Without this the very first boot goes straight to recovery with
	// "No such image", which is what the integration suite caught.
	docker := &fakeDocker{startMakesHealthy: true, imagePresent: false}
	sup := newTestSupervisor(docker, &fakeProbe{})

	if err := sup.Reconcile(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if docker.pullCount() != 1 {
		t.Fatalf("want the image pulled once, got %d pulls", docker.pullCount())
	}
	if got := sup.Status().State; got != StateHealthy {
		t.Fatalf("want %q after pulling and starting, got %q", StateHealthy, got)
	}
}

func TestAPresentImageIsNotRePulled(t *testing.T) {
	// Re-pulling on every restart would turn each one into a network round
	// trip, and on a slow link into minutes of delay before a PLC that was
	// working comes back.
	docker := &fakeDocker{startMakesHealthy: true, imagePresent: true}
	sup := newTestSupervisor(docker, &fakeProbe{})

	if err := sup.Reconcile(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if docker.pullCount() != 0 {
		t.Fatalf("a present image must not be re-pulled, got %d pulls", docker.pullCount())
	}
}

func TestAFailedImagePullIsReportedWithTheImageName(t *testing.T) {
	// An operator seeing "downloading X failed" can check the tag or the
	// network; "reconcile failed" sends them nowhere.
	docker := &fakeDocker{imagePresent: false, pullErr: errors.New("manifest unknown")}
	sup := newTestSupervisor(docker, &fakeProbe{})

	err := sup.Reconcile(context.Background())
	if err == nil {
		t.Fatal("a failed pull must surface an error")
	}
	if !strings.Contains(err.Error(), "test:1") ||
		!strings.Contains(err.Error(), "manifest unknown") {
		t.Fatalf("the error must name the image and the cause, got %v", err)
	}
}

func TestTheDownloadIsVisibleInTheStatusWhileItRuns(t *testing.T) {
	// On a slow device this pull runs for minutes. The difference between
	// "downloading 50%" and an apparently hung device is whether the editor
	// has anything to show, so the reason has to be updated DURING the pull,
	// not merely at the end. Captured from inside the progress callback,
	// because by the time Reconcile returns the state is already healthy.
	docker := &fakeDocker{startMakesHealthy: true, imagePresent: false}
	sup := newTestSupervisor(docker, &fakeProbe{})
	docker.onPullProgress = func() { docker.observed = sup.Status() }

	if err := sup.Reconcile(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	if docker.observed.State != StateStarting {
		t.Fatalf("want %q while downloading, got %q", StateStarting, docker.observed.State)
	}
	if !strings.Contains(docker.observed.Reason, "downloading") {
		t.Fatalf("the reason must say what is happening, got %q", docker.observed.Reason)
	}
	if !strings.Contains(docker.observed.Reason, "50%") {
		t.Fatalf("the reason must carry the percentage, got %q", docker.observed.Reason)
	}
}
