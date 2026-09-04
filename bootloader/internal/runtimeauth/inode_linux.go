//go:build linux

package runtimeauth

import (
	"os"
	"syscall"
)

// inodeOf reports the file's inode, so a replaced file is noticed even when
// its size and timestamp happen to match.
func inodeOf(info os.FileInfo) uint64 {
	if sys, ok := info.Sys().(*syscall.Stat_t); ok {
		return sys.Ino
	}
	return 0
}
