//go:build linux

package discovery

import (
	"syscall"

	"golang.org/x/sys/unix"
)

// reusePort sets SO_REUSEADDR and SO_REUSEPORT on the listening socket.
//
// The runtime sets both when it binds the discovery port. Linux shares a UDP
// port only when every socket involved asked to, so the bootloader has to ask
// too -- otherwise its lingering socket makes the runtime's bind fail, and the
// runtime binds once at start-up and never retries.
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
