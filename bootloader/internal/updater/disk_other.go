// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

//go:build !linux

package updater

// freeBytes stub: exists so the package builds on non-Linux dev
// machines. Returning 0 makes the pre-check skip rather than fail.
func freeBytes(_ string) (int64, error) {
	return 0, nil
}
