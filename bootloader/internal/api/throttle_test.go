// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

package api

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
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
	key := throttleKey("10.0.0.1")

	if wait := throttle.wait(key); wait != 0 {
		t.Fatalf("a fresh key waits %d s", wait)
	}
	throttle.recordFailure(key)
	if wait := throttle.wait(key); wait != 5 {
		t.Fatalf("after one failure: wait %d s, want 5", wait)
	}
	clock.Advance(4200 * time.Millisecond)
	if wait := throttle.wait(key); wait != 1 {
		t.Fatalf("0.8 s before the end: wait %d s, want 1 (rounded up)", wait)
	}
	clock.Advance(800 * time.Millisecond)
	if wait := throttle.wait(key); wait != 0 {
		t.Fatalf("at the end of the delay: wait %d s, want 0", wait)
	}
	for failures, want := range []int{10, 20, 40, 80, 160, 300, 300} {
		throttle.recordFailure(key)
		if wait := throttle.wait(key); wait != want {
			t.Fatalf("after %d failures: wait %d s, want %d", failures+2, wait, want)
		}
		clock.Advance(time.Duration(want) * time.Second)
	}
}

func TestTheKeyIsTheClientAddressOnly(t *testing.T) {
	if key := throttleKey("10.0.0.1"); key != "addr:10.0.0.1" {
		t.Fatalf("key of 10.0.0.1: %q", key)
	}
	if key := throttleKey(""); key != "addr:unknown" {
		t.Fatalf("key of an unknown address: %q, want addr:unknown", key)
	}
}

func TestFailuresFromOneAddressNeverDelayAnother(t *testing.T) {
	throttle := throttleAt(newClock())
	throttle.recordFailure(throttleKey("10.0.0.1"))
	if wait := throttle.wait(throttleKey("10.0.0.2")); wait != 0 {
		t.Fatalf("another address waits %d s, want 0", wait)
	}
	if wait := throttle.wait(throttleKey("")); wait != 0 {
		t.Fatalf("an unknown address waits %d s, want 0", wait)
	}
}

func TestUnknownAddressesShareOneCounter(t *testing.T) {
	throttle := throttleAt(newClock())
	throttle.recordFailure(throttleKey(""))
	if wait := throttle.wait(throttleKey("")); wait != 5 {
		t.Fatalf("an unknown address waits %d s, want 5", wait)
	}
	if wait := throttle.wait(throttleKey("10.0.0.1")); wait != 0 {
		t.Fatalf("a known address waits %d s, want 0", wait)
	}
}

func TestACounterIdleForAnHourStartsAgainAndIsDropped(t *testing.T) {
	clock := newClock()
	throttle := throttleAt(clock)
	for i := 0; i < 4; i++ {
		throttle.recordFailure(throttleKey("10.0.0.1"))
	}
	for i := 0; i < 100; i++ {
		throttle.recordFailure(throttleKey("10.0.1." + strconv.Itoa(i)))
	}

	clock.Advance(throttleForget + time.Second)
	throttle.recordFailure(throttleKey("10.0.0.1"))
	if wait := throttle.wait(throttleKey("10.0.0.1")); wait != 5 {
		t.Fatalf("after an hour idle the delay is %d s, want 5", wait)
	}
	throttle.mu.Lock()
	size := len(throttle.counters)
	throttle.mu.Unlock()
	if size != 1 {
		t.Fatalf("%d counters remain, want only the one just recorded", size)
	}
}

func TestACounterWithinTheHourKeepsCounting(t *testing.T) {
	clock := newClock()
	throttle := throttleAt(clock)
	key := throttleKey("10.0.0.1")
	throttle.recordFailure(key)
	clock.Advance(throttleForget)
	throttle.recordFailure(key)
	if wait := throttle.wait(key); wait != 10 {
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

func TestASuccessfulSignInKeepsTheAddressCounter(t *testing.T) {
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

	signIn(t, failing, "admin", "wrong")
	if wait := throttle.wait(throttleKey("127.0.0.1")); wait != 10 {
		t.Fatalf("the failure after a success waits %d s, want 10: a success must not clear the address", wait)
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

// passwordUsers accepts one password for admin and refuses every other sign-in.
type passwordUsers struct {
	fakeUsers
}

func (p *passwordUsers) Authenticate(_ context.Context, username, password, _ string) (*runtimeauth.User, error) {
	if username == "admin" && password == "right" {
		return &runtimeauth.User{ID: "1", Username: "admin", Role: runtimeauth.RoleAdmin}, nil
	}
	return nil, runtimeauth.ErrNoSuchUser
}

func TestFailuresForManyUsernamesFromOneAddressAreThrottledTogether(t *testing.T) {
	clock := newClock()
	rig := newRig(t, &fakeUsers{count: 1, authErr: runtimeauth.ErrNoSuchUser}, throttleAt(clock))

	for i, want := range []int{5, 10, 20, 40} {
		username := "user" + strconv.Itoa(i)
		if resp, _ := signIn(t, rig, username, "wrong"); resp.StatusCode != http.StatusUnauthorized {
			t.Fatalf("failure %d (%s) got HTTP %d, want 401", i+1, username, resp.StatusCode)
		}
		resp, _ := signIn(t, rig, "next-"+username, "wrong")
		if resp.StatusCode != http.StatusTooManyRequests || resp.Header.Get("Retry-After") != strconv.Itoa(want) {
			t.Fatalf("after %d failures, each for another username, got HTTP %d, Retry-After %q; want 429, %d",
				i+1, resp.StatusCode, resp.Header.Get("Retry-After"), want)
		}
		clock.Advance(time.Duration(want) * time.Second)
	}
}

func TestTheSameUsernameFromTwoAddressesIsThrottledIndependently(t *testing.T) {
	throttle := throttleAt(newClock())
	server := &Server{cfg: Config{
		Version:        "bootloader-v1.0.0-test",
		RuntimeVersion: func() string { return "v4.2.1" },
		Users:          &passwordUsers{fakeUsers{count: 1}},
		Supervisor:     healthySupervisor(),
		Logs:           &fakeLogs{out: "log"},
		Updater:        &fakeUpdater{},
		SelfUpdater:    &fakeSelfUpdater{},
		Log:            slog.New(slog.NewTextHandler(io.Discard, nil)),
	}}
	server.throttleOnce.Do(func() { server.throttleImpl = throttle })
	mux := http.NewServeMux()
	server.routes(mux)
	signInFrom := func(address, password string) int {
		req := httptest.NewRequest(http.MethodPost, "/api/bootloader/login",
			strings.NewReader(`{"username":"admin","password":"`+password+`"}`))
		req.Header.Set("Content-Type", "application/json")
		req.RemoteAddr = address + ":40000"
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec.Code
	}

	if code := signInFrom("10.0.0.1", "wrong"); code != http.StatusUnauthorized {
		t.Fatalf("a failure from 10.0.0.1 got HTTP %d, want 401", code)
	}
	if code := signInFrom("10.0.0.2", "right"); code != http.StatusOK {
		t.Fatalf("admin from 10.0.0.2 got HTTP %d after a failure from 10.0.0.1, want 200", code)
	}
	if code := signInFrom("10.0.0.2", "wrong"); code != http.StatusUnauthorized {
		t.Fatalf("a failure for admin from 10.0.0.2 got HTTP %d, want 401", code)
	}
	for _, address := range []string{"10.0.0.1", "10.0.0.2"} {
		if wait := throttle.wait(throttleKey(address)); wait != 5 {
			t.Fatalf("%s waits %d s, want 5: only its own failure counts", address, wait)
		}
	}
}

func TestAnAttackerFailingOncePerExpiryNeverDelaysAnotherAddress(t *testing.T) {
	clock := newClock()
	server := &Server{cfg: Config{
		Version:        "bootloader-v1.0.0-test",
		RuntimeVersion: func() string { return "v4.2.1" },
		Users:          &passwordUsers{fakeUsers{count: 1}},
		Supervisor:     healthySupervisor(),
		Logs:           &fakeLogs{out: "log"},
		Updater:        &fakeUpdater{},
		SelfUpdater:    &fakeSelfUpdater{},
		Log:            slog.New(slog.NewTextHandler(io.Discard, nil)),
	}}
	server.throttleOnce.Do(func() { server.throttleImpl = throttleAt(clock) })
	mux := http.NewServeMux()
	server.routes(mux)
	signInFrom := func(address, password string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/api/bootloader/login",
			strings.NewReader(`{"username":"admin","password":"`+password+`"}`))
		req.Header.Set("Content-Type", "application/json")
		req.RemoteAddr = address + ":40000"
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec
	}

	end := clock.Now().Add(2 * time.Hour)
	failures, wait := 0, 0
	for clock.Now().Before(end) {
		if rec := signInFrom("10.6.6.6", "wrong"); rec.Code != http.StatusUnauthorized {
			t.Fatalf("attacker failure %d got HTTP %d, want 401", failures+1, rec.Code)
		}
		failures++
		early := signInFrom("10.6.6.6", "right")
		if early.Code != http.StatusTooManyRequests {
			t.Fatalf("attacker retry got HTTP %d, want 429", early.Code)
		}
		if rec := signInFrom("10.0.0.5", "right"); rec.Code != http.StatusOK {
			t.Fatalf("after %d attacker failures the operator got HTTP %d, want 200", failures, rec.Code)
		}
		var err error
		if wait, err = strconv.Atoi(early.Header().Get("Retry-After")); err != nil || wait <= 0 {
			t.Fatalf("Retry-After %q", early.Header().Get("Retry-After"))
		}
		clock.Advance(time.Duration(wait) * time.Second)
	}
	if failures < 20 || wait != int(throttleMaxDelay.Seconds()) {
		t.Fatalf("%d attacker failures, last wait %d s; want at least 20 and %d s", failures, wait,
			int(throttleMaxDelay.Seconds()))
	}
}
