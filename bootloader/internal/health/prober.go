// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Package health probes the runtime webserver. Scope is narrow: "is
// the webserver answering". PLC state is the webserver's business
// (runtimemanager._monitor() handles plc_main). Caring about PLC
// state here would let a bad user program trigger a runtime rollback.
package health

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Prober checks the runtime's unauthenticated /api/version endpoint.
// /api/ping is JWT-required and would always 401 from here.
type Prober struct {
	url    string
	client *http.Client
}

// DefaultURL is where the runtime listens on a host-network container.
const DefaultURL = "https://127.0.0.1:8443/api/version"

// New returns a prober for url, or DefaultURL when empty. TLS verify is
// off: the connection is to 127.0.0.1 against the runtime's self-signed
// cert, so there is no name to verify and no network path to intercept.
func New(url string, timeout time.Duration) *Prober {
	if url == "" {
		url = DefaultURL
	}
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	return &Prober{
		url: url,
		client: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec // loopback, self-signed
			},
		},
	}
}

// Probe returns nil when the runtime webserver answered.
//
// Any 2xx counts. The body is not parsed: this asks "is it up", and a runtime
// that answers at all has its Flask app serving, which is the whole question.
func (p *Prober) Probe(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.url, nil)
	if err != nil {
		return fmt.Errorf("building probe request: %w", err)
	}
	resp, err := p.client.Do(req)
	if err != nil {
		return fmt.Errorf("probing %s: %w", p.url, err)
	}
	defer resp.Body.Close()
	// Drain so the connection is reusable across the start-up poll loop.
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 8*1024))

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("probing %s: HTTP %d", p.url, resp.StatusCode)
	}
	return nil
}
