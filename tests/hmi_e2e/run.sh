#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

# End-to-end test of the Runtime v4 HMI (docs/HMI.md); see README.md.
# Usage: tests/hmi_e2e/run.sh [IMAGE] [EDITOR_ROOT]
#   IMAGE        runtime image built from this tree (default openplc-runtime:p9)
#   EDITOR_ROOT  openplc-editor worktree with openplc-cli.dev.js built from it
# Env: HMI_E2E_PORT (host port, default 18543), HMI_E2E_WORK (work dir, default a temp dir),
#      HMI_E2E_KEEP=1 (leave the container and work dir in place), HMI_E2E_CONTAINER (container
#      name, replaced if it exists; default openplc-hmi-e2e), HMI_E2E_PLAYWRIGHT (path of the
#      playwright package whose installed Chromium the browser half uses; default the editor's).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="$(cd "$HERE/../.." && pwd)"
IMAGE="${1:-openplc-runtime:p9}"
EDITOR="$(cd "${2:-${OPENPLC_EDITOR:-$RUNTIME/../openplc-editor}}" && pwd)"
PORT="${HMI_E2E_PORT:-18543}"
CONTAINER="${HMI_E2E_CONTAINER:-openplc-hmi-e2e}"
PLAYWRIGHT="${HMI_E2E_PLAYWRIGHT:-$EDITOR/node_modules/playwright}"
WORK="${HMI_E2E_WORK:-$(mktemp -d "${TMPDIR:-/tmp}/hmi-e2e.XXXXXX")}"
PY="$RUNTIME/venvs/test-env/bin/python"
TARGET="OpenPLC Runtime v4"

cleanup() {
    if [ "${HMI_E2E_KEEP:-0}" != "1" ] && [ -z "${HMI_E2E_WORK:-}" ]; then rm -rf "$WORK"; fi
}
trap cleanup EXIT

[ -x "$PY" ] || { echo "no $PY: create venvs/test-env first" >&2; exit 2; }
"$PY" -c 'import socketio, requests, websocket' 2>/dev/null ||
    "$PY" -m pip install -q "python-socketio[client]" requests
[ -f "$EDITOR/openplc-cli.dev.js" ] || { echo "no $EDITOR/openplc-cli.dev.js (npm run build:cli:dev?)" >&2; exit 2; }
docker image inspect "$IMAGE" > /dev/null

cli() { (cd "$EDITOR" && npx electron ./openplc-cli.dev.js --cli "$@"); }

mkdir -p "$WORK"
echo "### work dir: $WORK"
rm -rf "$WORK/proj1" "$WORK/proj2" "$WORK/gen"
cp -R "$HERE/project" "$WORK/proj1"
cp -R "$HERE/project" "$WORK/proj2"
# The changed program of TC12 is uploaded without hmi/.
rm -rf "$WORK/proj2/hmi"
# The changed program of TC12: one more variable, so its MD5 differs.
cat > "$WORK/proj2/pous/programs/Main.st" << 'EOF'
PROGRAM Main
  VAR
    Run : BOOL;
    Level : REAL;
    Count : INT;
  END_VAR

  IF Run THEN
    Level := Level + 1.0;
  END_IF;
  Count := Count + 1;
END_PROGRAM
EOF

for p in proj1 proj2; do
    echo "### compile $p ($TARGET)"
    cli compile "$WORK/$p" --target "$TARGET" --json > "$WORK/$p.json" 2> "$WORK/$p.log" ||
        { cat "$WORK/$p.json"; tail -30 "$WORK/$p.log"; exit 1; }
done
B1="$WORK/proj1/build/$TARGET/src"
B2="$WORK/proj2/build/$TARGET/src"
grep -h PROGRAM_MD5 "$B1/defines.h" "$B2/defines.h"

# The editor's build composed hmi/ inside the bundle; the upload adds it from its own folder.
[ -f "$B1/hmi/manifest.json" ] || { echo "the build produced no hmi/" >&2; exit 1; }
[ ! -e "$B2/hmi" ] || { echo "the build without screens produced hmi/" >&2; exit 1; }
mkdir -p "$WORK/gen"
mv "$B1/hmi" "$WORK/gen/hmi"

echo "### run against $IMAGE on port $PORT"
KEEP=()
[ "${HMI_E2E_KEEP:-0}" = "1" ] && KEEP=(--keep)
"$PY" "$HERE/e2e_hmi.py" --image "$IMAGE" --port "$PORT" --bundle "$B1" --hmi "$WORK/gen" \
    --bundle2 "$B2" --vpp "$HERE/vpp_plugin" --container "$CONTAINER" --playwright "$PLAYWRIGHT" \
    --shots "$WORK/shots" "${KEEP[@]+"${KEEP[@]}"}"
