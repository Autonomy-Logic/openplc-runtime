// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

package runtimeauth

// Account roles, matching webserver/access.py.
const (
	RoleAdmin    = "admin"
	RoleEngineer = "engineer"
	RoleOperator = "operator"
	RoleViewer   = "viewer"
)

// legacyUserRole is the pre-four-role name of engineer, still stored until the runtime migrates it.
const legacyUserRole = "user"

// NormalizeRole maps a stored role to its current name: NULL, empty and "user" become engineer.
func NormalizeRole(stored string) string {
	if stored == "" || stored == legacyUserRole {
		return RoleEngineer
	}
	return stored
}

// MayUseBootloader reports whether role reaches the bootloader's authenticated routes.
func MayUseBootloader(role string) bool {
	return role == RoleAdmin || role == RoleEngineer
}
