#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

# HMI plugin tests per compiler pair: compile-hmi.sh with -Werror (exports, seal),
# then test_hmi_plugin.cpp plain and under ASan/UBSan when available.

# HMI_UNIT_DIR: the editor's resources/sources/hmi/unit, which holds hmi_access.h
# (default ../openplc-editor/resources/sources/hmi/unit).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HERE="$ROOT/tests/host/hmi"
UNIT_DIR="${HMI_UNIT_DIR:-$ROOT/../openplc-editor/resources/sources/hmi/unit}"
if [ ! -f "$UNIT_DIR/hmi_access.h" ]; then
    echo "hmi_access.h not found in $UNIT_DIR; set HMI_UNIT_DIR to the editor's resources/sources/hmi/unit" >&2
    exit 2
fi
UNIT_DIR="$(cd "$UNIT_DIR" && pwd)"

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

PAIRS=()
for pair in "gcc:g++" "clang:clang++"; do
    cc="${pair%%:*}"
    cxx="${pair#*:}"
    command -v "$cc" > /dev/null 2>&1 && command -v "$cxx" > /dev/null 2>&1 || continue
    # Apple's gcc is clang; test it once.
    if [ "$cc" = gcc ] && "$cc" --version 2>/dev/null | grep -qi clang; then
        continue
    fi
    PAIRS+=("$pair")
done
if [ ${#PAIRS[@]} -eq 0 ]; then
    echo "no C/C++ compiler pair found" >&2
    exit 2
fi

failures=0
fail() {
    echo "  FAIL  $1"
    failures=$((failures + 1))
}

make_generated() {
    local root="$1"
    mkdir -p "$root/core/generated/hmi/src" "$root/core/generated/hmi/client"
    cp "$UNIT_DIR/hmi_access.h" "$HERE/fake_access/hmi_access.c" "$root/core/generated/hmi/src/"
    printf '{"format": 2}\n' > "$root/core/generated/hmi/manifest.json"
    printf '<!doctype html><title>HMI</title>\n' > "$root/core/generated/hmi/client/index.html"
    printf 'export {};\n' > "$root/core/generated/hmi/client/hmi.js"
}

for pair in "${PAIRS[@]}"; do
    cc="${pair%%:*}"
    cxx="${pair#*:}"
    printf '\n=== %s / %s ===\n' "$cc" "$cxx"

    # 1. compile-hmi.sh
    build_root="$OUT/build-$cc"
    make_generated "$build_root"
    if (cd "$build_root" && HMI_SRC_ROOT="$ROOT" HMI_CC="$cc" \
        HMI_EXTRA_FLAGS="-Werror" bash "$ROOT/scripts/compile-hmi.sh" > "$OUT/compile-$cc.log" 2>&1); then
        echo "  ok    compile-hmi.sh builds with -Wall -Wextra -Werror"
        so="$build_root/build/hmi/libhmi_plugin.so"
        seal="$build_root/build/hmi/hmi.seal"
        exports=$(nm -g "$so" 2>/dev/null | awk '$2 == "T" { sub(/^_/, "", $3); print $3 }' | sort | tr '\n' ' ')
        for sym in init start_loop stop_loop cleanup set_image_sizes; do
            case " $exports " in
                *" $sym "*) ;;
                *) fail "$sym is not exported" ;;
            esac
        done
        if nm -g "$so" | awk '$2 == "T"' | grep -q ' _\{0,1\}hmi_values_offer$'; then
            fail "host symbols are exported"
        else
            echo "  ok    only the plugin interface is exported"
        fi
        if [ "$(wc -l < "$seal" | tr -d ' ')" = 3 ] &&
            grep -q '  libhmi_plugin.so$' "$seal" && grep -q '  client/index.html$' "$seal" &&
            grep -q '  client/hmi.js$' "$seal"; then
            echo "  ok    hmi.seal covers the plugin and both client files"
        else
            fail "hmi.seal content"
        fi
        if (cd "$build_root/build/hmi" && while read -r hash rel; do
            actual=$(shasum -a 256 "$rel" 2>/dev/null || sha256sum "$rel")
            [ "${actual%% *}" = "$hash" ] || exit 1
        done < hmi.seal); then
            echo "  ok    seal hashes match the files"
        else
            fail "seal hashes"
        fi
        sealed_before=$(cat "$seal")
        printf 'this is not C\n' > "$build_root/core/generated/hmi/src/broken.c"
        if (cd "$build_root" && HMI_SRC_ROOT="$ROOT" HMI_CC="$cc" \
            bash "$ROOT/scripts/compile-hmi.sh" > /dev/null 2>&1); then
            fail "a broken HMI source builds"
        elif [ "$(cat "$seal" 2>/dev/null)" = "$sealed_before" ] && [ -f "$so" ] &&
            [ ! -e "$build_root/build/hmi.new" ]; then
            echo "  ok    a failed build leaves the installed HMI as it was"
        else
            fail "a failed build changed the installed HMI"
        fi
        rm -f "$build_root/core/generated/hmi/src/broken.c"
        rm -rf "$build_root/core/generated/hmi"
        (cd "$build_root" && HMI_SRC_ROOT="$ROOT" bash "$ROOT/scripts/compile-hmi.sh" > /dev/null)
        if [ -e "$build_root/build/hmi" ]; then
            fail "build/hmi kept without core/generated/hmi"
        else
            echo "  ok    build/hmi removed when the program has no HMI"
        fi
    else
        cat "$OUT/compile-$cc.log"
        fail "compile-hmi.sh with $cc"
    fi

    # 2. socket test, plain and sanitized
    variants=("plain:")
    probe="$OUT/probe-$cc.c"
    printf 'int main(void){return 0;}\n' > "$probe"
    if "$cc" -fsanitize=address,undefined "$probe" -o "$OUT/probe-$cc" > /dev/null 2>&1; then
        variants+=("asan:-fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all")
    elif "$cc" -fsanitize=address "$probe" -o "$OUT/probe-$cc" > /dev/null 2>&1; then
        echo "  note  UndefinedBehaviorSanitizer not available with $cc; AddressSanitizer only"
        variants+=("asan:-fsanitize=address -fno-omit-frame-pointer")
    else
        echo "  skip  sanitizers not available with $cc"
    fi
    for variant in "${variants[@]}"; do
        vname="${variant%%:*}"
        vflags="${variant#*:}"
        objdir="$OUT/obj-$cc-$vname"
        mkdir -p "$objdir"
        sockdir="$OUT/hmi-$cc-$vname"
        # shellcheck disable=SC2206
        flags=(-g -O1 -Wall -Wextra -Werror -ffp-contract=off -pthread $vflags
            -I "$ROOT/core/src/drivers/plugins/native/hmi" -I "$ROOT/core/src/drivers"
            -I "$UNIT_DIR" "-DHMI_SOCKET_DIR=\"$sockdir\""
            "-DHMI_SOCKET_PATH=\"$sockdir/hmi.socket\"")
        objs=()
        ok=1
        for src in "$ROOT"/core/src/drivers/plugins/native/hmi/*.c "$HERE"/fake_access/hmi_access.c; do
            obj="$objdir/$(basename "$(dirname "$src")")-$(basename "${src%.c}").o"
            "$cc" -std=c11 "${flags[@]}" -c "$src" -o "$obj" || { ok=0; break; }
            objs+=("$obj")
        done
        if [ "$ok" -eq 1 ]; then
            "$cxx" -std=c++17 "${flags[@]}" "$HERE/test_hmi_plugin.cpp" "${objs[@]}" \
                -o "$objdir/test_hmi_plugin" || ok=0
        fi
        if [ "$ok" -ne 1 ]; then
            fail "test_hmi_plugin ($vname) did not compile with $cc"
            continue
        fi
        echo "  --- test_hmi_plugin ($vname)"
        if ! "$objdir/test_hmi_plugin"; then
            fail "test_hmi_plugin ($vname) with $cc"
        fi
    done

    # 3. manifest checks: every bad manifest leaves the HMI out of date; the valid base is live.
    echo "  --- test_hmi_bad_manifest"
    badobj="$OUT/bad-$cc"
    mkdir -p "$badobj"
    sockdir="$OUT/hmi-$cc-bad"
    # shellcheck disable=SC2206
    flags=(-g -O1 -Wall -Wextra -Werror -pthread -I "$ROOT/core/src/drivers/plugins/native/hmi"
        -I "$ROOT/core/src/drivers" -I "$UNIT_DIR" "-DHMI_SOCKET_DIR=\"$sockdir\""
        "-DHMI_SOCKET_PATH=\"$sockdir/hmi.socket\"")
    hostobjs=()
    for src in "$ROOT"/core/src/drivers/plugins/native/hmi/*.c; do
        obj="$badobj/$(basename "${src%.c}").o"
        "$cc" -std=c11 "${flags[@]}" -c "$src" -o "$obj" || { fail "host did not compile for the manifest test"; continue 2; }
        hostobjs+=("$obj")
    done
    for bad in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14; do
        "$cc" -std=c11 "${flags[@]}" "-DHMI_BAD=$bad" -c "$HERE/fake_access/hmi_access_bad.c" \
            -o "$badobj/manifest-$bad.o" &&
            "$cxx" -std=c++17 "${flags[@]}" "-DHMI_BAD=$bad" "$HERE/test_hmi_bad_manifest.cpp" \
                "${hostobjs[@]}" "$badobj/manifest-$bad.o" -o "$badobj/test-$bad" &&
            "$badobj/test-$bad" || fail "manifest variant $bad with $cc"
    done
done

printf '\n'
if [ "$failures" -eq 0 ]; then
    echo "hmi tests: all passed"
else
    echo "hmi tests: $failures failed"
    exit 1
fi
