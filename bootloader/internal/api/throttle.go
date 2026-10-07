// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

package api

import (
	"net"
	"net/http"
	"sync"
	"time"
)

// Login throttling. Every attempt runs 600k PBKDF2 iterations (timing-safe
// against enumeration), so the endpoint needs both a global concurrency cap
// and per-source backoff to resist brute force and DoS.
const (
	// maxConcurrentVerifications is small on purpose. Two verifications in
	// flight is more than a legitimate operator ever needs, and it leaves the
	// remaining cores to the runtime.
	maxConcurrentVerifications = 2
	// failuresBeforeBackoff allows the ordinary mistyped password without
	// friction.
	failuresBeforeBackoff = 5
	// backoffWindow is how long failures are remembered.
	backoffWindow = 15 * time.Minute
	// backoffDuration is how long a source is refused once it crosses the
	// threshold.
	backoffDuration = 1 * time.Minute
	// maxTrackedSources bounds the map so the tracking cannot itself become
	// the memory exhaustion it exists to prevent.
	maxTrackedSources = 1024
)

type sourceRecord struct {
	failures int
	first    time.Time
	blocked  time.Time
}

// loginThrottle bounds concurrent password verifications and backs off a
// source that keeps failing.
type loginThrottle struct {
	slots chan struct{}

	mu      sync.Mutex
	sources map[string]*sourceRecord
	now     func() time.Time
}

func newLoginThrottle() *loginThrottle {
	return &loginThrottle{
		slots:   make(chan struct{}, maxConcurrentVerifications),
		sources: make(map[string]*sourceRecord),
		now:     time.Now,
	}
}

// blockedFor reports how long the source must wait, zero when it may proceed.
func (t *loginThrottle) blockedFor(source string) time.Duration {
	t.mu.Lock()
	defer t.mu.Unlock()
	record, ok := t.sources[source]
	if !ok {
		return 0
	}
	now := t.now()
	if record.blocked.After(now) {
		return record.blocked.Sub(now)
	}
	// The window has passed with no further failures: forget the source so a
	// mistyped password months ago costs nothing.
	if now.Sub(record.first) > backoffWindow {
		delete(t.sources, source)
	}
	return 0
}

// acquire takes a verification slot, reporting false when none is free.
func (t *loginThrottle) acquire() bool {
	select {
	case t.slots <- struct{}{}:
		return true
	default:
		return false
	}
}

func (t *loginThrottle) release() {
	select {
	case <-t.slots:
	default:
	}
}

// recordFailure counts a rejected attempt and starts a backoff at the
// threshold.
func (t *loginThrottle) recordFailure(source string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := t.now()

	record, ok := t.sources[source]
	if !ok {
		// Evict wholesale rather than tracking an unbounded set. A flood from
		// many addresses is handled by the concurrency cap; this map only has
		// to make repeated guessing from one place expensive.
		if len(t.sources) >= maxTrackedSources {
			t.sources = make(map[string]*sourceRecord)
		}
		t.sources[source] = &sourceRecord{failures: 1, first: now}
		return
	}
	if now.Sub(record.first) > backoffWindow {
		record.failures = 0
		record.first = now
	}
	record.failures++
	if record.failures >= failuresBeforeBackoff {
		record.blocked = now.Add(backoffDuration)
		record.failures = 0
		record.first = now
	}
}

// recordSuccess clears the history for a source that proved it knows a
// password.
func (t *loginThrottle) recordSuccess(source string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.sources, source)
}

// requestSource identifies the caller by remote address only. No proxy
// sits in front, so X-Forwarded-For would be attacker-controlled.
func requestSource(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}
