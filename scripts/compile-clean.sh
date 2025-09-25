#!/bin/bash
set -euo pipefail

BUILD_DIR="build"

# Ensure build dir exists
mkdir -p "$BUILD_DIR"

# Move result if present
if [[ -f libplc.so ]]; then
    mv libplc_new.so "$BUILD_DIR/libplc.so"
fi

# Remove old object files from root (if any left from older builds)
find . -maxdepth 1 -name "*.o" -type f -exec rm -f {} \;

# (Optional) also clean extra .o files from build dir if needed
# rm -f "$BUILD_DIR"/*.o

echo "[INFO] Cleanup finished. libplc.so is in $BUILD_DIR/"
