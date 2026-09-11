package dockerapi

import (
	"context"
	"net/http"
)

// Info is the subset of the daemon's /info the bootloader reports.
//
// These are HOST facts, not container ones, and that is the whole reason this
// exists. The bootloader runs in a container: its own uname reports the shared
// kernel correctly but its hostname is a container id, and reading /etc/os-release
// from the image would describe the image rather than the device. The daemon
// runs on the host and answers for it, over a socket the bootloader already
// holds -- so no extra mounts, no extra privileges, and nothing that has to be
// kept in step with how the container happens to be launched.
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
