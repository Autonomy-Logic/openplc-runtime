#!/bin/bash
set -euo pipefail

# Paths
ROOT="core/generated"
LIB_PATH="$ROOT/lib"
SRC_PATH="$ROOT"
BUILD_PATH="build"

FLAGS="-w -O3 -fPIC"

check_required_files() {
    local missing_files=()
    
    if [ ! -f "$SRC_PATH/Config0.c" ]; then
        missing_files+=("$SRC_PATH/Config0.c")
    fi
    if [ ! -f "$SRC_PATH/Res0.c" ]; then
        missing_files+=("$SRC_PATH/Res0.c")
    fi
    if [ ! -f "$SRC_PATH/debug.c" ]; then
        missing_files+=("$SRC_PATH/debug.c")
    fi
    if [ ! -f "$SRC_PATH/glueVars.c" ]; then
        missing_files+=("$SRC_PATH/glueVars.c")
    fi
    if [ ! -d "$LIB_PATH" ]; then
        missing_files+=("$LIB_PATH (directory)")
    fi
    
    if [ ${#missing_files[@]} -ne 0 ]; then
        echo "[ERROR] Missing required source files:" >&2
        printf '  %s\n' "${missing_files[@]}" >&2
        exit 1
    fi
}

check_required_files

# Ensure build directory exists
mkdir -p "$BUILD_PATH"
if [ ! -d "$BUILD_PATH" ]; then
    echo "[ERROR] Failed to create build directory: $BUILD_PATH" >&2
    exit 1
fi

# Compile objects into build/
echo "[INFO] Compiling Config0.c..."
gcc $FLAGS -I "$LIB_PATH" -c "$SRC_PATH/Config0.c"   -o "$BUILD_PATH/Config0.o"
echo "[INFO] Compiling Res0.c..."
gcc $FLAGS -I "$LIB_PATH" -c "$SRC_PATH/Res0.c"      -o "$BUILD_PATH/Res0.o"
echo "[INFO] Compiling debug.c..."
gcc $FLAGS -I "$LIB_PATH" -c "$SRC_PATH/debug.c"     -o "$BUILD_PATH/debug.o"
echo "[INFO] Compiling glueVars.c..."
gcc $FLAGS -I "$LIB_PATH" -c "$SRC_PATH/glueVars.c"  -o "$BUILD_PATH/glueVars.o"

# Link shared library into build/
echo "[INFO] Compiling shared library..."
gcc $FLAGS -shared -o "$BUILD_PATH/libplc_new.so" \
    "$BUILD_PATH/Config0.o" "$BUILD_PATH/Res0.o" "$BUILD_PATH/debug.o" "$BUILD_PATH/glueVars.o"
