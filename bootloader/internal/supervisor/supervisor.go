// Package supervisor owns the runtime container's lifecycle.
//
// This is the part of the bootloader that decides what the runtime container
// should be doing: at boot it reconciles that container into existence, then
// sits blocked on the Docker events stream and does nothing until something
// happens. When the runtime dies it restarts it, and when it dies repeatedly
// it stops trying and enters recovery, so an operator can reach the device
// from the editor instead of the bootloader hammering a runtime that will
// never come up.
//
// Two boundaries are deliberate and easy to get wrong:
//
//   - Health means the runtime WEBSERVER came up. Whether plc_main is running,
//     whether a program is loaded, and whether that program errors are all the
//     webserver's concern -- it already restarts plc_main and drops to safe
//     mode on rapid crashes. If the bootloader looked at PLC state, a user
//     uploading broken logic would trigger a runtime recovery, which would be
//     a spectacular way to turn a program bug into a device outage.
//
//   - There is no automatic rollback. A failed update or a crash-loop stops
//     and waits for a human. Choosing a version is a decision with physical
//     consequences, and guessing wrong twice is worse than stopping once.
package supervisor

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/dockerapi"
)

// State is the supervisor's externally visible condition, reported by the
// status endpoint and used by the editor to decide what to show.
type State string

const (
	// StateBooting is the brief window before the first reconcile finishes.
	StateBooting State = "booting"
	// StateStarting means a container is up but has not yet been confirmed
	// healthy.
	StateStarting State = "starting"
	// StateHealthy is steady state: the runtime webserver is answering and the
	// supervisor is idle on the events stream.
	StateHealthy State = "healthy"
	// StateUpdating means a version change is in progress. Exits during this
	// state are expected and never count as crashes.
	StateUpdating State = "updating"
	// StateRecovery means the supervisor has stopped trying. The runtime
	// container is stopped, UDP discovery answers with a recovery flag, and
	// only recovery commands are accepted.
	StateRecovery State = "recovery"
)

// Reason explains a recovery state to an operator. Kept as prose rather than a
// code because it is displayed verbatim in the editor.
type Status struct {
	State        State     `json:"state"`
	Reason       string    `json:"reason,omitempty"`
	Version      string    `json:"version,omitempty"`
	Image        string    `json:"image,omitempty"`
	CrashCount   int       `json:"crashCount"`
	Since        time.Time `json:"since"`
	ContainerID  string    `json:"containerId,omitempty"`
	HealthSource string    `json:"healthSource,omitempty"`
}

// DockerClient is the slice of the Docker API the supervisor uses.
//
// An interface rather than *dockerapi.Client so the state machine can be
// tested directly. The subtle logic here is crash accounting -- distinguishing
// an exit we asked for from one we did not -- and that is exactly the kind of
// thing that is wrong in a way no integration test notices until a device
// drops into recovery during its first successful update.
type DockerClient interface {
	Ping(ctx context.Context) error
	InspectContainer(ctx context.Context, name string) (*dockerapi.ContainerInspect, error)
	CreateContainer(ctx context.Context, name string, spec any) (*dockerapi.CreateContainerResponse, error)
	StartContainer(ctx context.Context, name string) error
	StopContainer(ctx context.Context, name string, grace time.Duration) error
	RemoveContainer(ctx context.Context, name string, force bool) error
	StreamEvents(ctx context.Context, name string, handle func(dockerapi.Event)) error
	// Image access, so the supervisor can fetch a runtime it has been told to
	// run but does not have. Needed on a fresh install -- install.sh writes
	// the spec and starts the bootloader without pulling anything -- and
	// after a data wipe or an operator editing the spec by hand.
	InspectImage(ctx context.Context, ref string) (*dockerapi.ImageInfo, error)
	PullImage(ctx context.Context, ref string, onProgress func(dockerapi.PullProgress)) error
}

// SpecProvider supplies the container definition to create the runtime from.
// An interface so the supervisor never has to know how the spec was assembled
// or where the board-specific mounts came from.
type SpecProvider interface {
	// ContainerSpec returns a Docker create payload for the given image ref.
	ContainerSpec(imageRef string) any
	// ImageRef returns the image the runtime should be running right now.
	ImageRef() string
}

// HealthProber reports whether the runtime webserver is answering. Separate
// from the container's own healthcheck so the supervisor still has an opinion
// on images built before the HEALTHCHECK landed.
type HealthProber interface {
	Probe(ctx context.Context) error
}

// Config tunes the supervisor. Zero values fall back to the package defaults.
type Config struct {
	ContainerName string
	MaxCrashes    int
	CrashWindow   time.Duration
	// StartTimeout bounds how long a freshly started container has to report
	// healthy before the attempt is treated as a failure.
	StartTimeout time.Duration
	// StopGrace is handed to Docker's stop. The runtime flushes retained
	// variables on SIGTERM, so this must not be stingy.
	StopGrace time.Duration
	// RestartDelayBase is the first backoff step after an unexpected exit.
	// Configurable so tests do not sleep through real backoff.
	RestartDelayBase time.Duration
}

const (
	DefaultContainerName = "openplc-runtime"
	DefaultStartTimeout  = 90 * time.Second
	DefaultStopGrace     = 30 * time.Second
)

func (c *Config) withDefaults() Config {
	out := *c
	if out.ContainerName == "" {
		out.ContainerName = DefaultContainerName
	}
	if out.MaxCrashes <= 0 {
		out.MaxCrashes = DefaultMaxCrashes
	}
	if out.CrashWindow <= 0 {
		out.CrashWindow = DefaultCrashWindow
	}
	if out.StartTimeout <= 0 {
		out.StartTimeout = DefaultStartTimeout
	}
	if out.StopGrace <= 0 {
		out.StopGrace = DefaultStopGrace
	}
	if out.RestartDelayBase <= 0 {
		out.RestartDelayBase = DefaultRestartDelay
	}
	return out
}

// Supervisor reconciles and watches one runtime container.
type Supervisor struct {
	docker DockerClient
	spec   SpecProvider
	health HealthProber
	cfg    Config
	log    *slog.Logger

	crashes *crashWindow

	mu sync.Mutex
	// status is the current externally visible condition.
	status Status
	// expectStop suppresses crash accounting while we are deliberately taking
	// the container down. Counted rather than boolean: an update stops the
	// container and a concurrent reconcile must not clear the suppression
	// early, which would make our own stop look like a crash.
	expectStop int
	// consecutiveFailures drives restart backoff, reset by a healthy start.
	consecutiveFailures int
	// onRecovery is invoked when the supervisor enters recovery, so the UDP
	// discovery responder can be switched on without this package importing it.
	onRecovery func(Status)
	// onHealthy is the mirror, used to switch discovery back off.
	onHealthy func(Status)
}

// New builds a supervisor. Nothing is started until Run.
func New(
	docker DockerClient,
	spec SpecProvider,
	health HealthProber,
	cfg Config,
	log *slog.Logger,
) *Supervisor {
	resolved := cfg.withDefaults()
	return &Supervisor{
		docker:  docker,
		spec:    spec,
		health:  health,
		cfg:     resolved,
		log:     log,
		crashes: newCrashWindow(resolved.MaxCrashes, resolved.CrashWindow),
		status:  Status{State: StateBooting, Since: time.Now()},
	}
}

// OnRecovery and OnHealthy register transition hooks. Set before Run.
func (s *Supervisor) OnRecovery(fn func(Status)) { s.onRecovery = fn }
func (s *Supervisor) OnHealthy(fn func(Status))  { s.onHealthy = fn }

// Status returns a snapshot of the current condition.
func (s *Supervisor) Status() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	status := s.status
	status.CrashCount = s.crashes.count()
	return status
}

// setState records a transition and fires the matching hook. Hooks run outside
// the lock: they touch the discovery responder, and holding the supervisor
// lock across that would invite a deadlock the moment either side grows.
func (s *Supervisor) setState(state State, reason string) {
	s.mu.Lock()
	if s.status.State == state && s.status.Reason == reason {
		s.mu.Unlock()
		return
	}
	previous := s.status.State
	s.status.State = state
	s.status.Reason = reason
	s.status.Since = time.Now()
	snapshot := s.status
	s.mu.Unlock()

	s.log.Info("state change", "from", previous, "to", state, "reason", reason)

	switch state {
	case StateRecovery:
		if s.onRecovery != nil {
			s.onRecovery(snapshot)
		}
	case StateHealthy:
		if s.onHealthy != nil {
			s.onHealthy(snapshot)
		}
	}
}

// Run reconciles the runtime container, then watches it until ctx is
// cancelled. It returns only on cancellation: a broken events stream is
// reconnected, because losing the watch is not a reason to stop supervising.
func (s *Supervisor) Run(ctx context.Context) error {
	if err := s.docker.Ping(ctx); err != nil {
		// Without the socket the bootloader cannot do its job at all, and saying
		// so plainly beats failing later inside a container create.
		return fmt.Errorf("docker socket unreachable at start-up: %w", err)
	}

	if err := s.Reconcile(ctx); err != nil {
		// A failed reconcile is not fatal to the process: recovery mode exists
		// precisely so an operator can reach a device whose runtime will not
		// come up. Log it, enter recovery, and keep serving.
		s.log.Error("initial reconcile failed", "error", err)
		s.enterRecovery(ctx, fmt.Sprintf("runtime could not be started: %v", err))
	}

	return s.watch(ctx)
}

// watch consumes the events stream, reconnecting on failure.
//
// Every reconnect re-reconciles. The stream can only report what happened
// while it was open, so a gap -- most often the daemon restarting -- may hide
// a container exit. Re-inspecting is the only way to be sure the world still
// matches what we believe.
func (s *Supervisor) watch(ctx context.Context) error {
	const reconnectDelay = 2 * time.Second
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		err := s.docker.StreamEvents(ctx, s.cfg.ContainerName, func(event dockerapi.Event) {
			s.handleEvent(ctx, event)
		})
		if ctx.Err() != nil {
			return ctx.Err()
		}
		s.log.Warn("events stream ended, reconnecting", "error", err)

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(reconnectDelay):
		}

		// Re-sync before trusting the new stream.
		if err := s.Reconcile(ctx); err != nil {
			s.log.Error("reconcile after events reconnect failed", "error", err)
		}
	}
}

// handleEvent reacts to one container event.
func (s *Supervisor) handleEvent(ctx context.Context, event dockerapi.Event) {
	if event.ContainerName() != s.cfg.ContainerName {
		return
	}

	switch {
	case event.Action == "die":
		code, _ := event.ExitCode()
		s.handleDeath(ctx, code)

	case event.HealthStatus() == "unhealthy":
		// The container is alive but the webserver stopped answering. Without
		// this the supervisor would idle forever beside a wedged runtime: a
		// hung process never emits a die event.
		s.log.Warn("runtime reported unhealthy")
		s.handleWedged(ctx)

	case event.HealthStatus() == "healthy":
		s.markHealthy("docker healthcheck")
	}
}

// handleDeath restarts the runtime, or gives up if it keeps dying.
func (s *Supervisor) handleDeath(ctx context.Context, exitCode int) {
	if s.consumeExpectedStop() {
		s.log.Info("runtime stopped as expected", "exitCode", exitCode)
		return
	}

	s.mu.Lock()
	state := s.status.State
	s.mu.Unlock()
	if state == StateRecovery || state == StateUpdating {
		// Already handled by whoever put us here.
		return
	}

	looping := s.crashes.record()
	s.mu.Lock()
	s.consecutiveFailures++
	failures := s.consecutiveFailures
	s.mu.Unlock()

	s.log.Warn("runtime exited unexpectedly",
		"exitCode", exitCode, "crashesInWindow", s.crashes.count())

	if looping {
		s.enterRecovery(ctx, fmt.Sprintf(
			"runtime exited %d times within %s (last exit code %d); "+
				"not restarting again",
			s.cfg.MaxCrashes, s.cfg.CrashWindow, exitCode))
		return
	}

	delay := restartDelay(s.cfg.RestartDelayBase, failures)
	s.setState(StateStarting, "restarting after unexpected exit")
	select {
	case <-ctx.Done():
		return
	case <-time.After(delay):
	}

	if err := s.startAndConfirm(ctx); err != nil {
		s.log.Error("restart failed", "error", err)
		// Do not enter recovery here: the crash window is the authority on
		// when to give up, and a single failed restart is not it. The next die
		// event advances the count.
	}
}

// handleWedged deals with a container that is running but not answering.
// Stopping it converts an invisible hang into a die event, which then flows
// through the ordinary crash-loop accounting rather than needing a parallel
// code path with its own thresholds.
func (s *Supervisor) handleWedged(ctx context.Context) {
	s.mu.Lock()
	state := s.status.State
	s.mu.Unlock()
	if state == StateRecovery || state == StateUpdating {
		return
	}
	if err := s.docker.StopContainer(ctx, s.cfg.ContainerName, s.cfg.StopGrace); err != nil {
		s.log.Error("stopping wedged runtime failed", "error", err)
	}
}

// Reconcile brings the runtime container to the desired state and is safe to
// call at any time.
//
// Adoption is the important property: a running healthy container is left
// exactly as it is. The bootloader restarts (its own crash, a self-update) far
// more often than the runtime does, and a reconcile that recreated or bounced
// a working runtime would turn a bootloader hiccup into a plant outage.
func (s *Supervisor) Reconcile(ctx context.Context) error {
	inspect, err := s.docker.InspectContainer(ctx, s.cfg.ContainerName)
	switch {
	case err == nil:
		// Exists. Adopt, or start it if it is down.
	case dockerapi.IsNotFound(err):
		s.log.Info("runtime container absent, creating", "name", s.cfg.ContainerName)
		if err := s.create(ctx); err != nil {
			return err
		}
		return s.startAndConfirm(ctx)
	default:
		return fmt.Errorf("inspecting %s: %w", s.cfg.ContainerName, err)
	}

	s.mu.Lock()
	s.status.ContainerID = inspect.ID
	s.status.Image = inspect.Config.Image
	s.mu.Unlock()

	// Does the existing container actually match what the spec now asks for?
	//
	// This is what makes Reconcile reconcile rather than merely "start
	// whatever is there". A container is created from an image reference and
	// keeps it for life, so after a version change the existing one is the OLD
	// version -- and the branch below would have happily restarted it and
	// reported success. The update then appeared to work while the device kept
	// running the version it started with, which is precisely the bug the
	// integration suite caught.
	//
	// It also covers an operator editing the spec by hand (a board mount, an
	// env var) and restarting the bootloader: the container is rebuilt from
	// the spec instead of silently keeping the old configuration.
	desired := s.spec.ImageRef()
	if inspect.Config.Image != desired {
		s.log.Info("runtime container is on a different image, recreating",
			"running", inspect.Config.Image, "desired", desired)
		if err := s.create(ctx); err != nil {
			return err
		}
		return s.startAndConfirm(ctx)
	}

	if !inspect.State.Running {
		s.log.Info("runtime container present but not running",
			"status", inspect.State.Status, "exitCode", inspect.State.ExitCode)
		return s.startAndConfirm(ctx)
	}

	// Running. Trust Docker's healthcheck when the image declares one;
	// otherwise probe the webserver ourselves so an image built before the
	// HEALTHCHECK landed is still supervised rather than assumed fine.
	switch inspect.HealthStatus() {
	case "healthy":
		s.markHealthy("docker healthcheck")
		return nil
	case "unhealthy":
		s.handleWedged(ctx)
		return nil
	case "starting":
		s.setState(StateStarting, "waiting for healthcheck")
		return s.awaitHealthy(ctx)
	default:
		return s.confirmByProbe(ctx)
	}
}

// create makes the container from the current spec. A stale container under
// the same name is removed first: create fails with a name conflict otherwise,
// and by the time we are creating we have already decided the existing one is
// not usable.
func (s *Supervisor) create(ctx context.Context) error {
	imageRef := s.spec.ImageRef()
	if err := s.ensureImage(ctx, imageRef); err != nil {
		return err
	}
	if err := s.docker.RemoveContainer(ctx, s.cfg.ContainerName, true); err != nil {
		return fmt.Errorf("removing stale container %s: %w", s.cfg.ContainerName, err)
	}
	created, err := s.docker.CreateContainer(ctx, s.cfg.ContainerName, s.spec.ContainerSpec(imageRef))
	if err != nil {
		return fmt.Errorf("creating %s from %s: %w", s.cfg.ContainerName, imageRef, err)
	}
	for _, warning := range created.Warnings {
		s.log.Warn("docker create warning", "warning", warning)
	}
	s.mu.Lock()
	s.status.ContainerID = created.ID
	s.status.Image = imageRef
	s.mu.Unlock()
	return nil
}

// ensureImage pulls imageRef when it is not already present.
//
// The bootloader is what fetches the runtime on a fresh device: install.sh
// writes the spec and starts the bootloader without pulling anything, so
// without this a brand-new install would go straight to recovery with "No
// such image". It also covers a spec that names a version whose image was
// retired, or one an operator edited by hand.
//
// Only pulls when the image is absent. A present image is never re-pulled --
// that would turn every restart into a network round trip, and on a slow link
// into minutes of delay before a PLC that was working comes back.
func (s *Supervisor) ensureImage(ctx context.Context, imageRef string) error {
	if _, err := s.docker.InspectImage(ctx, imageRef); err == nil {
		return nil
	} else if !dockerapi.IsNotFound(err) {
		return fmt.Errorf("checking for image %s: %w", imageRef, err)
	}

	s.log.Info("runtime image not present locally, pulling", "image", imageRef)
	// The reason is surfaced so the editor shows "downloading" rather than a
	// silent wait: on a slow device this pull can run for many minutes.
	s.setState(StateStarting, fmt.Sprintf("downloading %s", imageRef))

	err := s.docker.PullImage(ctx, imageRef, func(p dockerapi.PullProgress) {
		if p.Percent == nil {
			return
		}
		s.setState(StateStarting,
			fmt.Sprintf("downloading %s (%d%%)", imageRef, *p.Percent))
	})
	if err != nil {
		return fmt.Errorf("downloading %s: %w", imageRef, err)
	}
	s.log.Info("runtime image pulled", "image", imageRef)
	return nil
}

// startAndConfirm starts the container and waits for it to report healthy.
func (s *Supervisor) startAndConfirm(ctx context.Context) error {
	s.setState(StateStarting, "starting runtime")
	if err := s.docker.StartContainer(ctx, s.cfg.ContainerName); err != nil && !dockerapi.IsConflict(err) {
		return fmt.Errorf("starting %s: %w", s.cfg.ContainerName, err)
	}
	return s.awaitHealthy(ctx)
}

// awaitHealthy polls until the runtime is healthy or StartTimeout elapses.
//
// Polling, not events: a container that never becomes healthy emits no event
// to wait for, so a timeout is the only way to notice. The poll is on the
// bootloader's own clock and touches nothing in the scan path.
func (s *Supervisor) awaitHealthy(ctx context.Context) error {
	deadline := time.Now().Add(s.cfg.StartTimeout)
	const pollInterval = 2 * time.Second

	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		inspect, err := s.docker.InspectContainer(ctx, s.cfg.ContainerName)
		if err != nil {
			if dockerapi.IsNotFound(err) {
				return fmt.Errorf("container %s vanished while starting", s.cfg.ContainerName)
			}
			return fmt.Errorf("inspecting %s while starting: %w", s.cfg.ContainerName, err)
		}

		if !inspect.State.Running {
			// Exited during start-up. The die event drives crash accounting;
			// reporting the exit code here is what makes the failure legible.
			return fmt.Errorf("container %s exited during start-up with code %d",
				s.cfg.ContainerName, inspect.State.ExitCode)
		}

		switch inspect.HealthStatus() {
		case "healthy":
			s.markHealthy("docker healthcheck")
			return nil
		case "unhealthy":
			return fmt.Errorf("container %s reported unhealthy during start-up",
				s.cfg.ContainerName)
		case "":
			// No healthcheck in this image: fall back to our own probe.
			if err := s.health.Probe(ctx); err == nil {
				s.markHealthy("api probe")
				return nil
			}
		}

		if time.Now().After(deadline) {
			return fmt.Errorf("container %s did not become healthy within %s",
				s.cfg.ContainerName, s.cfg.StartTimeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(pollInterval):
		}
	}
}

// confirmByProbe validates an already-running container that declares no
// healthcheck.
func (s *Supervisor) confirmByProbe(ctx context.Context) error {
	if err := s.health.Probe(ctx); err != nil {
		s.setState(StateStarting, "runtime running but not yet answering")
		return s.awaitHealthy(ctx)
	}
	s.markHealthy("api probe")
	return nil
}

// markHealthy records steady state and clears the restart backoff.
//
// It deliberately does NOT clear the crash window. A crash-loop is a runtime
// that dies, comes back up fine, and dies again -- which is the common shape,
// because a program that faults on load lets the webserver start before it
// takes the process down. Resetting the count on every healthy start would
// zero the evidence between each crash, so the loop could never reach the
// threshold and the supervisor would restart forever instead of handing the
// device to an operator. The window forgets by aging entries out, which is all
// the forgetting that is wanted: crashes weeks apart never accumulate.
func (s *Supervisor) markHealthy(source string) {
	s.mu.Lock()
	s.consecutiveFailures = 0
	s.status.HealthSource = source
	s.mu.Unlock()
	s.setState(StateHealthy, "")
}

// enterRecovery stops the runtime and switches to recovery mode.
//
// Stopping first is what makes UDP discovery exclusive: only one service on
// the host may answer the broadcast, and recovery is defined as "the runtime
// is not running", so the responder can be switched on without ever racing
// the runtime's own.
func (s *Supervisor) enterRecovery(ctx context.Context, reason string) {
	s.markExpectedStop()
	if err := s.docker.StopContainer(ctx, s.cfg.ContainerName, s.cfg.StopGrace); err != nil {
		// Log and continue: recovery must be reachable even if the stop
		// failed, and a container we could not stop is all the more reason to
		// let an operator in.
		s.log.Error("stopping runtime for recovery failed", "error", err)
		s.consumeExpectedStop()
	}
	s.setState(StateRecovery, reason)
}

// EnterRecovery is the exported entry point for other packages (the update
// executor) to hand control to an operator after a failure.
func (s *Supervisor) EnterRecovery(ctx context.Context, reason string) {
	s.enterRecovery(ctx, reason)
}

// BeginUpdate claims the supervisor for a version change, suppressing crash
// accounting for the stop that is about to happen. It returns an error when an
// update is already running: two concurrent swaps of the same container is not
// a situation worth trying to make safe.
func (s *Supervisor) BeginUpdate() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.status.State == StateUpdating {
		return errors.New("an update is already in progress")
	}
	s.status.State = StateUpdating
	s.status.Reason = "version change in progress"
	s.status.Since = time.Now()
	s.expectStop++
	return nil
}

// EndUpdate releases the claim taken by BeginUpdate without asserting an
// outcome; the caller decides whether to reconcile or enter recovery.
func (s *Supervisor) EndUpdate() {
	s.mu.Lock()
	if s.expectStop > 0 {
		s.expectStop--
	}
	s.mu.Unlock()
}

// markExpectedStop suppresses crash accounting for one upcoming exit.
func (s *Supervisor) markExpectedStop() {
	s.mu.Lock()
	s.expectStop++
	s.mu.Unlock()
}

// consumeExpectedStop reports whether the exit we just saw was one we asked
// for, decrementing the suppression if so.
func (s *Supervisor) consumeExpectedStop() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.expectStop > 0 {
		s.expectStop--
		return true
	}
	return false
}

// Stop takes the runtime down deliberately, without it counting as a crash.
func (s *Supervisor) Stop(ctx context.Context) error {
	s.markExpectedStop()
	if err := s.docker.StopContainer(ctx, s.cfg.ContainerName, s.cfg.StopGrace); err != nil {
		s.consumeExpectedStop()
		return err
	}
	return nil
}

// ContainerName is what this supervisor manages.
func (s *Supervisor) ContainerName() string { return s.cfg.ContainerName }
