//go:build !linux

package updater

// freeBytes has no non-Linux implementation.
//
// The bootloader only ever runs on Linux -- it manages Linux containers
// through a Linux daemon. This file exists purely so the package still builds
// and its tests still run on a developer's machine; returning 0 makes the
// pre-check skip rather than fail, which is the right behaviour when the
// measurement is simply unavailable.
func freeBytes(_ string) (int64, error) {
	return 0, nil
}
