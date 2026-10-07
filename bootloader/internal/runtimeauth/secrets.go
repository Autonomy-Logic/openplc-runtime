// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Package runtimeauth authenticates callers against the runtime's own
// credentials.
//
// The bootloader deliberately does not keep a second user database. It reads the
// runtime's “.env“ and “restapi.db“ from the shared data directory --
// mounted read-only, because it only ever needs to read them -- so there is
// exactly one set of accounts on the device and no second thing to keep in
// sync or forget to revoke.
//
// The formats here mirror the runtime's and must stay byte-compatible with it,
// the same hazard as the ctypes mirror in shared/plugin_runtime_args.py. Both
// sides are pinned by a shared test vector: tests/pytest/restapi generates a
// hash and a token, and the Go tests verify the identical values.
package runtimeauth

import (
	"bufio"
	"fmt"
	"os"
	"strings"
)

// Secrets are the two values generated once by the runtime's
// generate_env_file and never rotated: changing either invalidates every
// stored password hash. The bootloader uses the pepper to verify
// passwords and the JWT secret to sign its OWN tokens (not shared).
type Secrets struct {
	// JWTSecret signs and verifies access tokens (HS256).
	JWTSecret string
	// Pepper is appended to a password before hashing.
	Pepper string
}

// LoadSecrets reads the runtime's .env. Hand-rolled parser: the file has
// four fixed KEY=VALUE lines with no quoting or expansion.
func LoadSecrets(path string) (*Secrets, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("reading runtime secrets from %s: %w", path, err)
	}
	defer file.Close()

	values := map[string]string{}
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, found := strings.Cut(line, "=")
		if !found {
			continue
		}
		values[strings.TrimSpace(key)] = strings.TrimSpace(value)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("reading %s: %w", path, err)
	}

	secrets := &Secrets{
		JWTSecret: values["JWT_SECRET_KEY"],
		Pepper:    values["PEPPER"],
	}
	// Both are required. Proceeding with an empty secret would accept tokens
	// signed with an empty key, which is worse than refusing to start.
	if secrets.JWTSecret == "" {
		return nil, fmt.Errorf("%s has no JWT_SECRET_KEY", path)
	}
	if secrets.Pepper == "" {
		return nil, fmt.Errorf("%s has no PEPPER", path)
	}
	return secrets, nil
}
