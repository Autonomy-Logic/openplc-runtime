#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# MSYS2 provisioning script run from the Windows installer build.
# Delegates to install.sh --native for all package setup.

set -e

echo "=========================================="
echo "OpenPLC Runtime - MSYS2 Provisioning"
echo "=========================================="

# Get the OpenPLC directory (parent of windows folder)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENPLC_DIR="$(dirname "$SCRIPT_DIR")"

echo "OpenPLC Directory: $OPENPLC_DIR"

# --native explicit: install.sh defaults to Docker, which cannot work
# on MSYS2 and would compile nothing. Native is forced here anyway.
cd "$OPENPLC_DIR"
./install.sh --native

# Clean up to reduce size for the installer payload
echo "Cleaning up to reduce size..."
pacman -Scc --noconfirm || true
rm -rf /var/cache/pacman/pkg/* 2>/dev/null || true
rm -rf /var/log/* 2>/dev/null || true
rm -rf /tmp/* 2>/dev/null || true

echo "=========================================="
echo "OpenPLC Runtime provisioning complete!"
echo "=========================================="
