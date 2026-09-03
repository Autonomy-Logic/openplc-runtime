package supervisor

import (
	"sync"
	"time"
)

// Defaults mirror webserver/runtimemanager.py's MAX_RAPID_CRASHES /
// RAPID_CRASH_WINDOW one layer up. That module already does this for
// plc_main: restart it, count crashes in a window, and stop restarting when
// the fault is clearly not transient. The sidecar applies the same shape to
// the container, so the two layers behave predictably alike and neither
// masks the other's failure.
const (
	DefaultMaxCrashes   = 3
	DefaultCrashWindow  = 5 * time.Minute
	DefaultRestartDelay = 2 * time.Second
	// Restart backoff is capped so a persistent fault does not stretch to an
	// interval where an operator concludes the sidecar has given up quietly.
	// It reaches the crash ceiling well inside the window either way.
	MaxRestartDelay = 30 * time.Second
)

// crashWindow counts unexpected container exits inside a sliding window.
//
// Only UNEXPECTED exits belong here. A runtime that exits because we asked it
// to -- an update handshake, a stop we issued -- is not evidence of a fault,
// and counting those would make the first update look like a crash-loop and
// drop a perfectly healthy device into recovery. Callers gate on
// Supervisor.expectStop rather than filtering by exit code, because a
// deliberate stop and a genuine crash can both exit non-zero.
type crashWindow struct {
	mu     sync.Mutex
	times  []time.Time
	max    int
	window time.Duration
	// now is injectable so tests can drive the clock instead of sleeping
	// through a five-minute window.
	now func() time.Time
}

func newCrashWindow(max int, window time.Duration) *crashWindow {
	return &crashWindow{max: max, window: window, now: time.Now}
}

// record adds a crash and reports whether the window is now full, meaning the
// runtime should be considered bad rather than restarted again.
func (w *crashWindow) record() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	now := w.now()
	w.prune(now)
	w.times = append(w.times, now)
	return len(w.times) >= w.max
}

// count returns the number of crashes currently inside the window.
func (w *crashWindow) count() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.prune(w.now())
	return len(w.times)
}

// reset clears the history. Called once a runtime has been confirmed healthy,
// so an isolated crash weeks apart never accumulates into a false loop.
func (w *crashWindow) reset() {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.times = nil
}

// prune drops entries that have aged out. Caller holds the lock.
func (w *crashWindow) prune(now time.Time) {
	cutoff := now.Add(-w.window)
	kept := w.times[:0]
	for _, t := range w.times {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	w.times = kept
}

// restartDelay backs off as crashes accumulate: a container that died once
// probably hit something transient and should come back immediately, while one
// dying repeatedly should not be hammered. Bounded by MaxRestartDelay.
func restartDelay(base time.Duration, consecutive int) time.Duration {
	if consecutive <= 0 {
		return 0
	}
	if base <= 0 {
		base = DefaultRestartDelay
	}
	delay := base
	for i := 1; i < consecutive; i++ {
		delay *= 4
		if delay >= MaxRestartDelay {
			return MaxRestartDelay
		}
	}
	return delay
}
