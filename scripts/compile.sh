#!/bin/bash
#
# compile.sh — build the user PLC program into core/build/new_libplc.so
#
# This script is the entry point the runtime's webserver calls after
# extracting an upload into core/generated/. The actual build rules
# live in scripts/Makefile.strucpp — invoking `make` lets us:
#
#   - run per-file compilation in parallel via `-j$(nproc)` (Pi 4 = 4×,
#     workstations more);
#   - reuse cached .o files automatically when ccache is installed,
#     so incremental rebuilds (one POU edited) drop from minutes to
#     a few seconds;
#   - adapt to whatever .cpp set STruC++'s codegen split emitted —
#     the Makefile uses `wildcard $(GENERATED_DIR)/*.cpp` rather than
#     a hard-coded list.
#
# The MatIEC-era files (Config0.c, Res0.c, debug.c, glueVars.c) are
# rejected with a clear error so stale uploads fail loudly instead of
# silently building against the old pipeline.

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

# Build the program — actual rules live in scripts/Makefile.strucpp.
#
# Pick the build parallelism (`-j`) as min(CPU cap, memory cap). Both
# are real constraints on the Pi-class targets we ship to, and either
# bound alone has caused field outages.
#
# CPU cap = nproc - 1.  On a 4-core Pi 4 `-j$(nproc)` saturates every
# core with g++ and starves the webserver / runtime monitor of CPU
# during the compile; combined with the Pi's slow SD-card swap, that
# made port-8443 RST new connections for 60+ seconds while the compile
# thrashed. Reserving one core for the Flask webserver, the runtime
# monitor thread, and any plugins keeps the device responsive
# throughout — only ~25 % slower per build on a Pi 4.
#
# Memory cap = total RAM (rounded to nearest GB) — one parallel job
# per gigabyte.  Each cc1plus invocation on OpenPLC-generated TUs
# peaks at ~500–700 MB on a Pi 4.  With `-j3` on a 2 GB device,
# three concurrent cc1pluses exhaust RAM + swap and the system enters
# a swap-thrash deadlock where no compile process makes progress —
# requires a physical reboot to recover on a headless target.
# Capping at one job per GB gives ~500 MB per cc1plus + headroom for
# the kernel, the webserver, the PLC core, and the plugins.  The
# `+512` before dividing rounds MemTotal to the nearest GB so a 2 GB
# Pi (which reports ~1.8 GiB usable after kernel reserves) doesn't
# get wrongly demoted to -j1.
#
# Floor at 1 so single-core or sub-GB targets don't end up with `-j0`
# (which means "unlimited" in GNU make, i.e. fork-bomb).
#
# Worked examples:
#   Pi 4 2 GB (nproc=4): cpu=3, mem=2 → -j2
#   Pi 4 4 GB (nproc=4): cpu=3, mem=4 → -j3
#   Pi 4 8 GB (nproc=4): cpu=3, mem=8 → -j3
#   1 GB / 1 core VM:    cpu=1, mem=1 → -j1
#   Workstation 8c/16GB: cpu=7, mem=16 → -j7
CPU_JOBS=$(nproc)
[ "$CPU_JOBS" -gt 1 ] && CPU_JOBS=$((CPU_JOBS - 1))
MEM_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
MEM_MB=$((MEM_KB / 1024))
MEM_JOBS=$(( (MEM_MB + 512) / 1024 ))
[ "$MEM_JOBS" -lt 1 ] && MEM_JOBS=1
if [ "$CPU_JOBS" -lt "$MEM_JOBS" ]; then
    JOBS=$CPU_JOBS
else
    JOBS=$MEM_JOBS
fi
make -j"$JOBS" -f scripts/Makefile.strucpp

# -----------------------------------------------------------------------
# Compile VPP plugin if source is present in the uploaded project
#
# The editor ships an optional vpp_plugin/ subtree alongside the IEC
# program when the project includes a VPP package. The plugin builds
# into BUILD_PATH (next to new_libplc.so) so the runtime's plugin
# loader picks it up under the same lookup rules as built-ins.
#
# Checksum cache: skip recompilation when the source hasn't changed
# AND a previous build's .so is still present. Saves ~20-60 s per
# upload on the slowest targets.
# -----------------------------------------------------------------------
VPP_PLUGIN_DIR="$GENERATED_DIR/vpp_plugin"
VPP_CHECKSUM_FILE="$VPP_PLUGIN_DIR/checksum.sha256"
# VPP outputs land in a dedicated subdir of BUILD_PATH so the cleanup
# glob below can scope itself to VPP-only artefacts. If a future built-in
# plugin ships as a .so dropped into BUILD_PATH directly, the old
# "$BUILD_PATH/lib*_plugin.so" glob would have rm'd it on every upload
# without a vpp_plugin subtree present.
VPP_OUTPUT_DIR="$BUILD_PATH/vpp"
VPP_CACHED_CHECKSUM="$VPP_OUTPUT_DIR/checksum.sha256"

if [ -d "$VPP_PLUGIN_DIR" ] && [ -f "$VPP_PLUGIN_DIR/Makefile" ]; then
    NEEDS_COMPILE=1
    mkdir -p "$VPP_OUTPUT_DIR"

    if [ -f "$VPP_CHECKSUM_FILE" ] && [ -f "$VPP_CACHED_CHECKSUM" ]; then
        if diff -q "$VPP_CHECKSUM_FILE" "$VPP_CACHED_CHECKSUM" > /dev/null 2>&1; then
            if ls "$VPP_OUTPUT_DIR"/lib*_plugin.so 1>/dev/null 2>&1; then
                echo "[INFO] VPP plugin source unchanged (checksum match), skipping recompilation"
                NEEDS_COMPILE=0
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

        if [ -f "$VPP_CHECKSUM_FILE" ]; then
            cp "$VPP_CHECKSUM_FILE" "$VPP_CACHED_CHECKSUM"
        fi
        echo "[INFO] VPP plugin compiled successfully"
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
