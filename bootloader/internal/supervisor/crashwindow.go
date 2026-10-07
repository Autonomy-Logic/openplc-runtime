// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

package supervisor

import (
	"sync"
	"time"
)

// Defaults mirror the runtime's own MAX_RAPID_CRASHES/RAPID_CRASH_WINDOW
// so container-level and process-level crash accounting behave alike.
const (
	DefaultMaxCrashes   = 3
	DefaultCrashWindow  = 5 * time.Minute
	DefaultRestartDelay = 2 * time.Second
	// Restart backoff is capped so a persistent fault does not stretch to an
	// interval where an operator concludes the bootloader has given up quietly.
	// It reaches the crash ceiling well inside the window either way.
	MaxRestartDelay = 30 * time.Second
)

// crashWindow counts UNEXPECTED container exits inside a sliding
// window. Callers gate on Supervisor.expectStop rather than exit code
// since a deliberate stop and a crash can both exit non-zero.
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
