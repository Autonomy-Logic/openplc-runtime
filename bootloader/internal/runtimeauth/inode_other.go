//go:build !linux

package runtimeauth

import "os"

// inodeOf has no portable answer off Linux; size and mtime carry the
// comparison there. The bootloader only ships for Linux -- this exists so the
// package still builds for a developer running `go test` on a laptop.
func inodeOf(os.FileInfo) uint64 { return 0 }
