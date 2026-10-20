// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Package dockerapi is a minimal client for the Docker Engine API
// over the host's unix socket. Hand-rolled to avoid pulling the
// official SDK's dependency tree into the one component that must
// still work when everything else is broken.
package dockerapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// DefaultSocket is where the Docker daemon listens on a standard install. The
// bootloader bind-mounts it read-write; there is no read-only mode for a socket,
// which is why the bootloader stays small enough to audit.
const DefaultSocket = "/var/run/docker.sock"

// apiVersion is pinned low enough for Docker 20.10 (SLM-RP4). Pinning
// avoids a daemon upgrade silently changing a response shape.
const apiVersion = "v1.41"

// Client talks to the Docker daemon. Safe for concurrent use: the embedded
// http.Client is, and nothing else here holds mutable state.
type Client struct {
	http *http.Client
	// streamHTTP has no request timeout, for long-lived response bodies.
	streamHTTP *http.Client
	socket     string
}

// New returns a client bound to socket (empty means DefaultSocket). The
// 30s timeout applies to unary calls only; streaming (events, pull) runs
// on a separate timeout-free client.
func New(socket string) *Client {
	if socket == "" {
		socket = DefaultSocket
	}
	dial := func(ctx context.Context, _, _ string) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, "unix", socket)
	}
	return &Client{
		http: &http.Client{
			Transport: &http.Transport{
				DialContext:         dial,
				IdleConnTimeout:     90 * time.Second,
				MaxIdleConnsPerHost: 4,
			},
			Timeout: 30 * time.Second,
		},
		streamHTTP: newStreamClient(socket),
		socket:     socket,
	}
}

// newStreamClient builds the timeout-free client used for long-lived
// response bodies. One instance (not per-call) so idle sockets are
// shared and bounded, instead of pinned by throwaway transports.
func newStreamClient(socket string) *http.Client {
	dial := func(ctx context.Context, _, _ string) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, "unix", socket)
	}
	return &http.Client{Transport: &http.Transport{
		DialContext: dial,
		// Bound the pool anyway: a socket that goes quiet should not be held
		// open indefinitely just because the pool has room for it.
		IdleConnTimeout:     90 * time.Second,
		MaxIdleConnsPerHost: 2,
	}}
}

// APIError is a non-2xx response from the daemon. The daemon's own message is
// preserved verbatim: it is almost always more specific than anything we would
// write, and it is what ends up in front of an operator.
type APIError struct {
	Status  int
	Message string
	Path    string
}

func (e *APIError) Error() string {
	if e.Message == "" {
		return fmt.Sprintf("docker %s: HTTP %d", e.Path, e.Status)
	}
	return fmt.Sprintf("docker %s: HTTP %d: %s", e.Path, e.Status, e.Message)
}

// IsNotFound reports whether err is a 404 from the daemon, which is how it
// says "no such container" and "no such image". Callers branch on this
// constantly -- a missing container is the normal case on first boot, not a
// failure.
func IsNotFound(err error) bool {
	return hasStatus(err, http.StatusNotFound)
}

// IsConflict reports whether err is a 409, which the daemon uses for "already
// started", "already stopped" and name collisions. All three mean the desired
// state already holds, so reconcile treats them as success.
func IsConflict(err error) bool {
	return hasStatus(err, http.StatusConflict)
}

func hasStatus(err error, status int) bool {
	var apiErr *APIError
	return errors.As(err, &apiErr) && apiErr.Status == status
}

// do issues a unary request and decodes a JSON response into out. A nil out
// discards the body, which several endpoints return empty anyway.
func (c *Client) do(ctx context.Context, method, path string, body, out any) error {
	req, err := c.newRequest(ctx, method, path, body)
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("docker %s: %w", path, err)
	}
	defer resp.Body.Close()

	if err := checkResponse(resp, path); err != nil {
		return err
	}
	if out == nil {
		// Drain so the connection can be reused rather than closed.
		_, _ = io.Copy(io.Discard, resp.Body)
		return nil
	}
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		return fmt.Errorf("docker %s: decoding response: %w", path, err)
	}
	return nil
}

// stream issues a request whose response body the caller consumes
// incrementally. The caller owns the returned ReadCloser and must close it.
func (c *Client) stream(ctx context.Context, method, path string, body any) (io.ReadCloser, error) {
	req, err := c.newRequest(ctx, method, path, body)
	if err != nil {
		return nil, err
	}
	resp, err := c.streamHTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("docker %s: %w", path, err)
	}
	if err := checkResponse(resp, path); err != nil {
		resp.Body.Close()
		return nil, err
	}
	return resp.Body, nil
}

func (c *Client) newRequest(ctx context.Context, method, path string, body any) (*http.Request, error) {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("docker %s: encoding request: %w", path, err)
		}
		reader = bytes.NewReader(encoded)
	}
	// The host part is ignored by the unix-socket dialer but http.NewRequest
	// insists on an absolute URL.
	req, err := http.NewRequestWithContext(ctx, method, "http://docker"+apiVersion+path, reader)
	if err != nil {
		return nil, fmt.Errorf("docker %s: building request: %w", path, err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return req, nil
}

// checkResponse turns a non-2xx into an *APIError carrying the daemon's own
// message. The daemon answers errors as {"message": "..."} but not always, so
// a body that will not decode falls back to the raw text.
func checkResponse(resp *http.Response, path string) error {
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return nil
	}
	// Bounded: an error body is small, and an unbounded read here would let a
	// misbehaving daemon exhaust memory in the recovery component.
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	message := strings.TrimSpace(string(raw))
	var decoded struct {
		Message string `json:"message"`
	}
	if json.Unmarshal(raw, &decoded) == nil && decoded.Message != "" {
		message = decoded.Message
	}
	return &APIError{Status: resp.StatusCode, Message: message, Path: path}
}

// doLongRunning issues a request bounded by the caller's context, not by
// the shared client's fixed timeout. Needed for calls the daemon holds
// open (stop with a grace period, image pull).
func (c *Client) doLongRunning(ctx context.Context, method, path string, body any) error {
	req, err := c.newRequest(ctx, method, path, body)
	if err != nil {
		return err
	}
	resp, err := c.streamHTTP.Do(req)
	if err != nil {
		return fmt.Errorf("docker %s: %w", path, err)
	}
	defer resp.Body.Close()

	if err := checkResponse(resp, path); err != nil {
		return err
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	return nil
}

// Ping reports whether the daemon is reachable. Used at start-up so a missing
// or unmountable socket is reported as exactly that, instead of surfacing later
// as a confusing container-create failure.
func (c *Client) Ping(ctx context.Context) error {
	return c.do(ctx, http.MethodGet, "/_ping", nil, nil)
}

// Version reports the daemon and API versions, for logging and for the status
// the editor displays.
func (c *Client) Version(ctx context.Context) (Version, error) {
	var v Version
	err := c.do(ctx, http.MethodGet, "/version", nil, &v)
	return v, err
}

// Version is the subset of /version the bootloader reports.
type Version struct {
	Version    string `json:"Version"`
	APIVersion string `json:"ApiVersion"`
	Arch       string `json:"Arch"`
	KernelVer  string `json:"KernelVersion"`
}

// encodeQuery renders params as a query string, or "" when there are none.
func encodeQuery(params url.Values) string {
	if len(params) == 0 {
		return ""
	}
	return "?" + params.Encode()
}

// Reason extracts the daemon's own message from a wrapped error chain:
// everything above the APIError is transport machinery, and operators
// need the bottom layer. The full chain still goes to the log.
func Reason(err error) string {
	if err == nil {
		return ""
	}
	var apiErr *APIError
	if errors.As(err, &apiErr) && apiErr.Message != "" {
		return apiErr.Message
	}
	// Not a daemon response (a transport failure, a stall). Keep the
	// innermost segment, which is where the cause is.
	message := err.Error()
	if index := strings.LastIndex(message, ": "); index >= 0 && index+2 < len(message) {
		return message[index+2:]
	}
	return message
}
