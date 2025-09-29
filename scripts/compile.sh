#!/bin/bash
set -euo pipefail

# Paths
ROOT="core/generated"
LIB_PATH="$ROOT/lib"
SRC_PATH="$ROOT"
BUILD_PATH="build"

FLAGS="-w -O3 -fPIC"

# Ensure build directory exists
mkdir -p "$BUILD_PATH"

# Compile objects into build/
gcc $FLAGS -I "$LIB_PATH" -c "$SRC_PATH/Config0.c"   -o "$BUILD_PATH/Config0.o"
gcc $FLAGS -I "$LIB_PATH" -c "$SRC_PATH/Res0.c"      -o "$BUILD_PATH/Res0.o"
gcc $FLAGS -I "$LIB_PATH" -c "$SRC_PATH/debug.c"     -o "$BUILD_PATH/debug.o"
gcc $FLAGS -I "$LIB_PATH" -c "$SRC_PATH/glueVars.c"  -o "$BUILD_PATH/glueVars.o"

# Link shared library into build/
gcc $FLAGS -shared -o "$BUILD_PATH/libplc_new.so" \
    "$BUILD_PATH/Config0.o" "$BUILD_PATH/Res0.o" "$BUILD_PATH/debug.o" "$BUILD_PATH/glueVars.o"

echo "[INFO] Build finished. Artifacts in $BUILD_PATH:"
ls -lh "$BUILD_PATH"
