#!/usr/bin/env bash
#
# resolve-python-deps.sh — pin the Python dependency closure of a source tree.
#
#   scripts/resolve-python-deps.sh <src-root> <out-dir>
#
# The runtime's requirements files are not lockfiles: most entries are unpinned
# and install.sh / manage_plugin_venvs.sh resolve them with pip at install time.
# Neither osv-scanner nor cdxgen can tell which versions that yields, so this
# script asks pip for the same resolution (dry run, nothing installed) and writes
# one fully pinned file per manifest:
#
#   <out-dir>/requirements.txt                                  (runtime venv)
#   <out-dir>/core/src/drivers/plugins/python/<p>/requirements.txt  (plugin venvs)
#
# Each manifest is resolved on its own, as each plugin gets its own venv.
# A manifest that pip cannot resolve fails the run: an unresolved closure must
# never look like a clean one.
#
# Env: PYTHON (default python3) — the interpreter whose version and platform the
#      resolution targets.

set -euo pipefail

SRC="${1:?usage: resolve-python-deps.sh <src-root> <out-dir>}"
OUT="${2:?usage: resolve-python-deps.sh <src-root> <out-dir>}"
PY="${PYTHON:-python3}"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
cd "$SRC"

shopt -s nullglob
manifests=()
[ -f requirements.txt ] && manifests+=(requirements.txt)
for m in core/src/drivers/plugins/python/*/requirements.txt; do manifests+=("$m"); done
shopt -u nullglob

if [ "${#manifests[@]}" -eq 0 ]; then
  echo "No Python manifest under $SRC; nothing to resolve."
  exit 0
fi

TOOLS="$(mktemp -d)"
trap 'rm -rf "$TOOLS"' EXIT
"$PY" -m venv "$TOOLS/venv"
"$TOOLS/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade "pip>=24"

for m in "${manifests[@]}"; do
  echo "==> Resolving $m"
  report="$TOOLS/report.json"
  rm -f "$report"
  "$TOOLS/venv/bin/pip" install --quiet --disable-pip-version-check --dry-run \
    --ignore-installed --report "$report" -r "$m"
  mkdir -p "$OUT/$(dirname "$m")"
  "$TOOLS/venv/bin/python" - "$report" "$OUT/$m" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    report = json.load(f)
pins = sorted(
    {f"{i['metadata']['name']}=={i['metadata']['version']}" for i in report.get("install", [])},
    key=str.lower,
)
with open(sys.argv[2], "w", encoding="utf-8") as f:
    f.write("".join(p + "\n" for p in pins))
print(f"    {len(pins)} pinned packages -> {sys.argv[2]}")
PY
done
