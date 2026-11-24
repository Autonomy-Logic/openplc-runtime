// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

package api

import (
	"errors"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/runtimeauth"
)

// fakeClock is a settable time source for the throttle.
type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

func throttleAt(clock *fakeClock) *loginThrottle {
	throttle := newLoginThrottle()
	throttle.now = clock.Now
	return throttle
}

func newClock() *fakeClock {
	return &fakeClock{now: time.Date(2026, 11, 15, 12, 0, 0, 0, time.UTC)}
}

func TestTheDelayDoublesFromFiveSecondsUpToFiveMinutes(t *testing.T) {
	want := map[int]time.Duration{
		0: 0, 1: 5 * time.Second, 2: 10 * time.Second, 3: 20 * time.Second, 4: 40 * time.Second,
		5: 80 * time.Second, 6: 160 * time.Second, 7: 300 * time.Second, 8: 300 * time.Second,
		100: 300 * time.Second,
	}
	for failures, delay := range want {
		if got := throttleDelay(failures); got != delay {
			t.Errorf("throttleDelay(%d) = %s, want %s", failures, got, delay)
		}
	}
}

func TestEachFailureSetsTheNextAllowedAttempt(t *testing.T) {
	clock := newClock()
	throttle := throttleAt(clock)
	keys := throttleKeys("admin", "10.0.0.1")

	if wait := throttle.wait(keys); wait != 0 {
		t.Fatalf("a fresh key waits %d s", wait)
	}
	throttle.recordFailure(keys)
	if wait := throttle.wait(keys); wait != 5 {
		t.Fatalf("after one failure: wait %d s, want 5", wait)
	}
	clock.Advance(4200 * time.Millisecond)
	if wait := throttle.wait(keys); wait != 1 {
		t.Fatalf("0.8 s before the end: wait %d s, want 1 (rounded up)", wait)
	}
	clock.Advance(800 * time.Millisecond)
	if wait := throttle.wait(keys); wait != 0 {
		t.Fatalf("at the end of the delay: wait %d s, want 0", wait)
	}
	for failures, want := range []int{10, 20, 40, 80, 160, 300, 300} {
		throttle.recordFailure(keys)
		if wait := throttle.wait(keys); wait != want {
			t.Fatalf("after %d failures: wait %d s, want %d", failures+2, wait, want)
		}
		clock.Advance(time.Duration(want) * time.Second)
	}
}

func TestTheUsernameKeyIgnoresCaseAndSurroundingSpace(t *testing.T) {
	throttle := throttleAt(newClock())
	throttle.recordFailure(throttleKeys(" Admin ", "10.0.0.1"))
	if wait := throttle.wait(throttleKeys("admin", "10.0.0.2")); wait != 5 {
		t.Fatalf("the same username from another address waits %d s, want 5", wait)
	}
}

func TestTheAddressKeyHoldsAnyUsername(t *testing.T) {
	throttle := throttleAt(newClock())
	throttle.recordFailure(throttleKeys("admin", "10.0.0.1"))
	if wait := throttle.wait(throttleKeys("someone-else", "10.0.0.1")); wait != 5 {
		t.Fatalf("another username from the same address waits %d s, want 5", wait)
	}
	if wait := throttle.wait(throttleKeys("someone-else", "10.0.0.2")); wait != 0 {
		t.Fatalf("an unrelated username and address waits %d s", wait)
	}
}

func TestASuccessClearsTheUsernameCounterButNotTheAddress(t *testing.T) {
	clock := newClock()
	throttle := throttleAt(clock)
	throttle.recordFailure(throttleKeys("admin", "10.0.0.1"))
	throttle.recordFailure(throttleKeys("admin", "10.0.0.1"))
	throttle.recordSuccess(" ADMIN ")

	if wait := throttle.wait(throttleKeys("admin", "10.0.0.2")); wait != 0 {
		t.Fatalf("the username is still held for %d s after a success", wait)
	}
	if wait := throttle.wait(throttleKeys("other", "10.0.0.1")); wait != 10 {
		t.Fatalf("the address waits %d s, want 10: a success must not clear it", wait)
	}
}

func TestACounterIdleForAnHourStartsAgainAndIsDropped(t *testing.T) {
	clock := newClock()
	throttle := throttleAt(clock)
	for i := 0; i < 4; i++ {
		throttle.recordFailure(throttleKeys("admin", "10.0.0.1"))
	}
	for i := 0; i < 100; i++ {
		throttle.recordFailure(throttleKeys("user"+strconv.Itoa(i), "10.0.1."+strconv.Itoa(i)))
	}

	clock.Advance(throttleForget + time.Second)
	throttle.recordFailure(throttleKeys("admin", "10.0.0.1"))
	if wait := throttle.wait(throttleKeys("admin", "10.0.0.1")); wait != 5 {
		t.Fatalf("after an hour idle the delay is %d s, want 5", wait)
	}
	throttle.mu.Lock()
	size := len(throttle.counters)
	throttle.mu.Unlock()
	if size != 2 {
		t.Fatalf("%d counters remain, want only the 2 just recorded", size)
	}
}

func TestACounterWithinTheHourKeepsCounting(t *testing.T) {
	clock := newClock()
	throttle := throttleAt(clock)
	keys := throttleKeys("admin", "10.0.0.1")
	throttle.recordFailure(keys)
	clock.Advance(throttleForget)
	throttle.recordFailure(keys)
	if wait := throttle.wait(keys); wait != 10 {
		t.Fatalf("a second failure exactly an hour later waits %d s, want 10", wait)
	}
}

// --- over HTTP -----------------------------------------------------------

func signIn(t *testing.T, rig *testRig, username, password string) (*http.Response, map[string]any) {
	t.Helper()
	return postJSON(t, rig.srv, "/api/bootloader/login", "",
		`{"username":"`+username+`","password":"`+password+`"}`)
}

func TestAFailedSignInHoldsTheNextAttemptWith429AndRetryAfter(t *testing.T) {
	clock := newClock()
	users := &fakeUsers{count: 1, authErr: runtimeauth.ErrNoSuchUser}
	rig := newRig(t, users, throttleAt(clock))

	resp, _ := signIn(t, rig, "admin", "wrong-secret-1")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("first failure got HTTP %d, want 401", resp.StatusCode)
	}
	resp, body := signIn(t, rig, "admin", "wrong-secret-2")
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("an immediate retry got HTTP %d, want 429", resp.StatusCode)
	}
	if got := resp.Header.Get("Retry-After"); got != "5" {
		t.Fatalf("Retry-After %q, want 5", got)
	}
	if body["error"] == "" {
		t.Error("no explanation was returned")
	}

	clock.Advance(2500 * time.Millisecond)
	resp, _ = signIn(t, rig, "other", "wrong-secret-3")
	if resp.StatusCode != http.StatusTooManyRequests || resp.Header.Get("Retry-After") != "3" {
		t.Fatalf("another username from the same address got HTTP %d, Retry-After %q; want 429, 3",
			resp.StatusCode, resp.Header.Get("Retry-After"))
	}

	// The held attempts were not counted: the second failure waits 10 s, not more.
	clock.Advance(2500 * time.Millisecond)
	resp, _ = signIn(t, rig, "admin", "wrong-secret-4")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("after the delay got HTTP %d, want 401", resp.StatusCode)
	}
	resp, _ = signIn(t, rig, "admin", "wrong-secret-5")
	if got := resp.Header.Get("Retry-After"); resp.StatusCode != http.StatusTooManyRequests || got != "10" {
		t.Fatalf("after the second failure got HTTP %d, Retry-After %q; want 429, 10", resp.StatusCode, got)
	}

	log := rig.log.String()
	if !strings.Contains(log, "failed sign-in") || !strings.Contains(log, "throttled a sign-in") {
		t.Errorf("failed and throttled sign-ins were not logged: %s", log)
	}
	if strings.Contains(log, "wrong-secret") {
		t.Errorf("a password reached the log: %s", log)
	}
}

func TestASuccessfulSignInAfterTheDelayClearsTheUsername(t *testing.T) {
	clock := newClock()
	throttle := throttleAt(clock)
	failing := newRig(t, &fakeUsers{count: 1, authErr: runtimeauth.ErrNoSuchUser}, throttle)
	signIn(t, failing, "admin", "wrong")

	clock.Advance(5 * time.Second)
	succeeding := newRig(t, &fakeUsers{
		count: 1, user: &runtimeauth.User{ID: "1", Username: "admin", Role: runtimeauth.RoleAdmin},
	}, throttle)
	resp, _ := signIn(t, succeeding, "admin", "right")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("sign-in after the delay got HTTP %d, want 200", resp.StatusCode)
	}
	if wait := throttle.wait([]string{usernameKey("admin")}); wait != 0 {
		t.Fatalf("the username is still held for %d s", wait)
	}
}

func TestAnAccountDatabaseErrorIsNotCountedAsAFailedSignIn(t *testing.T) {
	clock := newClock()
	users := &fakeUsers{count: 1, authErr: errors.New("disk I/O error")}
	rig := newRig(t, users, throttleAt(clock))

	resp, _ := signIn(t, rig, "admin", "pw")
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("got HTTP %d, want 503", resp.StatusCode)
	}
	resp, _ = signIn(t, rig, "admin", "pw")
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("the retry got HTTP %d, want 503 and no throttle", resp.StatusCode)
	}
}
