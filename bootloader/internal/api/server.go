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
	"time"

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

// Authenticator resolves credentials against the runtime's account set.
type Authenticator interface {
	Authenticate(ctx context.Context, username, password, pepper string) (*runtimeauth.User, error)
	CountUsers(ctx context.Context) (int, error)
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
	Secrets        *runtimeauth.Secrets
	Users          Authenticator
	Supervisor     Supervisor
	Logs           LogReader
	Updater        Updater
	Log            *slog.Logger
}

// Server is the bootloader's HTTPS control API.
type Server struct {
	cfg  Config
	http *http.Server
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
	mux.HandleFunc("GET /api/bootloader/logs", s.authenticated(s.handleLogs))
	mux.HandleFunc("POST /api/bootloader/restart", s.authenticated(s.handleRestart))
	mux.HandleFunc("POST /api/bootloader/update", s.authenticated(s.handleUpdate))
	mux.HandleFunc("GET /api/bootloader/update", s.authenticated(s.handleUpdateProgress))
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
		claims, err := runtimeauth.VerifyToken(s.cfg.Secrets.JWTSecret, token)
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
		next(w, r)
	}
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

	user, err := s.cfg.Users.Authenticate(r.Context(), body.Username, body.Password, s.cfg.Secrets.Pepper)
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
		s.cfg.Log.Warn("failed login", "username", body.Username)
		writeError(w, http.StatusUnauthorized, "wrong username or password")
		return
	}

	token, err := runtimeauth.IssueToken(s.cfg.Secrets.JWTSecret, user.ID, runtimeauth.DefaultTokenTTL)
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
