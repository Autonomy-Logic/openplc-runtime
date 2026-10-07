// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Package runtimeauth authenticates callers against the runtime's own
// credentials. Reads `.env` and `restapi.db` from the shared data
// directory (mounted read-only). The hash and token formats mirror
// the runtime's — pinned byte-for-byte by a shared pytest/Go vector.
package runtimeauth

import (
	"bufio"
	"fmt"
	"os"
	"strings"
)

// Secrets generated once by generate_env_file and never rotated.
// Changing either invalidates every stored password hash. Pepper verifies
// passwords; JWTSecret signs the bootloader's own tokens.
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
