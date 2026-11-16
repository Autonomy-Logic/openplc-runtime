// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

package api

import (
	"bytes"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/runtimeauth"
)

// syncBuffer is a log sink safe to read while the server goroutines write to it.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// testRig is a server with every collaborator faked, its log captured and, optionally, its throttle preset.
type testRig struct {
	srv  *httptest.Server
	log  *syncBuffer
	sup  *fakeSupervisor
	up   *fakeUpdater
	self *fakeSelfUpdater
}

func newRig(t *testing.T, users *fakeUsers, throttle *loginThrottle) *testRig {
	t.Helper()
	rig := &testRig{
		log:  &syncBuffer{},
		sup:  healthySupervisor(),
		up:   &fakeUpdater{},
		self: &fakeSelfUpdater{},
	}
	server := &Server{cfg: Config{
		Version:        "bootloader-v1.0.0-test",
		RuntimeVersion: func() string { return "v4.2.1" },
		Users:          users,
		Supervisor:     rig.sup,
		Logs:           &fakeLogs{out: "log"},
		Updater:        rig.up,
		SelfUpdater:    rig.self,
		Log:            slog.New(slog.NewTextHandler(rig.log, nil)),
	}}
	if throttle != nil {
		server.throttleOnce.Do(func() { server.throttleImpl = throttle })
	}
	mux := http.NewServeMux()
	server.routes(mux)
	rig.srv = httptest.NewServer(mux)
	t.Cleanup(rig.srv.Close)
	return rig
}

type route struct {
	name, method, path, body string
}

var readRoutes = []route{
	{"status", http.MethodGet, "/api/bootloader/status", ""},
	{"device-info", http.MethodGet, "/api/bootloader/device-info", ""},
	{"logs", http.MethodGet, "/api/bootloader/logs", ""},
	{"update progress", http.MethodGet, "/api/bootloader/update", ""},
}

var adminRoutes = []route{
	{"restart", http.MethodPost, "/api/bootloader/restart", "{}"},
	{"update", http.MethodPost, "/api/bootloader/update", `{"version":"v4.2.1"}`},
	{"self-update", http.MethodPost, "/api/bootloader/self-update", `{"version":"bootloader-v1.0.1"}`},
}

func (rig *testRig) call(t *testing.T, rt route, token string) (*http.Response, map[string]any) {
	t.Helper()
	if rt.method == http.MethodGet {
		return get(t, rig.srv, rt.path, token)
	}
	return postJSON(t, rig.srv, rt.path, token, rt.body)
}

func (rig *testRig) assertNothingRan(t *testing.T) {
	t.Helper()
	if rig.sup.stopHits != 0 || rig.sup.reconcileHits != 0 || len(rig.up.started) != 0 || len(rig.self.requested) != 0 {
		t.Error("a refused request still reached its handler")
	}
}

// --- roles ---------------------------------------------------------------

func TestHMIRolesAreRefusedOnEveryAuthenticatedRoute(t *testing.T) {
	for _, role := range []string{runtimeauth.RoleOperator, runtimeauth.RoleViewer} {
		for _, rt := range append(append([]route{}, readRoutes...), adminRoutes...) {
			t.Run(role+" "+rt.name, func(t *testing.T) {
				rig := newRig(t, &fakeUsers{count: 1, role: role}, nil)

				resp, body := rig.call(t, rt, validToken(t))
				if resp.StatusCode != http.StatusForbidden {
					t.Fatalf("%s got HTTP %d on %s %s, want 403", role, resp.StatusCode, rt.method, rt.path)
				}
				if body["error"] == "" {
					t.Error("no explanation was returned")
				}
				if !strings.Contains(rig.log.String(), "refused a request from a role without bootloader access") ||
					!strings.Contains(rig.log.String(), "role="+role) {
					t.Errorf("the refusal was not logged with the role: %s", rig.log.String())
				}
				rig.assertNothingRan(t)
			})
		}
	}
}

func TestAnUnknownRoleIsRefused(t *testing.T) {
	rig := newRig(t, &fakeUsers{count: 1, role: "superuser"}, nil)
	resp, _ := get(t, rig.srv, "/api/bootloader/status", validToken(t))
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("an unknown role got HTTP %d, want 403", resp.StatusCode)
	}
}

func TestAnEngineerReadsButCannotChangeWhatTheDeviceRuns(t *testing.T) {
	for _, rt := range readRoutes {
		t.Run(rt.name, func(t *testing.T) {
			rig := newRig(t, &fakeUsers{count: 1, role: runtimeauth.RoleEngineer}, nil)
			resp, _ := rig.call(t, rt, validToken(t))
			if resp.StatusCode != http.StatusOK {
				t.Fatalf("an engineer got HTTP %d on %s, want 200", resp.StatusCode, rt.path)
			}
		})
	}
	for _, rt := range adminRoutes {
		t.Run(rt.name, func(t *testing.T) {
			rig := newRig(t, &fakeUsers{count: 1, role: runtimeauth.RoleEngineer}, nil)
			resp, body := rig.call(t, rt, validToken(t))
			if resp.StatusCode != http.StatusForbidden {
				t.Fatalf("an engineer got HTTP %d on %s, want 403", resp.StatusCode, rt.path)
			}
			if body["error"] == "" {
				t.Error("no explanation was returned")
			}
			rig.assertNothingRan(t)
		})
	}
}

func TestAnAdminReachesEveryRoute(t *testing.T) {
	for _, rt := range append(append([]route{}, readRoutes...), adminRoutes...) {
		t.Run(rt.name, func(t *testing.T) {
			rig := newRig(t, &fakeUsers{count: 1, role: runtimeauth.RoleAdmin}, nil)
			resp, _ := rig.call(t, rt, validToken(t))
			if resp.StatusCode >= 400 {
				t.Fatalf("an admin got HTTP %d on %s", resp.StatusCode, rt.path)
			}
		})
	}
}

// Sign-in stays open to every role; the token it issues is then refused by the routes.
func TestAnOperatorCanSignInButNotUseTheToken(t *testing.T) {
	users := &fakeUsers{
		count: 1,
		user:  &runtimeauth.User{ID: "3", Username: "hmi", Role: runtimeauth.RoleOperator},
		role:  runtimeauth.RoleOperator,
	}
	rig := newRig(t, users, nil)

	resp, body := postJSON(t, rig.srv, "/api/bootloader/login", "", `{"username":"hmi","password":"pw"}`)
	if resp.StatusCode != http.StatusOK || body["role"] != runtimeauth.RoleOperator {
		t.Fatalf("sign-in got HTTP %d (%v), want 200 with role operator", resp.StatusCode, body)
	}
	token, _ := body["access_token"].(string)
	statusResp, _ := get(t, rig.srv, "/api/bootloader/status", token)
	if statusResp.StatusCode != http.StatusForbidden {
		t.Fatalf("the operator's token got HTTP %d on status, want 403", statusResp.StatusCode)
	}
}

// --- account state -------------------------------------------------------

func TestATokenWhoseAccountIsGoneIsRefused(t *testing.T) {
	for _, rt := range []route{readRoutes[0], adminRoutes[1]} {
		t.Run(rt.name, func(t *testing.T) {
			rig := newRig(t, &fakeUsers{count: 1, accountErr: runtimeauth.ErrNoSuchUser}, nil)
			resp, _ := rig.call(t, rt, validToken(t))
			if resp.StatusCode != http.StatusUnauthorized {
				t.Fatalf("got HTTP %d, want 401 for a deleted account", resp.StatusCode)
			}
			rig.assertNothingRan(t)
		})
	}
}

func TestAnUnreadableAccountIsReportedAsUnavailable(t *testing.T) {
	rig := newRig(t, &fakeUsers{count: 1, accountErr: errors.New("disk I/O error")}, nil)
	resp, _ := get(t, rig.srv, "/api/bootloader/status", validToken(t))
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("got HTTP %d, want 503", resp.StatusCode)
	}
}

func TestATokenIssuedBeforeTheAccountCutOffIsRefused(t *testing.T) {
	token := validToken(t)
	cutoff := time.Now().Add(time.Minute).UnixMilli()
	for _, rt := range []route{readRoutes[0], adminRoutes[0]} {
		t.Run(rt.name, func(t *testing.T) {
			rig := newRig(t, &fakeUsers{count: 1, validAfterMs: cutoff}, nil)
			resp, body := rig.call(t, rt, token)
			if resp.StatusCode != http.StatusUnauthorized {
				t.Fatalf("a token from before the cut-off got HTTP %d, want 401", resp.StatusCode)
			}
			if body["error"] == "" {
				t.Error("no explanation was returned")
			}
			if !strings.Contains(rig.log.String(), "refused a token issued before the account's sessions were ended") {
				t.Errorf("the refusal was not logged: %s", rig.log.String())
			}
			rig.assertNothingRan(t)
		})
	}
}

func TestATokenIssuedAfterTheAccountCutOffIsAccepted(t *testing.T) {
	cutoff := time.Now().Add(-time.Minute).UnixMilli()
	rig := newRig(t, &fakeUsers{count: 1, validAfterMs: cutoff}, nil)
	resp, _ := get(t, rig.srv, "/api/bootloader/status", validToken(t))
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("a token from after the cut-off got HTTP %d, want 200", resp.StatusCode)
	}
}

func TestATokenFromSignInAfterACutOffIsAccepted(t *testing.T) {
	cutoff := time.Now().UnixMilli() - 1
	users := &fakeUsers{
		count: 1, validAfterMs: cutoff,
		user: &runtimeauth.User{ID: "1", Username: "admin", Role: runtimeauth.RoleAdmin},
	}
	rig := newRig(t, users, nil)
	resp, body := postJSON(t, rig.srv, "/api/bootloader/login", "", `{"username":"admin","password":"pw"}`)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("sign-in got HTTP %d", resp.StatusCode)
	}
	token, _ := body["access_token"].(string)
	statusResp, _ := get(t, rig.srv, "/api/bootloader/status", token)
	if statusResp.StatusCode != http.StatusOK {
		t.Fatalf("a fresh token got HTTP %d after the cut-off, want 200", statusResp.StatusCode)
	}
}
