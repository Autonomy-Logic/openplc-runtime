#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

set -euo pipefail

BUILD_DIR="build"

# Remove old object files from root (if any left from older builds)
find . -maxdepth 1 -name "*.o" -type f -exec rm -f {} \;

# Clean extra .o files from build dir if needed
rm -f "$BUILD_DIR"/*.o

# Remove old libplc_*.so files to ensure only one exists
rm -f "$BUILD_DIR"/libplc_*.so

TIMESTAMP=$(date +%s%N)
UNIQUE_LIBPLC="libplc_${TIMESTAMP}.so"

# Move resulting shared library to unique name
mv "$BUILD_DIR/new_libplc.so" "$BUILD_DIR/$UNIQUE_LIBPLC"
