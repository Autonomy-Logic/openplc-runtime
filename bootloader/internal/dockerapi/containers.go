// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

package dockerapi

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// ContainerState is the subset of a container inspect that the supervisor
// reasons about.
type ContainerState struct {
	Status     string `json:"Status"` // created|running|paused|restarting|removing|exited|dead
	Running    bool   `json:"Running"`
	ExitCode   int    `json:"ExitCode"`
	StartedAt  string `json:"StartedAt"`
	FinishedAt string `json:"FinishedAt"`
	Health     *struct {
		// starting|healthy|unhealthy, or absent when the image declares no
		// HEALTHCHECK. Absent is not a failure: it means "no opinion", and the
		// supervisor falls back to liveness alone.
		Status string `json:"Status"`
	} `json:"Health"`
}

// ContainerHostConfig is the host-config slice captured from a RUNNING
// container so a self-update can reproduce it, including operator-added
// mounts and port overrides that defaults would silently drop.
type ContainerHostConfig struct {
	Binds       []string `json:"Binds"`
	NetworkMode string   `json:"NetworkMode"`
	// "host", or empty for Docker's private default.
	UTSMode       string        `json:"UTSMode"`
	Privileged    bool          `json:"Privileged"`
	RestartPolicy RestartPolicy `json:"RestartPolicy"`
}

// RestartPolicy mirrors Docker's shape.
type RestartPolicy struct {
	Name              string `json:"Name"`
	MaximumRetryCount int    `json:"MaximumRetryCount,omitempty"`
}

// ContainerInspect is the subset of GET /containers/{id}/json we use.
type ContainerInspect struct {
	ID     string         `json:"Id"`
	Name   string         `json:"Name"`
	State  ContainerState `json:"State"`
	Config struct {
		Image  string   `json:"Image"`
		Env    []string `json:"Env"`
		Cmd    []string `json:"Cmd"`
		Labels map[string]string
	} `json:"Config"`
	HostConfig ContainerHostConfig `json:"HostConfig"`
	Image      string              `json:"Image"` // resolved image ID, not the tag
}

// HealthStatus returns the container's healthcheck verdict, or "" when the
// image declares none.
func (c *ContainerInspect) HealthStatus() string {
	if c.State.Health == nil {
		return ""
	}
	return c.State.Health.Status
}

// InspectContainer returns the container's current state. A missing container
// yields an error satisfying IsNotFound, which is the normal first-boot case.
func (c *Client) InspectContainer(ctx context.Context, name string) (*ContainerInspect, error) {
	var out ContainerInspect
	if err := c.do(ctx, http.MethodGet, "/containers/"+url.PathEscape(name)+"/json", nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// CreateContainerResponse is the daemon's reply to a create.
type CreateContainerResponse struct {
	ID       string   `json:"Id"`
	Warnings []string `json:"Warnings"`
}

// CreateContainer creates a container from spec under the given name. The
// spec is passed through as-is so the caller owns the whole configuration --
// see internal/runtimespec, which is the single place the runtime's flags are
// decided.
func (c *Client) CreateContainer(ctx context.Context, name string, spec any) (*CreateContainerResponse, error) {
	params := url.Values{}
	params.Set("name", name)
	var out CreateContainerResponse
	path := "/containers/create" + encodeQuery(params)
	if err := c.do(ctx, http.MethodPost, path, spec, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// StartContainer starts an existing container. A 409 means it is already
// running, which the caller may treat as success.
func (c *Client) StartContainer(ctx context.Context, name string) error {
	return c.do(ctx, http.MethodPost, "/containers/"+url.PathEscape(name)+"/start", nil, nil)
}

// StopContainer sends SIGTERM, then SIGKILL after the grace period. The
// runtime flushes retained variables on SIGTERM; cutting the grace short
// risks losing the retain image.
func (c *Client) StopContainer(ctx context.Context, name string, grace time.Duration) error {
	params := url.Values{}
	params.Set("t", strconv.Itoa(int(grace.Seconds())))
	path := "/containers/" + url.PathEscape(name) + "/stop" + encodeQuery(params)

	// Daemon holds this open for the whole grace; the client needs more
	// than the shared 30s unary timeout, which equals the default grace
	// and would truncate every clean shutdown.
	stopCtx, cancel := context.WithTimeout(ctx, grace+stopTimeoutMargin)
	defer cancel()

	err := c.doLongRunning(stopCtx, http.MethodPost, path, nil)
	if err != nil && (IsNotFound(err) || hasStatus(err, http.StatusNotModified)) {
		// Reported, not swallowed: a caller suppressing crash accounting
		// around this stop must know when no exit will follow.
		return ErrNotRunning
	}
	return err
}

// ErrNotRunning means the container was already stopped or absent, so this
// stop was a no-op and no `die` event will follow it.
var ErrNotRunning = errors.New("container was not running")

// stopTimeoutMargin is the slack on top of the grace period, covering the
// daemon's own teardown after the container has exited.
const stopTimeoutMargin = 30 * time.Second

// RemoveContainer deletes a container, forcing it down if still running.
// A missing container is success: the goal is "not present".
func (c *Client) RemoveContainer(ctx context.Context, name string, force bool) error {
	params := url.Values{}
	if force {
		params.Set("force", "true")
	}
	path := "/containers/" + url.PathEscape(name) + encodeQuery(params)
	if err := c.do(ctx, http.MethodDelete, path, nil, nil); err != nil && !IsNotFound(err) {
		return err
	}
	return nil
}

// ContainerLogs returns the tail of a container's combined output.
// Used by the bootloader status endpoint so an operator can see why a
// runtime would not start without needing shell access on the device.
func (c *Client) ContainerLogs(ctx context.Context, name string, tail int) (string, error) {
	params := url.Values{}
	params.Set("stdout", "true")
	params.Set("stderr", "true")
	params.Set("tail", strconv.Itoa(tail))
	path := "/containers/" + url.PathEscape(name) + "/logs" + encodeQuery(params)
	body, err := c.stream(ctx, http.MethodGet, path, nil)
	if err != nil {
		return "", err
	}
	defer body.Close()
	return readMultiplexed(body, 512*1024)
}

// RenameContainer gives an existing container a new name. Used by the
// self-update to swap atomically: a failed create leaves the old
// container untouched instead of removed-then-missing.
func (c *Client) RenameContainer(ctx context.Context, name, newName string) error {
	params := url.Values{}
	params.Set("name", newName)
	path := "/containers/" + url.PathEscape(name) + "/rename" + encodeQuery(params)
	return c.do(ctx, http.MethodPost, path, nil, nil)
}
