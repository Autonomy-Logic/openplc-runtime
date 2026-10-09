// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

package dockerapi

import (
	"context"
	"net/http"
)

// Info is the subset of /info the bootloader reports. These are HOST
// facts (hostname, OS), not the container's: the daemon answers for the
// host over the socket the bootloader already holds.
type Info struct {
	// Name is the host's hostname.
	Name            string `json:"Name"`
	Architecture    string `json:"Architecture"`
	KernelVersion   string `json:"KernelVersion"`
	OperatingSystem string `json:"OperatingSystem"`
	OSType          string `json:"OSType"`
	NCPU            int    `json:"NCPU"`
	MemTotal        int64  `json:"MemTotal"`
	ServerVersion   string `json:"ServerVersion"`
}

// SystemInfo reports what the daemon knows about the host it runs on.
func (c *Client) SystemInfo(ctx context.Context) (*Info, error) {
	var info Info
	if err := c.do(ctx, http.MethodGet, "/info", nil, &info); err != nil {
		return nil, err
	}
	return &info, nil
}
