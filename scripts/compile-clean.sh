#!/bin/bash
set -euo pipefail

BUILD_DIR="build"

# Move resulting shared library to standard name
mv "$BUILD_DIR/libplc_new.so" "$BUILD_DIR/libplc.so"

# Remove old object files from root (if any left from older builds)
find . -maxdepth 1 -name "*.o" -type f -exec rm -f {} \;

# Clean extra .o files from build dir if needed
rm -f "$BUILD_DIR"/*.o

echo "[INFO] Cleanup finished. libplc.so is in $BUILD_DIR/"
