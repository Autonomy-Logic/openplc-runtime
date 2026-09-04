// Package api is the bootloader's control API on port 8445.
//
// Deliberately small. This is the interface to the component that recovers a
// device, so its surface is the shortest list that does the job: say what
// state you are in, show me the runtime's logs, restart it, change its
// version, wipe its data. It accepts no programs and does not control the PLC
// -- those belong to the runtime, and a bootloader that could do them would be
// a second, less-reviewed path to the same capability.
//
// Every route except login and capabilities requires a token from the
// runtime's own account set. Capabilities is unauthenticated for the same
// reason the runtime's is: a client has to be able to tell what it is talking
// to before it has credentials.
package api

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/dockerapi"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/runtimeauth"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/supervisor"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/updater"
)

// DefaultPort is the bootloader's control port. 8445 keeps to the odd numbers
// alongside the runtime's 8443, and being a separate port avoids any handoff
// race with the runtime over a shared one.
const DefaultPort = 8445

// Supervisor is what the API can ask of the runtime container. Narrow on
// purpose: the API must not be able to do anything to the runtime that is not
// on this list.
type Supervisor interface {
	Status() supervisor.Status
	Reconcile(ctx context.Context) error
	Stop(ctx context.Context) error
	ContainerName() string
}

// LogReader fetches the runtime container's recent output, so an operator can
// see why a runtime would not start without shell access.
type LogReader interface {
	ContainerLogs(ctx context.Context, name string, tail int) (string, error)
}

// Updater changes which runtime version the device runs. Start returns as
// soon as the work is under way; a pull can run for many minutes on a plant
// link, so the caller polls Progress rather than holding a request open.
type Updater interface {
	Start(ctx context.Context, targetVersion string) error
	Progress() updater.Progress
}

// SelfUpdater replaces the bootloader with a newer version of itself.
//
// Start returns once the helper that performs the swap is running: this
// process is about to be stopped by it, so there is no completion to report
// and nothing to poll -- the client reconnects and reads the new version from
// capabilities.
type SelfUpdater interface {
	Start(ctx context.Context, version string) error
}

// HostReporter answers for the machine the runtime runs on.
//
// The bootloader is the right place for this. It exists on every device that
// can be updated from an editor, including one running a runtime far older
// than these endpoints -- so a Runtime Status screen fed from here is
// populated regardless of which runtime version is installed, which is not
// true of anything served by the runtime itself.
type HostReporter interface {
	SystemInfo(ctx context.Context) (*dockerapi.Info, error)
}

// Authenticator resolves credentials against the runtime's account set, and
// serves the signing secret behind the tokens it issues.
//
// Secrets() is read per request rather than snapshotted at start-up: on a
// fresh install the runtime writes .env and restapi.db AFTER the bootloader is
// already running, and a snapshot taken before that left every authenticated
// route answering 503 until the container was restarted.
type Authenticator interface {
	Authenticate(ctx context.Context, username, password, pepper string) (*runtimeauth.User, error)
	CountUsers(ctx context.Context) (int, error)
	Secrets() *runtimeauth.Secrets
	RoleByID(ctx context.Context, userID string) (string, error)
}

// Config wires the server.
type Config struct {
	Port     int
	StateDir string
	// Version of the bootloader binary, reported by capabilities.
	Version string
	// RuntimeVersion is the image tag the bootloader intends to run, which is
	// not necessarily what is running right now (mid-update, or in recovery).
	RuntimeVersion func() string
	Users          Authenticator
	Supervisor     Supervisor
	Host           HostReporter
	Logs           LogReader
	Updater        Updater
	SelfUpdater    SelfUpdater
	Log            *slog.Logger
}

// Server is the bootloader's HTTPS control API.
type Server struct {
	cfg  Config
	http *http.Server

	// Created on first use rather than in New, so a Server built any other
	// way (the tests build literals) still has it. A nil-safe throttle was
	// the alternative and a worse one: it would leave the rate limit silently
	// absent instead of simply present.
	throttleOnce sync.Once
	throttleImpl *loginThrottle
}

func (s *Server) loginLimiter() *loginThrottle {
	s.throttleOnce.Do(func() { s.throttleImpl = newLoginThrottle() })
	return s.throttleImpl
}

// New builds the server, generating a TLS certificate on first use.
func New(cfg Config) (*Server, error) {
	if cfg.Port == 0 {
		cfg.Port = DefaultPort
	}
	if cfg.Log == nil {
		return nil, errors.New("api: a logger is required")
	}
	cert, err := LoadOrCreateCertificate(cfg.StateDir)
	if err != nil {
		return nil, err
	}

	server := &Server{cfg: cfg}
	mux := http.NewServeMux()
	server.routes(mux)

	server.http = &http.Server{
		Addr:    ":" + strconv.Itoa(cfg.Port),
		Handler: mux,
		TLSConfig: &tls.Config{
			Certificates: []tls.Certificate{cert},
			MinVersion:   tls.VersionTLS12,
		},
		// A slow or dead client must not be able to hold a connection open
		// forever against the recovery component.
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		// Generous: a log tail can be large, and an update's progress stream
		// is polled rather than held open.
		WriteTimeout: 60 * time.Second,
		IdleTimeout:  120 * time.Second,
	}
	return server, nil
}

func (s *Server) routes(mux *http.ServeMux) {
	// Unauthenticated: a client must be able to identify what it reached and
	// obtain a token.
	mux.HandleFunc("GET /api/bootloader/capabilities", s.handleCapabilities)
	mux.HandleFunc("POST /api/bootloader/login", s.handleLogin)

	// Authenticated.
	mux.HandleFunc("GET /api/bootloader/status", s.authenticated(s.handleStatus))
	mux.HandleFunc("GET /api/bootloader/device-info", s.authenticated(s.handleDeviceInfo))
	mux.HandleFunc("GET /api/bootloader/logs", s.authenticated(s.handleLogs))
	mux.HandleFunc("POST /api/bootloader/restart", s.adminOnly(s.handleRestart))
	mux.HandleFunc("POST /api/bootloader/update", s.adminOnly(s.handleUpdate))
	mux.HandleFunc("GET /api/bootloader/update", s.authenticated(s.handleUpdateProgress))
	mux.HandleFunc("POST /api/bootloader/self-update", s.adminOnly(s.handleSelfUpdate))
}

// ListenAndServe blocks until ctx is cancelled or the listener fails.
func (s *Server) ListenAndServe(ctx context.Context) error {
	listener, err := net.Listen("tcp", s.http.Addr)
	if err != nil {
		return fmt.Errorf("listening on %s: %w", s.http.Addr, err)
	}
	s.cfg.Log.Info("control API listening", "addr", s.http.Addr)

	errCh := make(chan error, 1)
	go func() {
		errCh <- s.http.ServeTLS(listener, "", "")
	}()

	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := s.http.Shutdown(shutdownCtx); err != nil {
			s.cfg.Log.Warn("control API shutdown", "error", err)
		}
		return ctx.Err()
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return fmt.Errorf("control API: %w", err)
	}
}

// --- middleware ----------------------------------------------------------

// authenticated wraps a handler with bearer-token verification.
//
// It also enforces the no-users rule: with no accounts on the device the
// bootloader accepts nothing at all. First-user bootstrap is a sensitive flow
// that lives in the runtime alone, and a bootloader that could mint the first
// admin would be a second path to owning the device.
func (s *Server) authenticated(next func(http.ResponseWriter, *http.Request)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		count, err := s.cfg.Users.CountUsers(r.Context())
		if err != nil {
			s.cfg.Log.Error("counting users", "error", err)
			writeError(w, http.StatusServiceUnavailable,
				"cannot read the runtime's account database")
			return
		}
		if count == 0 {
			writeError(w, http.StatusForbidden,
				"no accounts exist on this device yet; create the first one through "+
					"the runtime before using the bootloader")
			return
		}

		token, ok := bearerToken(r)
		if !ok {
			writeError(w, http.StatusUnauthorized, "a bearer token is required")
			return
		}
		claims, err := runtimeauth.VerifyToken(s.cfg.Users.Secrets().JWTSecret, token)
		if err != nil {
			if errors.Is(err, runtimeauth.ErrTokenExpired) {
				writeError(w, http.StatusUnauthorized, "token expired; log in again")
				return
			}
			writeError(w, http.StatusUnauthorized, "invalid token")
			return
		}
		s.cfg.Log.Debug("authenticated request",
			"path", r.URL.Path, "subject", claims.Subject)
		next(w, r.WithContext(withSubject(r.Context(), claims.Subject)))
	}
}

// subjectKey carries the authenticated user id to the admin check below.
type subjectKey struct{}

func withSubject(ctx context.Context, subject string) context.Context {
	return context.WithValue(ctx, subjectKey{}, subject)
}

func subjectFrom(ctx context.Context) string {
	subject, _ := ctx.Value(subjectKey{}).(string)
	return subject
}

// RoleAdmin is the role the runtime gives an account that may change the
// device. Matched against the runtime's own value (webserver/restapi.py).
const RoleAdmin = "admin"

// adminOnly restricts a route to administrators.
//
// Applied to the routes that can change what this device runs. Without it any
// runtime account -- including one the runtime itself treats as restricted --
// could change the runtime version or self-update the bootloader, and a
// self-update starts a container with the Docker socket bound, which is host
// root. The runtime distinguishes these roles; the component that can replace
// the runtime must not be the one that ignores the distinction.
//
// The role is read from the database per request. The token carries none, and
// a role claim would mean a demotion did not take effect until the token
// expired.
func (s *Server) adminOnly(next func(http.ResponseWriter, *http.Request)) http.HandlerFunc {
	return s.authenticated(func(w http.ResponseWriter, r *http.Request) {
		subject := subjectFrom(r.Context())
		role, err := s.cfg.Users.RoleByID(r.Context(), subject)
		if err != nil {
			// Includes the account having been deleted while its token was
			// still valid. Refuse rather than guess.
			s.cfg.Log.Warn("could not read the role for an authenticated request",
				"subject", subject, "path", r.URL.Path, "error", err)
			writeError(w, http.StatusForbidden,
				"this account's role could not be confirmed; log in again")
			return
		}
		if role != RoleAdmin {
			s.cfg.Log.Warn("refused a non-admin request",
				"subject", subject, "role", role, "path", r.URL.Path)
			writeError(w, http.StatusForbidden,
				"this action requires an administrator account")
			return
		}
		next(w, r)
	})
}

func bearerToken(r *http.Request) (string, bool) {
	header := r.Header.Get("Authorization")
	// Case-insensitive scheme: RFC 7235 says the scheme is case-insensitive
	// and clients do vary.
	if len(header) < 7 || !strings.EqualFold(header[:7], "bearer ") {
		return "", false
	}
	token := strings.TrimSpace(header[7:])
	return token, token != ""
}

// --- handlers ------------------------------------------------------------

// handleDeviceInfo reports the machine the runtime runs on.
//
// Sourced from the Docker daemon, which runs on the host and answers for it.
// The obvious alternative -- have the runtime report on itself -- is what this
// replaces: that endpoint exists only in runtimes new enough to have it, so
// every device in the field today answered it with a catch-all body and the
// screen had nothing to show. The bootloader is present wherever an update is
// possible at all, which makes it the one source that is always there.
//
// Deliberately only facts that VARY between devices. "This runtime runs in a
// container" and "this device updates itself" were both here at one point and
// are neither: a client reaching this handler at all has already learned them
// from the bootloader answering, so reporting them again was a field that
// could only ever hold one value.
func (s *Server) handleDeviceInfo(w http.ResponseWriter, r *http.Request) {
	payload := map[string]any{
		"bootloaderVersion": s.cfg.Version,
		"runtimeVersion":    s.cfg.RuntimeVersion(),
	}

	if s.cfg.Host != nil {
		info, err := s.cfg.Host.SystemInfo(r.Context())
		if err != nil {
			// Report the versions rather than failing the request: a daemon
			// that will not answer says nothing about the bootloader, and a
			// half-filled header beats an error where there is no fault.
			s.cfg.Log.Warn("could not read host information", "error", err)
		} else {
			payload["hostname"] = info.Name
			payload["architecture"] = info.Architecture
			payload["kernel"] = info.KernelVersion
			payload["system"] = info.OperatingSystem
			payload["cpus"] = info.NCPU
			payload["memoryBytes"] = info.MemTotal
			payload["dockerVersion"] = info.ServerVersion
		}
	}

	writeJSON(w, http.StatusOK, payload)
}

func (s *Server) handleCapabilities(w http.ResponseWriter, r *http.Request) {
	status := s.cfg.Supervisor.Status()
	// Enough for a client to know what it reached and whether the runtime is
	// usable, and nothing more: this is served without authentication.
	writeJSON(w, http.StatusOK, map[string]any{
		"service":           "openplc-bootloader",
		"bootloaderVersion": s.cfg.Version,
		"runtimeVersion":    s.cfg.RuntimeVersion(),
		"state":             status.State,
		"recovery":          status.State == supervisor.StateRecovery,
	})
}

// loginRequest mirrors the runtime's /api/login body so the editor can reuse
// the same request shape against either port.
type loginRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	var body loginRequest
	// A bounded read: an unauthenticated caller must not be able to make the
	// bootloader buffer an arbitrary amount.
	if err := decodeJSON(w, r, &body, 4*1024); err != nil {
		return
	}
	if body.Username == "" || body.Password == "" {
		writeError(w, http.StatusBadRequest, "username and password are required")
		return
	}

	source := requestSource(r)
	if wait := s.loginLimiter().blockedFor(source); wait > 0 {
		s.cfg.Log.Warn("refused a login from a backed-off source",
			"source", source, "retry_after", wait.Round(time.Second))
		w.Header().Set("Retry-After", strconv.Itoa(int(wait.Round(time.Second).Seconds())))
		writeError(w, http.StatusTooManyRequests,
			"too many failed attempts from this address; try again shortly")
		return
	}

	count, err := s.cfg.Users.CountUsers(r.Context())
	if err != nil {
		s.cfg.Log.Error("counting users", "error", err)
		writeError(w, http.StatusServiceUnavailable,
			"cannot read the runtime's account database")
		return
	}
	if count == 0 {
		writeError(w, http.StatusForbidden,
			"no accounts exist on this device yet; create the first one through the runtime")
		return
	}

	// The PBKDF2 verification below is the expensive part, so the slot is
	// taken around it and nothing else. Refusing rather than queueing: a
	// queue would let an attacker hold connections open and still consume the
	// CPU eventually.
	if !s.loginLimiter().acquire() {
		s.cfg.Log.Warn("refused a login: too many verifications in flight", "source", source)
		w.Header().Set("Retry-After", "1")
		writeError(w, http.StatusTooManyRequests,
			"the bootloader is busy verifying another sign-in; try again shortly")
		return
	}
	user, err := s.cfg.Users.Authenticate(r.Context(), body.Username, body.Password, s.cfg.Users.Secrets().Pepper)
	s.loginLimiter().release()
	if err != nil {
		if errors.Is(err, runtimeauth.ErrUnsupportedHash) {
			// A deployment problem, not a wrong password. Saying so is what
			// makes it fixable; the alternative is an operator convinced they
			// have forgotten their own password.
			s.cfg.Log.Error("stored password hash is unreadable", "error", err)
			writeError(w, http.StatusInternalServerError,
				"this runtime's password hashes are in a format the bootloader cannot verify")
			return
		}
		// Unknown user and wrong password answer identically; distinguishing
		// them enumerates valid accounts.
		s.loginLimiter().recordFailure(source)
		s.cfg.Log.Warn("failed login", "username", body.Username, "source", source)
		writeError(w, http.StatusUnauthorized, "wrong username or password")
		return
	}
	s.loginLimiter().recordSuccess(source)

	token, err := runtimeauth.IssueToken(s.cfg.Users.Secrets().JWTSecret, user.ID, runtimeauth.DefaultTokenTTL)
	if err != nil {
		s.cfg.Log.Error("issuing token", "error", err)
		writeError(w, http.StatusInternalServerError, "could not issue a token")
		return
	}
	s.cfg.Log.Info("login", "username", user.Username, "role", user.Role)
	writeJSON(w, http.StatusOK, map[string]any{
		"access_token": token,
		"role":         user.Role,
	})
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	status := s.cfg.Supervisor.Status()
	writeJSON(w, http.StatusOK, map[string]any{
		"state":          status.State,
		"reason":         status.Reason,
		"since":          status.Since,
		"crashCount":     status.CrashCount,
		"healthSource":   status.HealthSource,
		"containerId":    status.ContainerID,
		"containerName":  s.cfg.Supervisor.ContainerName(),
		"image":          status.Image,
		"runtimeVersion": s.cfg.RuntimeVersion(),
		"recovery":       status.State == supervisor.StateRecovery,
	})
}

// maxLogTail bounds a log request so a caller cannot ask the daemon for an
// entire container's history.
const (
	defaultLogTail = 200
	maxLogTail     = 5000
)

func (s *Server) handleLogs(w http.ResponseWriter, r *http.Request) {
	tail := defaultLogTail
	if raw := r.URL.Query().Get("tail"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed <= 0 {
			writeError(w, http.StatusBadRequest, "tail must be a positive integer")
			return
		}
		tail = min(parsed, maxLogTail)
	}

	logs, err := s.cfg.Logs.ContainerLogs(r.Context(), s.cfg.Supervisor.ContainerName(), tail)
	if err != nil {
		// A missing container is the interesting case, not an error: it is
		// what a device looks like before its first successful start.
		s.cfg.Log.Warn("reading runtime logs", "error", err)
		writeJSON(w, http.StatusOK, map[string]any{
			"logs":      "",
			"available": false,
			"reason":    err.Error(),
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"logs":      logs,
		"available": true,
		"tail":      tail,
	})
}

func (s *Server) handleRestart(w http.ResponseWriter, r *http.Request) {
	// Stop then reconcile, rather than a "restart" call: reconcile is the one
	// path that knows how to create the container if it is missing, so this
	// works identically on a device that has never started one.
	if err := s.cfg.Supervisor.Stop(r.Context()); err != nil {
		s.cfg.Log.Warn("stopping runtime for restart", "error", err)
	}
	if err := s.cfg.Supervisor.Reconcile(r.Context()); err != nil {
		s.cfg.Log.Error("restarting runtime", "error", err)
		writeError(w, http.StatusInternalServerError,
			fmt.Sprintf("the runtime did not come back up: %v", err))
		return
	}
	status := s.cfg.Supervisor.Status()
	writeJSON(w, http.StatusOK, map[string]any{
		"state":  status.State,
		"reason": status.Reason,
	})
}

// updateRequest asks for a specific version. Upgrade and downgrade are the
// same request: there is no separate direction, because there is no version
// floor and nothing about the mechanism cares which way the number moves.
type updateRequest struct {
	Version string `json:"version"`
}

func (s *Server) handleUpdate(w http.ResponseWriter, r *http.Request) {
	var body updateRequest
	if err := decodeJSON(w, r, &body, 4*1024); err != nil {
		return
	}

	// No PLC-stopped gate: an authenticated request is sufficient, whether
	// the PLC is running or not. The runtime flushes retained variables on
	// SIGTERM and the stop grace period is sized for it.
	if err := s.cfg.Updater.Start(r.Context(), body.Version); err != nil {
		if errors.Is(err, updater.ErrInProgress) {
			// 409, with the current progress attached so a second editor can
			// simply follow along instead of guessing.
			writeJSON(w, http.StatusConflict, map[string]any{
				"error":    err.Error(),
				"progress": s.cfg.Updater.Progress(),
			})
			return
		}
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	s.cfg.Log.Info("update requested", "version", body.Version)
	// 202: accepted and running, not finished. The client polls GET on the
	// same path.
	writeJSON(w, http.StatusAccepted, map[string]any{
		"accepted": true,
		"progress": s.cfg.Updater.Progress(),
	})
}

func (s *Server) handleUpdateProgress(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.cfg.Updater.Progress())
}

// handleSelfUpdate replaces the bootloader itself.
//
// Separate from the runtime update on purpose: they change different things
// and fail differently. A bootloader that will not come back costs the ability
// to manage the device; a runtime that will not come back stops the plant. The
// runtime container is untouched here, so a PLC keeps running throughout.
//
// There is no progress to poll. This process is replaced as part of the
// operation, so the client's connection ends with it -- reconnecting and
// reading /capabilities is how you learn the outcome.
func (s *Server) handleSelfUpdate(w http.ResponseWriter, r *http.Request) {
	if s.cfg.SelfUpdater == nil {
		writeError(w, http.StatusNotImplemented, "this bootloader cannot update itself")
		return
	}

	var body updateRequest
	if err := decodeJSON(w, r, &body, 4*1024); err != nil {
		return
	}

	if err := s.cfg.SelfUpdater.Start(r.Context(), body.Version); err != nil {
		s.cfg.Log.Error("self-update refused", "version", body.Version, "error", err)
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	s.cfg.Log.Info("self-update accepted", "version", body.Version)
	writeJSON(w, http.StatusAccepted, map[string]any{
		"accepted": true,
		"message": "The bootloader is being replaced. It will be unreachable for a few " +
			"seconds; the runtime keeps running throughout.",
	})
}

// --- helpers -------------------------------------------------------------

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	// No caching: every response here is a live device state.
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		// The status line is already sent, so there is nothing to correct --
		// only something to record.
		return
	}
}

// writeError uses one shape for every failure so a client has exactly one
// thing to parse. The message is written for a person: it says what went
// wrong and, where there is one, what to do about it.
func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]any{"error": message})
}

func decodeJSON(w http.ResponseWriter, r *http.Request, out any, limit int64) error {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, limit))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(out); err != nil {
		writeError(w, http.StatusBadRequest, "request body is not the expected JSON")
		return err
	}
	return nil
}
