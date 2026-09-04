package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/runtimeauth"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/supervisor"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/updater"
)

const (
	testSecret = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	testPepper = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)

// --- fakes ---------------------------------------------------------------

type fakeSupervisor struct {
	status        supervisor.Status
	reconcileErr  error
	stopErr       error
	reconcileHits int
	stopHits      int
}

func (f *fakeSupervisor) Status() supervisor.Status { return f.status }
func (f *fakeSupervisor) Reconcile(context.Context) error {
	f.reconcileHits++
	return f.reconcileErr
}
func (f *fakeSupervisor) Stop(context.Context) error {
	f.stopHits++
	return f.stopErr
}
func (f *fakeSupervisor) ContainerName() string { return "openplc-runtime" }

type fakeLogs struct {
	out string
	err error
}

func (f *fakeLogs) ContainerLogs(context.Context, string, int) (string, error) {
	return f.out, f.err
}

type fakeUsers struct {
	count    int
	countErr error
	user     *runtimeauth.User
	authErr  error
	// role answers RoleByID. Defaults to admin so the existing cases, which
	// are about routing and auth rather than authorization, keep passing.
	role    string
	roleErr error
}

func (f *fakeUsers) CountUsers(context.Context) (int, error) { return f.count, f.countErr }
func (f *fakeUsers) Authenticate(context.Context, string, string, string) (*runtimeauth.User, error) {
	if f.authErr != nil {
		return nil, f.authErr
	}
	return f.user, nil
}

func (f *fakeUsers) Secrets() *runtimeauth.Secrets {
	return &runtimeauth.Secrets{JWTSecret: testSecret, Pepper: testPepper}
}

func (f *fakeUsers) RoleByID(context.Context, string) (string, error) {
	if f.roleErr != nil {
		return "", f.roleErr
	}
	if f.role == "" {
		return RoleAdmin, nil
	}
	return f.role, nil
}

// newTestServer builds a server with an httptest mux, bypassing TLS: the
// certificate path is covered separately, and routing plus auth is what these
// tests are about.
func newTestServer(t *testing.T, users *fakeUsers, sup *fakeSupervisor, logs *fakeLogs) *httptest.Server {
	t.Helper()
	srv := &Server{cfg: Config{
		Version:        "bootloader-v1.0.0-test",
		RuntimeVersion: func() string { return "v4.2.1" },
		Users:          users,
		Supervisor:     sup,
		Logs:           logs,
		Log:            slog.New(slog.NewTextHandler(io.Discard, nil)),
	}}
	mux := http.NewServeMux()
	srv.routes(mux)
	httpSrv := httptest.NewServer(mux)
	t.Cleanup(httpSrv.Close)
	return httpSrv
}

func healthySupervisor() *fakeSupervisor {
	return &fakeSupervisor{status: supervisor.Status{
		State: supervisor.StateHealthy, Since: time.Now(), HealthSource: "docker healthcheck",
	}}
}

func validToken(t *testing.T) string {
	t.Helper()
	token, err := runtimeauth.IssueToken(testSecret, "1", time.Hour)
	if err != nil {
		t.Fatalf("issuing token: %v", err)
	}
	return token
}

func get(t *testing.T, srv *httptest.Server, path, token string) (*http.Response, map[string]any) {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, srv.URL+path, nil)
	if err != nil {
		t.Fatalf("building request: %v", err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := srv.Client().Do(req)
	if err != nil {
		t.Fatalf("requesting %s: %v", path, err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	var body map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&body)
	return resp, body
}

func postJSON(t *testing.T, srv *httptest.Server, path, token, payload string) (*http.Response, map[string]any) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, srv.URL+path, strings.NewReader(payload))
	if err != nil {
		t.Fatalf("building request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := srv.Client().Do(req)
	if err != nil {
		t.Fatalf("requesting %s: %v", path, err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	var body map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&body)
	return resp, body
}

// --- capabilities --------------------------------------------------------

func TestCapabilitiesIsUnauthenticated(t *testing.T) {
	// A client has to be able to tell what it reached before it has
	// credentials, exactly as with the runtime's own /api/capabilities.
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	resp, body := get(t, srv, "/api/bootloader/capabilities", "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200, got %d", resp.StatusCode)
	}
	if body["service"] != "openplc-bootloader" {
		t.Fatalf("want a service identifier, got %v", body["service"])
	}
	if body["recovery"] != false {
		t.Fatalf("a healthy device is not in recovery, got %v", body["recovery"])
	}
}

func TestCapabilitiesFlagsRecovery(t *testing.T) {
	// The editor keys its recovery panel off this, so it has to be truthful
	// without needing a token.
	sup := &fakeSupervisor{status: supervisor.Status{
		State: supervisor.StateRecovery, Reason: "runtime exited 3 times",
	}}
	srv := newTestServer(t, &fakeUsers{count: 1}, sup, &fakeLogs{})
	_, body := get(t, srv, "/api/bootloader/capabilities", "")
	if body["recovery"] != true {
		t.Fatalf("want recovery true, got %v", body["recovery"])
	}
}

// --- authentication ------------------------------------------------------

func TestProtectedRoutesRejectAMissingToken(t *testing.T) {
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	for _, path := range []string{"/api/bootloader/status", "/api/bootloader/logs"} {
		resp, _ := get(t, srv, path, "")
		if resp.StatusCode != http.StatusUnauthorized {
			t.Fatalf("%s without a token: want 401, got %d", path, resp.StatusCode)
		}
	}
}

func TestProtectedRoutesRejectATokenSignedWithAnotherSecret(t *testing.T) {
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	forged, err := runtimeauth.IssueToken(strings.Repeat("c", 64), "1", time.Hour)
	if err != nil {
		t.Fatalf("issuing: %v", err)
	}
	resp, _ := get(t, srv, "/api/bootloader/status", forged)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d", resp.StatusCode)
	}
}

func TestATokenTheBootloaderIssuedIsAccepted(t *testing.T) {
	// The bootloader owns its own sessions: the editor logs in here with the
	// credentials it already holds, and this token is only ever presented
	// back to the bootloader. Cross-service acceptance is deliberately not a
	// contract -- the two services may resolve different .env files.
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	resp, _ := get(t, srv, "/api/bootloader/status", validToken(t))
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200, got %d", resp.StatusCode)
	}
}

func TestEverythingIsRefusedWhenNoAccountsExist(t *testing.T) {
	// First-user bootstrap belongs to the runtime alone. A bootloader that
	// could mint the first admin would be a second path to owning the device.
	srv := newTestServer(t, &fakeUsers{count: 0}, healthySupervisor(), &fakeLogs{})

	resp, body := get(t, srv, "/api/bootloader/status", validToken(t))
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("want 403 with no accounts, got %d", resp.StatusCode)
	}
	if msg, _ := body["error"].(string); !strings.Contains(msg, "runtime") {
		t.Fatalf("the refusal must point the operator at the runtime, got %q", msg)
	}

	resp, _ = postJSON(t, srv, "/api/bootloader/login", "",
		`{"username":"admin","password":"x"}`)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("login must be refused too, got %d", resp.StatusCode)
	}
}

func TestAnUnreadableAccountDatabaseIsReportedAsUnavailable(t *testing.T) {
	// Not 401: the caller's credentials are not the problem, and telling them
	// they are would send them chasing the wrong thing.
	srv := newTestServer(t,
		&fakeUsers{countErr: errors.New("disk error")}, healthySupervisor(), &fakeLogs{})
	resp, _ := get(t, srv, "/api/bootloader/status", validToken(t))
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("want 503, got %d", resp.StatusCode)
	}
}

func TestTheBearerSchemeIsCaseInsensitive(t *testing.T) {
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	req, err := http.NewRequest(http.MethodGet, srv.URL+"/api/bootloader/status", nil)
	if err != nil {
		t.Fatalf("building request: %v", err)
	}
	req.Header.Set("Authorization", "bearer "+validToken(t))
	resp, err := srv.Client().Do(req)
	if err != nil {
		t.Fatalf("requesting: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200 for a lowercase scheme, got %d", resp.StatusCode)
	}
}

// --- login ---------------------------------------------------------------

func TestLoginIssuesAUsableToken(t *testing.T) {
	users := &fakeUsers{count: 1, user: &runtimeauth.User{ID: "7", Username: "op", Role: "admin"}}
	srv := newTestServer(t, users, healthySupervisor(), &fakeLogs{})

	resp, body := postJSON(t, srv, "/api/bootloader/login", "",
		`{"username":"op","password":"op"}`)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200, got %d (%v)", resp.StatusCode, body)
	}
	token, _ := body["access_token"].(string)
	if token == "" {
		t.Fatal("login must return an access token")
	}
	// The token it issued must actually work on a protected route -- a token
	// that parses but is rejected would be a maddening thing to debug.
	statusResp, _ := get(t, srv, "/api/bootloader/status", token)
	if statusResp.StatusCode != http.StatusOK {
		t.Fatalf("the issued token was refused: %d", statusResp.StatusCode)
	}
}

func TestLoginRejectsBadCredentialsWithoutRevealingWhy(t *testing.T) {
	users := &fakeUsers{count: 1, authErr: runtimeauth.ErrNoSuchUser}
	srv := newTestServer(t, users, healthySupervisor(), &fakeLogs{})
	resp, body := postJSON(t, srv, "/api/bootloader/login", "",
		`{"username":"nobody","password":"x"}`)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d", resp.StatusCode)
	}
	msg, _ := body["error"].(string)
	// One message for both an unknown user and a wrong password.
	if !strings.Contains(msg, "wrong username or password") {
		t.Fatalf("want an indistinguishable message, got %q", msg)
	}
}

func TestAnUnverifiableHashIsNotReportedAsABadPassword(t *testing.T) {
	// Otherwise an operator is left convinced they have forgotten their own
	// password when the real problem is a hash format we cannot read.
	users := &fakeUsers{count: 1, authErr: runtimeauth.ErrUnsupportedHash}
	srv := newTestServer(t, users, healthySupervisor(), &fakeLogs{})
	resp, _ := postJSON(t, srv, "/api/bootloader/login", "",
		`{"username":"op","password":"op"}`)
	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("want 500 for an unreadable hash, got %d", resp.StatusCode)
	}
}

func TestLoginRequiresBothFields(t *testing.T) {
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	resp, _ := postJSON(t, srv, "/api/bootloader/login", "", `{"username":"op"}`)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", resp.StatusCode)
	}
}

func TestAnUnknownFieldInTheLoginBodyIsRejected(t *testing.T) {
	// DisallowUnknownFields: a client sending "user" instead of "username"
	// should be told, not silently treated as sending nothing.
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	resp, _ := postJSON(t, srv, "/api/bootloader/login", "",
		`{"username":"op","password":"op","extra":1}`)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", resp.StatusCode)
	}
}

// --- status and logs -----------------------------------------------------

func TestStatusReportsTheSupervisorState(t *testing.T) {
	sup := &fakeSupervisor{status: supervisor.Status{
		State: supervisor.StateRecovery, Reason: "runtime exited 3 times", CrashCount: 3,
	}}
	srv := newTestServer(t, &fakeUsers{count: 1}, sup, &fakeLogs{})
	_, body := get(t, srv, "/api/bootloader/status", validToken(t))

	if body["state"] != string(supervisor.StateRecovery) {
		t.Fatalf("want recovery, got %v", body["state"])
	}
	if body["reason"] != "runtime exited 3 times" {
		t.Fatalf("the reason must reach the operator verbatim, got %v", body["reason"])
	}
	if body["crashCount"].(float64) != 3 {
		t.Fatalf("want crashCount 3, got %v", body["crashCount"])
	}
}

func TestLogsReturnTheRuntimeTail(t *testing.T) {
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(),
		&fakeLogs{out: "starting OpenPLC\nready\n"})
	_, body := get(t, srv, "/api/bootloader/logs", validToken(t))
	if body["available"] != true {
		t.Fatalf("want available true, got %v", body["available"])
	}
	if !strings.Contains(body["logs"].(string), "ready") {
		t.Fatalf("logs did not come through: %v", body["logs"])
	}
}

func TestMissingLogsAreNotAnError(t *testing.T) {
	// A device before its first successful start has no container to read, and
	// that is a state to report rather than a failure.
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(),
		&fakeLogs{err: errors.New("no such container")})
	resp, body := get(t, srv, "/api/bootloader/logs", validToken(t))
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200, got %d", resp.StatusCode)
	}
	if body["available"] != false {
		t.Fatalf("want available false, got %v", body["available"])
	}
}

func TestAnAbsurdLogTailIsClamped(t *testing.T) {
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{out: "x"})
	_, body := get(t, srv, "/api/bootloader/logs?tail=999999", validToken(t))
	if body["tail"].(float64) != float64(maxLogTail) {
		t.Fatalf("want the tail clamped to %d, got %v", maxLogTail, body["tail"])
	}
}

func TestANonNumericLogTailIsRejected(t *testing.T) {
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	resp, _ := get(t, srv, "/api/bootloader/logs?tail=lots", validToken(t))
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", resp.StatusCode)
	}
}

// --- restart -------------------------------------------------------------

func TestRestartStopsThenReconciles(t *testing.T) {
	// Reconcile rather than a docker restart: it is the only path that also
	// creates the container when it is missing, so restart works identically
	// on a device that has never started one.
	sup := healthySupervisor()
	srv := newTestServer(t, &fakeUsers{count: 1}, sup, &fakeLogs{})

	resp, _ := postJSON(t, srv, "/api/bootloader/restart", validToken(t), `{}`)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200, got %d", resp.StatusCode)
	}
	if sup.stopHits != 1 || sup.reconcileHits != 1 {
		t.Fatalf("want one stop and one reconcile, got stop=%d reconcile=%d",
			sup.stopHits, sup.reconcileHits)
	}
}

func TestRestartReportsAFailureToComeBackUp(t *testing.T) {
	sup := healthySupervisor()
	sup.reconcileErr = errors.New("no such image")
	srv := newTestServer(t, &fakeUsers{count: 1}, sup, &fakeLogs{})

	resp, body := postJSON(t, srv, "/api/bootloader/restart", validToken(t), `{}`)
	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", resp.StatusCode)
	}
	if msg, _ := body["error"].(string); !strings.Contains(msg, "no such image") {
		t.Fatalf("the underlying cause must reach the operator, got %q", msg)
	}
}

func TestRestartIsNotReachableWithoutAToken(t *testing.T) {
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	resp, _ := postJSON(t, srv, "/api/bootloader/restart", "", `{}`)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d", resp.StatusCode)
	}
}

// --- surface -------------------------------------------------------------

func TestTheApiOffersNoProgramOrPlcControl(t *testing.T) {
	// The bootloader is deliberately dumb: programs and PLC control belong to
	// the runtime. This pins that, so a future addition has to be a conscious
	// decision rather than a route somebody added in passing.
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	token := validToken(t)
	for _, path := range []string{
		"/api/bootloader/upload-file",
		"/api/bootloader/start-plc",
		"/api/bootloader/stop-plc",
		"/api/bootloader/compile",
	} {
		resp, _ := postJSON(t, srv, path, token, `{}`)
		if resp.StatusCode != http.StatusNotFound {
			t.Fatalf("%s must not exist, got %d", path, resp.StatusCode)
		}
	}
}

// --- update --------------------------------------------------------------

type fakeUpdater struct {
	startErr error
	started  []string
	progress updater.Progress
}

func (f *fakeUpdater) Start(_ context.Context, version string) error {
	if f.startErr != nil {
		return f.startErr
	}
	f.started = append(f.started, version)
	return nil
}

func (f *fakeUpdater) Progress() updater.Progress { return f.progress }

// newTestServerWithUpdater is newTestServer plus an updater, kept separate so
// the existing tests keep exercising the routes that do not need one.
func newTestServerWithUpdater(t *testing.T, up Updater) *httptest.Server {
	t.Helper()
	srv := &Server{cfg: Config{
		Version:        "bootloader-v1.0.0-test",
		RuntimeVersion: func() string { return "v4.2.1" },
		Users:          &fakeUsers{count: 1},
		Supervisor:     healthySupervisor(),
		Logs:           &fakeLogs{},
		Updater:        up,
		Log:            slog.New(slog.NewTextHandler(io.Discard, nil)),
	}}
	mux := http.NewServeMux()
	srv.routes(mux)
	httpSrv := httptest.NewServer(mux)
	t.Cleanup(httpSrv.Close)
	return httpSrv
}

func TestAnUpdateIsAcceptedAndRunsInTheBackground(t *testing.T) {
	// 202, not 200: a pull can run for many minutes on a plant link, so the
	// request returns as soon as the work is under way and the client polls.
	up := &fakeUpdater{}
	srv := newTestServerWithUpdater(t, up)

	resp, body := postJSON(t, srv, "/api/bootloader/update", validToken(t),
		`{"version":"v4.2.2"}`)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("want 202, got %d (%v)", resp.StatusCode, body)
	}
	if len(up.started) != 1 || up.started[0] != "v4.2.2" {
		t.Fatalf("want the requested version started, got %v", up.started)
	}
}

func TestDowngradeUsesTheSameRequestAsUpgrade(t *testing.T) {
	// There is no separate direction and no version floor: a user may
	// deliberately pair an older runtime with an older editor.
	up := &fakeUpdater{}
	srv := newTestServerWithUpdater(t, up)

	resp, _ := postJSON(t, srv, "/api/bootloader/update", validToken(t),
		`{"version":"v4.1.10"}`)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("a downgrade must be accepted like any other version, got %d",
			resp.StatusCode)
	}
}

func TestASecondUpdateAnswers409WithTheProgressAttached(t *testing.T) {
	// Attaching the progress lets a second editor follow along rather than
	// guess what the first one asked for.
	up := &fakeUpdater{
		startErr: updater.ErrInProgress,
		progress: updater.Progress{State: updater.StatePulling, To: "v4.2.2"},
	}
	srv := newTestServerWithUpdater(t, up)

	resp, body := postJSON(t, srv, "/api/bootloader/update", validToken(t),
		`{"version":"v4.2.3"}`)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("want 409, got %d", resp.StatusCode)
	}
	progress, ok := body["progress"].(map[string]any)
	if !ok {
		t.Fatalf("the in-flight progress must be attached, got %v", body)
	}
	if progress["to"] != "v4.2.2" {
		t.Fatalf("want the in-flight target, got %v", progress["to"])
	}
}

func TestAnInvalidVersionIsRefusedWithTheReason(t *testing.T) {
	up := &fakeUpdater{startErr: errors.New(`"evil.example.com/x:v1" is not a valid version tag`)}
	srv := newTestServerWithUpdater(t, up)

	resp, body := postJSON(t, srv, "/api/bootloader/update", validToken(t),
		`{"version":"evil.example.com/x:v1"}`)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", resp.StatusCode)
	}
	if msg, _ := body["error"].(string); !strings.Contains(msg, "not a valid version tag") {
		t.Fatalf("the reason must reach the operator, got %q", msg)
	}
}

func TestUpdateProgressIsPollable(t *testing.T) {
	fifty := 50
	up := &fakeUpdater{progress: updater.Progress{
		State: updater.StatePulling, From: "v4.2.1", To: "v4.2.2",
		Phase: "Downloading", Percent: &fifty,
	}}
	srv := newTestServerWithUpdater(t, up)

	resp, body := get(t, srv, "/api/bootloader/update", validToken(t))
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200, got %d", resp.StatusCode)
	}
	if body["state"] != string(updater.StatePulling) {
		t.Fatalf("want pulling, got %v", body["state"])
	}
	if body["percent"].(float64) != 50 {
		t.Fatalf("want 50%%, got %v", body["percent"])
	}
}

func TestUpdateRoutesRequireAToken(t *testing.T) {
	srv := newTestServerWithUpdater(t, &fakeUpdater{})
	resp, _ := postJSON(t, srv, "/api/bootloader/update", "", `{"version":"v4.2.2"}`)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d", resp.StatusCode)
	}
	resp, _ = get(t, srv, "/api/bootloader/update", "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("want 401 on progress too, got %d", resp.StatusCode)
	}
}

// --- self-update ---------------------------------------------------------

type fakeSelfUpdater struct {
	err       error
	requested []string
}

func (f *fakeSelfUpdater) Start(_ context.Context, version string) error {
	if f.err != nil {
		return f.err
	}
	f.requested = append(f.requested, version)
	return nil
}

func newTestServerWithSelfUpdater(t *testing.T, self SelfUpdater) *httptest.Server {
	t.Helper()
	srv := &Server{cfg: Config{
		Version:        "bootloader-v1.0.0-test",
		RuntimeVersion: func() string { return "v4.2.1" },
		Users:          &fakeUsers{count: 1},
		Supervisor:     healthySupervisor(),
		Logs:           &fakeLogs{},
		SelfUpdater:    self,
		Log:            slog.New(slog.NewTextHandler(io.Discard, nil)),
	}}
	mux := http.NewServeMux()
	srv.routes(mux)
	httpSrv := httptest.NewServer(mux)
	t.Cleanup(httpSrv.Close)
	return httpSrv
}

func TestASelfUpdateIsAcceptedAndSaysWhatToExpect(t *testing.T) {
	// There is nothing to poll: this process is replaced as part of the
	// operation, so the response has to tell the operator what is about to
	// happen instead of promising progress that will never arrive.
	self := &fakeSelfUpdater{}
	srv := newTestServerWithSelfUpdater(t, self)

	resp, body := postJSON(t, srv, "/api/bootloader/self-update", validToken(t),
		`{"version":"bootloader-v1.1.0"}`)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("want 202, got %d (%v)", resp.StatusCode, body)
	}
	if len(self.requested) != 1 || self.requested[0] != "bootloader-v1.1.0" {
		t.Fatalf("want the requested version passed through, got %v", self.requested)
	}
	message, _ := body["message"].(string)
	if !strings.Contains(message, "runtime keeps running") {
		t.Fatalf("the reply must say the PLC is unaffected, got %q", message)
	}
}

func TestASelfUpdateRefusalIsSurfacedWithItsReason(t *testing.T) {
	self := &fakeSelfUpdater{err: errors.New("downloading bootloader-v9.9.9: manifest unknown")}
	srv := newTestServerWithSelfUpdater(t, self)

	resp, body := postJSON(t, srv, "/api/bootloader/self-update", validToken(t),
		`{"version":"bootloader-v9.9.9"}`)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", resp.StatusCode)
	}
	if msg, _ := body["error"].(string); !strings.Contains(msg, "manifest unknown") {
		t.Fatalf("the cause must reach the operator, got %q", msg)
	}
}

func TestSelfUpdateRequiresAToken(t *testing.T) {
	srv := newTestServerWithSelfUpdater(t, &fakeSelfUpdater{})
	resp, _ := postJSON(t, srv, "/api/bootloader/self-update", "", `{"version":"bootloader-v1.1.0"}`)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d", resp.StatusCode)
	}
}

func TestSelfUpdateIsReportedUnavailableWhenNotConfigured(t *testing.T) {
	// 501 rather than a panic on a nil interface: a bootloader built without
	// this wired up should say so.
	srv := newTestServer(t, &fakeUsers{count: 1}, healthySupervisor(), &fakeLogs{})
	resp, _ := postJSON(t, srv, "/api/bootloader/self-update", validToken(t),
		`{"version":"bootloader-v1.1.0"}`)
	if resp.StatusCode != http.StatusNotImplemented {
		t.Fatalf("want 501, got %d", resp.StatusCode)
	}
}
