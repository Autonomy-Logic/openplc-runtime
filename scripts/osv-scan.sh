#!/usr/bin/env bash
#
# osv-scan.sh — one osv-scanner run over a source tree and its pinned Python closure.
#
#   scripts/osv-scan.sh <src-root> <resolved-dir> <out.json> [config.toml]
#
# Scans the Go modules found under <src-root> and every pinned requirements.txt
# written by resolve-python-deps.sh under <resolved-dir>. --no-resolve keeps
# osv-scanner from re-resolving those files through deps.dev: they are already
# the closure pip installs.
#
# Always leaves a valid osv-scanner JSON at <out.json>. Exit codes 0 (no
# advisories), 1 (advisories found) and 128 (nothing to scan) all count as a
# completed scan; any other code fails the run, never a silently empty result.

set -euo pipefail

SRC="${1:?usage: osv-scan.sh <src-root> <resolved-dir> <out.json> [config.toml]}"
RES="${2:?usage: osv-scan.sh <src-root> <resolved-dir> <out.json> [config.toml]}"
OUT="${3:?usage: osv-scan.sh <src-root> <resolved-dir> <out.json> [config.toml]}"
CFG="${4:-}"

args=(scan --recursive --no-resolve --format=json --output-file="$OUT")
[ -n "$CFG" ] && args+=(--config="$CFG")
if [ -d "$RES" ]; then
  while IFS= read -r f; do
    args+=(-L "requirements.txt:$f")
  done < <(find "$RES" -type f -name requirements.txt | sort)
fi
args+=("$SRC")

rm -f "$OUT"
set +e
osv-scanner "${args[@]}"
rc=$?
set -e
case "$rc" in
  0|1) ;;
  128) echo "osv-scanner found no package to scan; recording an empty result." ;;
  *) echo "::error::osv-scanner failed on $SRC (exit $rc)"; exit 1 ;;
esac
if [ ! -s "$OUT" ]; then
  [ "$rc" = "1" ] && { echo "::error::osv-scanner reported advisories but wrote no output"; exit 1; }
  echo '{"results":[]}' > "$OUT"
fi
