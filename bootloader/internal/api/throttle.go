// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

package api

import (
	"math"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Sign-in throttling parameters, matching webserver/sessions.py.
const (
	// maxConcurrentVerifications caps the PBKDF2 verifications in flight.
	maxConcurrentVerifications = 2
	// throttleFirstDelay is the wait after the first failure on a key; each further failure doubles it.
	throttleFirstDelay = 5 * time.Second
	// throttleMaxDelay caps the wait on a key.
	throttleMaxDelay = 300 * time.Second
	// throttleForget is how long after its last failure a counter starts again and is dropped.
	throttleForget = 3600 * time.Second
)

// failureCounter holds the consecutive failed sign-ins of one key.
type failureCounter struct {
	failures    int
	lastFailure time.Time
	nextAllowed time.Time
}

// loginThrottle caps concurrent verifications and delays sign-ins per username from one client address
// and per client address, so failures from one address never delay another.
type loginThrottle struct {
	slots chan struct{}

	mu       sync.Mutex
	counters map[string]*failureCounter
	now      func() time.Time
}

func newLoginThrottle() *loginThrottle {
	return &loginThrottle{
		slots:    make(chan struct{}, maxConcurrentVerifications),
		counters: make(map[string]*failureCounter),
		now:      time.Now,
	}
}

// throttleDelay is the wait after failures consecutive failures: min(5 s * 2^(failures-1), 300 s).
func throttleDelay(failures int) time.Duration {
	if failures <= 0 {
		return 0
	}
	delay := throttleFirstDelay << min(failures-1, 16)
	return min(delay, throttleMaxDelay)
}

// usernameKey is the counter key of a username, trimmed and lower-cased, signing in from address;
// one key for every address when the address is unknown.
func usernameKey(username, address string) string {
	name := strings.ToLower(strings.TrimSpace(username))
	if address == "" {
		return "user:" + name
	}
	return "user:" + address + "/" + name
}

// throttleKeys are the counters a sign-in by username from address is held to.
func throttleKeys(username, address string) []string {
	keys := []string{usernameKey(username, address)}
	if address != "" {
		keys = append(keys, "addr:"+address)
	}
	return keys
}

// wait reports the whole seconds, rounded up, before an attempt on keys is accepted; 0 when it may go ahead.
func (t *loginThrottle) wait(keys []string) int {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := t.now()
	var longest time.Duration
	for _, key := range keys {
		if counter, ok := t.counters[key]; ok {
			longest = max(longest, counter.nextAllowed.Sub(now))
		}
	}
	if longest <= 0 {
		return 0
	}
	return int(math.Ceil(longest.Seconds()))
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

// recordFailure counts a failed sign-in on keys, sets their next allowed attempt and drops idle counters.
func (t *loginThrottle) recordFailure(keys []string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := t.now()
	t.forgetIdle(now)
	for _, key := range keys {
		counter, ok := t.counters[key]
		if !ok {
			counter = &failureCounter{}
			t.counters[key] = counter
		}
		counter.failures++
		counter.lastFailure = now
		counter.nextAllowed = now.Add(throttleDelay(counter.failures))
	}
}

// recordSuccess clears the counter of username from address; the address counter is kept.
func (t *loginThrottle) recordSuccess(username, address string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.counters, usernameKey(username, address))
}

// forgetIdle drops counters whose last failure is older than throttleForget. Called with t.mu held.
func (t *loginThrottle) forgetIdle(now time.Time) {
	for key, counter := range t.counters {
		if now.Sub(counter.lastFailure) > throttleForget {
			delete(t.counters, key)
		}
	}
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
