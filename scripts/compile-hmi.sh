#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

# Builds build/hmi/ (plugin, client copy, hmi.seal) from core/generated/hmi/, or
# removes it when there is none (docs/HMI.md). Run from the runtime root. The
# plugin is the runtime's HMI host linked with the upload's manifest data
# (hmi/src/hmi_access.c); screens run in the browser, not here. The build goes
# to build/hmi.new and replaces build/hmi only when it succeeds.

# HMI_CC: C compiler (gcc); HMI_EXTRA_FLAGS: added to every compile;
# HMI_SRC_ROOT: runtime source tree (default .).

set -euo pipefail

GENERATED_DIR="${GENERATED_DIR:-core/generated}"
HMI_GEN="$GENERATED_DIR/hmi"
HMI_DEST="build/hmi"
HMI_OUT="build/hmi.new"
SRC_ROOT="${HMI_SRC_ROOT:-.}"
HOST_DIR="$SRC_ROOT/core/src/drivers/plugins/native/hmi"
DRIVERS_INC="$SRC_ROOT/core/src/drivers"
CC="${HMI_CC:-gcc}"
EXTRA_FLAGS="${HMI_EXTRA_FLAGS:-}"
CLIENT_NAME_RE='^[A-Za-z0-9._-]{1,64}$'
CLIENT_EXT_RE='\.(html|js|css|wasm|txt|ttf)$'

sha256_hex() {
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum "$1" | awk '{ sub(/^\\/, "", $1); print $1; exit }'
    elif command -v shasum > /dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{ sub(/^\\/, "", $1); print $1; exit }'
    else
        return 1
    fi
}

rm -rf "$HMI_OUT"
if [ ! -d "$HMI_GEN" ]; then
    if [ -d "$HMI_DEST" ]; then
        echo "[INFO] No HMI in this program, removing $HMI_DEST"
        rm -rf "$HMI_DEST"
    fi
    exit 0
fi

if [ ! -f "$HMI_GEN/manifest.json" ] || [ ! -d "$HMI_GEN/src" ]; then
    echo "[ERROR] $HMI_GEN has no manifest.json or no src/ folder." >&2
    exit 4
fi

# A failed build leaves no partial plugin, client or seal behind, and the installed HMI as it was.
trap 'rc=$?; [ "$rc" -ne 0 ] && rm -rf "$HMI_OUT"; exit "$rc"' EXIT
OBJ_DIR="$HMI_OUT/obj"
mkdir -p "$OBJ_DIR" "$HMI_OUT/client"

COMMON_FLAGS=(-std=c11 -O2 -fPIC -fvisibility=hidden -pthread -Wall -Wextra)
# shellcheck disable=SC2206
[ -n "$EXTRA_FLAGS" ] && COMMON_FLAGS+=($EXTRA_FLAGS)

INCLUDES=(-I "$HOST_DIR" -I "$DRIVERS_INC" -I "$HMI_GEN/src")

OBJS=()
compile_one() {
    local src="$1" obj="$2"
    mkdir -p "$(dirname "$obj")"
    "$CC" "${COMMON_FLAGS[@]}" "${INCLUDES[@]}" -c "$src" -o "$obj"
    OBJS+=("$obj")
}

echo "[INFO] Compiling the HMI plugin..."
for src in "$HOST_DIR"/*.c; do
    compile_one "$src" "$OBJ_DIR/host/$(basename "${src%.*}").o"
done
SRC_COUNT=0
while IFS= read -r -d '' src; do
    rel="${src#"$HMI_GEN/src/"}"
    compile_one "$src" "$OBJ_DIR/src/${rel%.*}.o"
    SRC_COUNT=$((SRC_COUNT + 1))
done < <(find "$HMI_GEN/src" -maxdepth 1 -type f -name '*.c' -print0 | sort -z)
if [ "$SRC_COUNT" -eq 0 ]; then
    echo "[ERROR] $HMI_GEN/src has no sources." >&2
    rm -rf "$HMI_OUT"
    exit 4
fi

LINK_FLAGS=(-shared -pthread)
# Unresolved symbols fail the build here rather than the dlopen on the device.
[ "$(uname -s)" = "Linux" ] && LINK_FLAGS+=(-Wl,-z,defs)
"$CC" "${LINK_FLAGS[@]}" -o "$HMI_OUT/libhmi_plugin.so" "${OBJS[@]}"
rm -rf "$OBJ_DIR"

if [ -d "$HMI_GEN/client" ]; then
    for f in "$HMI_GEN/client"/* "$HMI_GEN/client"/.[!.]*; do
        [ -e "$f" ] || continue
        name="$(basename "$f")"
        if [ ! -f "$f" ] || [ -L "$f" ] || ! [[ "$name" =~ $CLIENT_NAME_RE ]] ||
            ! [[ "$name" =~ $CLIENT_EXT_RE ]]; then
            echo "[ERROR] HMI client entry '$name' is not an allowed file." >&2
            rm -rf "$HMI_OUT"
            exit 4
        fi
        cp "$f" "$HMI_OUT/client/$name"
    done
fi

# Seal: one line per file, "<sha256>  <path relative to build/hmi>". plc_main
# checks the plugin's line before dlopen; the web server checks client files.
SEAL_TMP="$HMI_OUT/hmi.seal.tmp"
: > "$SEAL_TMP"
seal_file() {
    local rel="$1" hash
    if ! hash=$(sha256_hex "$HMI_OUT/$rel"); then
        echo "[ERROR] Cannot hash $HMI_OUT/$rel (no sha256sum/shasum on PATH)." >&2
        rm -rf "$HMI_OUT"
        exit 3
    fi
    printf '%s  %s\n' "$hash" "$rel" >> "$SEAL_TMP"
}
seal_file "libhmi_plugin.so"
for f in "$HMI_OUT/client"/*; do
    [ -f "$f" ] || continue
    seal_file "client/$(basename "$f")"
done
mv "$SEAL_TMP" "$HMI_OUT/hmi.seal"
rm -rf "$HMI_DEST.old"
[ -d "$HMI_DEST" ] && mv "$HMI_DEST" "$HMI_DEST.old"
mv "$HMI_OUT" "$HMI_DEST"
rm -rf "$HMI_DEST.old"
echo "[INFO] HMI plugin built and sealed ($SRC_COUNT manifest sources)"
