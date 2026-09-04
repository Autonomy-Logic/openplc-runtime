//go:build !linux

package discovery

import "syscall"

// reusePort is a no-op off Linux. The bootloader only ships for Linux; this
// exists so the package builds for a developer running `go test` on a laptop.
func reusePort(_, _ string, _ syscall.RawConn) error { return nil }
