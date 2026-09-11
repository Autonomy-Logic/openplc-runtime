//go:build linux

package updater

import (
	"fmt"
	"syscall"
)

// freeBytes reports free space on the filesystem holding path.
//
// Statfs on the bootloader's own state directory, not the Docker data root:
// the bootloader has no mount of /var/lib/docker, and Docker's API exposes
// image sizes but no free-space figure at all. On the layout install.sh
// creates both live under /var/lib, so this is the same filesystem. That
// assumption is why the pre-check is a warning-with-a-number rather than a
// hard gate -- an operator who has moved Docker's data-root elsewhere (as the
// AM62xx Yocto board does, to /persist) would otherwise be blocked by a
// measurement of the wrong disk.
//
// Bavail, not Bfree: Bfree counts blocks reserved for root that an ordinary
// write cannot use, so it would overstate what is actually available.
func freeBytes(path string) (int64, error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, fmt.Errorf("checking free space on %s: %w", path, err)
	}
	return int64(stat.Bavail) * int64(stat.Bsize), nil
}
