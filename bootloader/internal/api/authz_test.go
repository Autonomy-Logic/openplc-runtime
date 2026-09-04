package api

import (
	"net"
	"net/http"
	"strconv"
	"testing"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/runtimeauth"
)

// The routes that can change what this device runs are admin-only.
//
// The runtime treats `user` as a restricted role, but the bootloader checked
// only the signature: any runtime account could change the runtime version or
// self-update the bootloader -- and a self-update starts a container with the
// Docker socket bound, which is host root.
func TestARestrictedAccountCannotChangeWhatTheDeviceRuns(t *testing.T) {
	cases := []struct {
		name, method, path, body string
	}{
		{"restart", http.MethodPost, "/api/bootloader/restart", "{}"},
		{"update", http.MethodPost, "/api/bootloader/update", `{"version":"v4.2.1"}`},
		{"self-update", http.MethodPost, "/api/bootloader/self-update", `{"version":"bootloader-v1.0.1"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			users := &fakeUsers{count: 1, role: "user"}
			srv := newTestServer(t, users, healthySupervisor(), &fakeLogs{})

			resp, body := postJSON(t, srv, tc.path, validToken(t), tc.body)
			if resp.StatusCode != http.StatusForbidden {
				t.Fatalf("a `user` account got HTTP %d on %s, want 403", resp.StatusCode, tc.path)
			}
			if body["error"] == "" {
				t.Error("no explanation was returned")
			}
		})
	}
}

func TestAnAdminCanStillChangeWhatTheDeviceRuns(t *testing.T) {
	users := &fakeUsers{count: 1, role: RoleAdmin}
	srv := newTestServer(t, users, healthySupervisor(), &fakeLogs{})

	resp, _ := postJSON(t, srv, "/api/bootloader/restart", validToken(t), "{}")
	if resp.StatusCode == http.StatusForbidden {
		t.Fatal("an admin was refused")
	}
}

// A token outliving its account must not keep its privileges.
func TestATokenWhoseAccountIsGoneIsRefused(t *testing.T) {
	users := &fakeUsers{count: 1, roleErr: runtimeauth.ErrNoSuchUser}
	srv := newTestServer(t, users, healthySupervisor(), &fakeLogs{})

	resp, _ := postJSON(t, srv, "/api/bootloader/update", validToken(t), `{"version":"v4.2.1"}`)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("got HTTP %d, want 403 when the role cannot be confirmed", resp.StatusCode)
	}
}

// Reads stay open to any account: seeing why a device is unhealthy is not a
// privileged act, and locking it down would make a broken device harder to
// diagnose for no security gain.
func TestARestrictedAccountCanStillReadStatus(t *testing.T) {
	users := &fakeUsers{count: 1, role: "user"}
	srv := newTestServer(t, users, healthySupervisor(), &fakeLogs{})

	resp, _ := get(t, srv, "/api/bootloader/status", validToken(t))
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("a `user` account got HTTP %d reading status, want 200", resp.StatusCode)
	}
}

// --- login throttling ----------------------------------------------------

func TestRepeatedFailuresFromOneSourceAreBackedOff(t *testing.T) {
	// The endpoint runs a 600k-iteration PBKDF2 per attempt, by design, on a
	// host-network service beside a PLC with real-time deadlines. Unbounded,
	// that is both a brute-force path and a cheap denial of service.
	users := &fakeUsers{count: 1, authErr: runtimeauth.ErrNoSuchUser}
	srv := newTestServer(t, users, healthySupervisor(), &fakeLogs{})

	var last int
	for attempt := 0; attempt < failuresBeforeBackoff+1; attempt++ {
		resp, _ := postJSON(t, srv, "/api/bootloader/login", "",
			`{"username":"op","password":"wrong"}`)
		last = resp.StatusCode
	}
	if last != http.StatusTooManyRequests {
		t.Fatalf("after %d failures the source still got HTTP %d, want 429",
			failuresBeforeBackoff+1, last)
	}
}

func TestASuccessfulLoginClearsTheBackoff(t *testing.T) {
	throttle := newLoginThrottle()
	for attempt := 0; attempt < failuresBeforeBackoff-1; attempt++ {
		throttle.recordFailure("10.0.0.1")
	}
	throttle.recordSuccess("10.0.0.1")
	throttle.recordFailure("10.0.0.1")

	if wait := throttle.blockedFor("10.0.0.1"); wait > 0 {
		t.Errorf("a source that proved it knows a password is still backed off for %s", wait)
	}
}

func TestTheSourceMapCannotGrowWithoutBound(t *testing.T) {
	// The tracking must not become the memory exhaustion it prevents.
	throttle := newLoginThrottle()
	for i := 0; i < maxTrackedSources*2; i++ {
		throttle.recordFailure(net.JoinHostPort("10.0.0.1", strconv.Itoa(i)))
	}
	throttle.mu.Lock()
	size := len(throttle.sources)
	throttle.mu.Unlock()
	if size > maxTrackedSources {
		t.Errorf("tracking %d sources, cap is %d", size, maxTrackedSources)
	}
}
