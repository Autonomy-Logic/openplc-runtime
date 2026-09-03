// Package dockerapi is a minimal client for the Docker Engine API over the
// host's unix socket.
//
// Hand-rolled rather than using the official SDK on purpose: the bootloader needs
// eight calls, and the SDK brings a dependency tree into the one component
// whose job is to still work when everything else is broken. The Engine API is
// JSON over HTTP; the only unusual part is dialing a unix socket instead of a
// TCP address, which the transport below handles.
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

// apiVersion is pinned low enough to work on the oldest engine we support.
// The SLM-RP4 test device ships Docker 20.10 (API 1.41), and every call this
// package makes has been stable since well before that. Pinning avoids a
// daemon upgrade silently changing a response shape under us.
const apiVersion = "v1.41"

// Client talks to the Docker daemon. Safe for concurrent use: the embedded
// http.Client is, and nothing else here holds mutable state.
type Client struct {
	http   *http.Client
	socket string
}

// New returns a client bound to socket. A zero-value socket means
// DefaultSocket.
//
// The timeout applies to unary calls only. Streaming calls (events, image
// pull) must not be bounded by it -- an events stream is meant to stay open
// for the life of the process -- so they run on a separate, timeout-free
// client. Using one client for both is the classic way to end up with an
// events stream that dies silently after 30 seconds.
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
			Transport: &http.Transport{DialContext: dial},
			Timeout:   30 * time.Second,
		},
		socket: socket,
	}
}

// streamClient is New's client without the request timeout, for long-lived
// response bodies. It shares nothing with the unary client but the socket
// path.
func (c *Client) streamClient() *http.Client {
	dial := func(ctx context.Context, _, _ string) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, "unix", c.socket)
	}
	return &http.Client{Transport: &http.Transport{DialContext: dial}}
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
	resp, err := c.streamClient().Do(req)
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
