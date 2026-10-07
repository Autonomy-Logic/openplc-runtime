#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# compile.sh builds the user PLC program into core/build/new_libplc.so.
# Entry point from the webserver after it extracts core/generated/.
# MatIEC-era files are rejected explicitly so stale uploads fail loudly.

set -euo pipefail

GENERATED_DIR="core/generated"
RUNTIME_SHIM="core/strucpp_runtime/runtime_v4_entry.cpp"
RUNTIME_INC="$GENERATED_DIR/strucpp_runtime/include"

# Output path the Makefile drops new_libplc.so into. Also where any
# user-supplied VPP plugin .so will land so the runtime's plugin
# loader can pick them up under the same lookup rules.
BUILD_PATH="build"

check_required_files() {
    if [ -f "$GENERATED_DIR/Config0.c" ] || [ -f "$GENERATED_DIR/glueVars.c" ]; then
        echo "[ERROR] core/generated contains MatIEC files (Config0.c / glueVars.c)." >&2
        echo "        This runtime no longer supports MatIEC programs."              >&2
        echo "        Re-export the project from a STruC++-aware editor build."      >&2
        exit 2
    fi

    local missing=()
    [ -f "$GENERATED_DIR/generated.hpp" ] || missing+=("$GENERATED_DIR/generated.hpp")
    [ -n "$(ls "$GENERATED_DIR"/*.cpp 2>/dev/null)" ] || missing+=("$GENERATED_DIR/*.cpp (at least one)")
    [ -f "$RUNTIME_SHIM" ]                || missing+=("$RUNTIME_SHIM")
    [ -d "$RUNTIME_INC" ]                 || missing+=("$RUNTIME_INC (directory)")

    if [ ${#missing[@]} -ne 0 ]; then
        echo "[ERROR] Missing required source files:" >&2
        printf '  %s\n' "${missing[@]}"               >&2
        exit 1
    fi
}

check_required_files

# Build parallelism = min(nproc-1, RAM_GB). The CPU bound keeps the
# webserver responsive; the memory bound avoids swap-thrash from
# cc1plus peaks (~500-700 MB each on Pi-class targets). Floor at 1.
CPU_JOBS=$(nproc)
[ "$CPU_JOBS" -gt 1 ] && CPU_JOBS=$((CPU_JOBS - 1))
MEM_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
MEM_MB=$((MEM_KB / 1024))
# Round to nearest GB so a 2 GB Pi (~1.8 GiB) does not demote to -j1.
MEM_JOBS=$(( (MEM_MB + 512) / 1024 ))
# Floor at 1: -j0 in GNU make means unlimited.
[ "$MEM_JOBS" -lt 1 ] && MEM_JOBS=1
if [ "$CPU_JOBS" -lt "$MEM_JOBS" ]; then
    JOBS=$CPU_JOBS
else
    JOBS=$MEM_JOBS
fi
make -j"$JOBS" -f scripts/Makefile.strucpp

# Build the optional VPP plugin subtree. checksum.sha256 is the
# recompilation cache key; the editor writes it, this build compares.
VPP_PLUGIN_DIR="$GENERATED_DIR/vpp_plugin"
VPP_CHECKSUM_FILE="$VPP_PLUGIN_DIR/checksum.sha256"
# VPP outputs in a dedicated subdir so cleanup can scope to VPP-only
# artefacts without touching other plugins dropped into BUILD_PATH.
VPP_OUTPUT_DIR="$BUILD_PATH/vpp"
VPP_CACHED_CHECKSUM="$VPP_OUTPUT_DIR/checksum.sha256"
# Seal the loader checks before dlopen (core/src/drivers/vpp_plugin_seal.c).
VPP_OBJECT_SEAL="$VPP_OUTPUT_DIR/vpp_plugin.seal"

# sha256 of a file, hex only. sha256sum (coreutils) on Linux targets, shasum on
# hosts that ship the Perl tool instead. No tool means no verification, and a
# security gate that cannot run must not be silently skipped -- so the caller
# fails the build instead.
sha256_hex() {
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum "$1" | awk '{ sub(/^\\/, "", $1); print $1; exit }'
    elif command -v shasum > /dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{ sub(/^\\/, "", $1); print $1; exit }'
    else
        return 1
    fi
}

# Every lib*_plugin.so on disk must hash to a line in the seal (review
# 2026-08-20, R3). Decides whether a checksum cache hit may skip the
# compile: objects the seal does not vouch for force a rebuild.
vpp_object_seal_matches() {
    [ -f "$VPP_OBJECT_SEAL" ] || return 1
    local so so_hash
    for so in "$VPP_OUTPUT_DIR"/lib*_plugin.so; do
        [ -f "$so" ] || continue
        so_hash=$(sha256_hex "$so") || return 1
        grep -Fxq "$so_hash  $(basename "$so")" "$VPP_OBJECT_SEAL" || return 1
    done
    return 0
}

if [ -d "$VPP_PLUGIN_DIR" ] && [ -f "$VPP_PLUGIN_DIR/Makefile" ]; then
    NEEDS_COMPILE=1
    mkdir -p "$VPP_OUTPUT_DIR"

    if [ -f "$VPP_CHECKSUM_FILE" ] && [ -f "$VPP_CACHED_CHECKSUM" ]; then
        if diff -q "$VPP_CHECKSUM_FILE" "$VPP_CACHED_CHECKSUM" > /dev/null 2>&1; then
            if ls "$VPP_OUTPUT_DIR"/lib*_plugin.so 1>/dev/null 2>&1; then
                # Cache hit only stands when the SEAL vouches for the
                # on-disk objects; otherwise rebuild from the uploaded
                # tree so unknown bytes are never blessed.
                if vpp_object_seal_matches; then
                    echo "[INFO] VPP plugin source unchanged (checksum match), skipping recompilation"
                    NEEDS_COMPILE=0
                else
                    echo "[INFO] Object seal missing or stale for cached .so -- recompiling from the uploaded tree"
                fi
            fi
        fi
    fi

    if [ "$NEEDS_COMPILE" -eq 1 ]; then
        echo "[INFO] Compiling VPP plugin from $VPP_PLUGIN_DIR..."
        PLUGIN_INCLUDE="-I $(pwd)/core/src/drivers -I $(pwd)/core/src/drivers/plugins/native -I $(pwd)/core/src/drivers/plugins/native/cjson -I $(pwd)/core/src/plc_app -I $(pwd)/core/lib"
        make -C "$VPP_PLUGIN_DIR" \
            INCLUDE_DIRS="$PLUGIN_INCLUDE" \
            OUTPUT_DIR="$(pwd)/$VPP_OUTPUT_DIR" \
            RUNTIME_ROOT="$(pwd)"

        # Save the uploader's checksum file as the cache key for the next
        # upload. Cache only -- see the header comment above: this is not, and
        # never was, an integrity record.
        if [ -f "$VPP_CHECKSUM_FILE" ]; then
            cp "$VPP_CHECKSUM_FILE" "$VPP_CACHED_CHECKSUM"
        fi
        echo "[INFO] VPP plugin compiled successfully"
    fi

    # Seal every .so this build produced so the loader can refuse an
    # object swapped in after compile. Only on compile paths: sealing a
    # cache-hit would bless whatever bytes already sat in build/vpp/.
    if [ "$NEEDS_COMPILE" -eq 1 ]; then
    : > "$VPP_OBJECT_SEAL"
    for so in "$VPP_OUTPUT_DIR"/lib*_plugin.so; do
        [ -f "$so" ] || continue
        if ! so_hash=$(sha256_hex "$so"); then
            echo "[ERROR] Cannot hash $so (no sha256sum/shasum on PATH)." >&2
            rm -f "$VPP_OBJECT_SEAL"
            exit 3
        fi
        printf '%s  %s\n' "$so_hash" "$(basename "$so")" >> "$VPP_OBJECT_SEAL"
        echo "[INFO] Sealed $(basename "$so") (${so_hash:0:12}...)"
    done
    fi
else
    # No VPP plugin in this upload — clean up the entire VPP output dir
    # so a stale .so doesn't get picked up by the loader. Scoping the rm
    # to VPP_OUTPUT_DIR (instead of a glob in BUILD_PATH) keeps cleanup
    # isolated from anything else that ends up in BUILD_PATH.
    if [ -d "$VPP_OUTPUT_DIR" ]; then
        echo "[INFO] No VPP plugin in upload, removing $VPP_OUTPUT_DIR"
        rm -rf "$VPP_OUTPUT_DIR"
    fi
fi
