// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

//go:build linux

package discovery

import (
	"syscall"

	"golang.org/x/sys/unix"
)

// reusePort sets SO_REUSEADDR and SO_REUSEPORT so the port can be shared
// with the runtime, which also sets both.
func reusePort(_, _ string, c syscall.RawConn) error {
	var setErr error
	err := c.Control(func(fd uintptr) {
		if setErr = unix.SetsockoptInt(
			int(fd), unix.SOL_SOCKET, unix.SO_REUSEADDR, 1); setErr != nil {
			return
		}
		setErr = unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_REUSEPORT, 1)
	})
	if err != nil {
		return err
	}
	return setErr
}
