// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

//go:build linux

package updater

import (
	"fmt"
	"syscall"
)

// freeBytes reports free space on the filesystem holding path.
// Measured on the bootloader's state dir (not Docker's data root, which
// is not mounted here). Uses Bavail, not Bfree, so root-reserved blocks
// are excluded.
func freeBytes(path string) (int64, error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, fmt.Errorf("checking free space on %s: %w", path, err)
	}
	return int64(stat.Bavail) * int64(stat.Bsize), nil
}
