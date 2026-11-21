#!/usr/bin/env bash
#
# generate-sbom.sh — Software Bill of Materials for the OpenPLC Runtime.
#
# Produces three artifacts under ./sbom/:
#   - <name>.cdx.json        CycloneDX 1.6  (canonical, security/VEX-oriented — OWASP)
#   - <name>.spdx.json       SPDX 2.3       (ISO/IEC 5962 — procurement/compliance)
#   - <name>.components.csv  flattened component list with licences
#
# The runtime has no single lockfile, so the SBOM is assembled from parts:
#   1. Python: the requirements files of the runtime venv and of every Python
#      plugin venv, resolved by pip (scripts/resolve-python-deps.sh), read by cdxgen.
#   2. Go: the bootloader module (bootloader/go.mod + go.sum), read by cdxgen.
#   3. C/C++ and fetched components no package manager declares:
#      security/components.json (vendored cJSON and Snap7, EtherDOG and SOEM,
#      the CMake release install.sh installs).
# scripts/merge-sbom.mjs combines them under one root component.
#
# Name comes from security/report-config.json (sbomBasename), version from VERSION.
# Env: PYTHON (interpreter used to resolve the Python closure, default python3).

set -euo pipefail
cd "$(dirname "$0")/.."

NAME="$(node -p "require('./security/report-config.json').sbomBasename")"
VERSION="$(sed -e 's/^v//' -e 's/[[:space:]]*$//' VERSION)"

OUT="sbom"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"

# Pinned so the SBOM is reproducible. Keep the version in ONE place and never
# paste it through a rendered web page (email obfuscation once mangled the
# `cdxgen@<version>` reference and made every run abort).
CDXGEN_VERSION="11.11.0"
cdxgen() {
  FETCH_LICENSE=true npx --yes "@cyclonedx/cdxgen@${CDXGEN_VERSION}" --spec-version 1.6 "$@"
}

echo "==> Resolving the Python dependency closure"
bash scripts/resolve-python-deps.sh . "$WORK/python"

echo "==> CycloneDX for the Python closure (cdxgen ${CDXGEN_VERSION}, -t python)"
cdxgen -t python -o "$WORK/python.cdx.json" --project-name "${NAME}-python" "$WORK/python"

echo "==> CycloneDX for the bootloader Go module (cdxgen ${CDXGEN_VERSION}, -t go)"
BOOT_VERSION="$(sed -e 's/^bootloader-v//' -e 's/[[:space:]]*$//' bootloader/VERSION)"
cdxgen -t go -o "$WORK/bootloader.cdx.json" bootloader

echo "==> Merging with the hand-maintained components (security/components.json)"
node scripts/merge-sbom.mjs \
  --out "$OUT/$NAME.cdx.json" --name "$NAME" --version "$VERSION" --license GPL-3.0-only \
  --merge "$WORK/python.cdx.json" \
  --nest "$WORK/bootloader.cdx.json" --nest-version "$BOOT_VERSION" \
  --components security/components.json

# CONTENT gate, not just a shape check: an empty or partial BOM must stop the
# run, since nothing downstream can tell it from a clean one.
echo "==> Validating the SBOM has real content"
node scripts/assert-sbom.mjs "$OUT/$NAME.cdx.json" "${MIN_COMPONENTS:-40}"

echo "==> Converting to SPDX 2.3"
node scripts/cdx-to-spdx.mjs "$OUT/$NAME.cdx.json" "$OUT/$NAME.spdx.json"

echo "==> Emitting flattened CSV"
node scripts/cdx-to-csv.mjs "$OUT/$NAME.cdx.json" "$OUT/$NAME.components.csv"

echo "==> Done. Artifacts in ./$OUT/"
ls -la "$OUT"
