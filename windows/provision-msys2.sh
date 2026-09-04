#!/bin/bash
# OpenPLC Runtime - MSYS2 Provisioning Script
# This script is run inside MSYS2 to install all required packages and dependencies
# for the OpenPLC Runtime Windows distribution.
#
# This script simply calls the main install.sh script which handles all
# MSYS2-specific installation and configuration.

set -e

echo "=========================================="
echo "OpenPLC Runtime - MSYS2 Provisioning"
echo "=========================================="

# Get the OpenPLC directory (parent of windows folder)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENPLC_DIR="$(dirname "$SCRIPT_DIR")"

echo "OpenPLC Directory: $OPENPLC_DIR"

# Run the main install script. --native explicitly: install.sh defaults to a
# Docker install, which cannot work here and would compile nothing. It forces
# native on MSYS2 anyway, but this script exists to build a Windows payload and
# should say so rather than depend on that detection.
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
