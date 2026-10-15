#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®
#
# Builds the MQTT end-to-end upload bundles with the Editor's real compile pipeline.
# Usage: generate.sh <openplc-editor checkout with node_modules> <output dir>
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDITOR="$(cd "$1" && pwd)"
OUT="$(mkdir -p "$2" && cd "$2" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mqtt-e2e-jest.XXXXXX")"
CONFIG="$WORK/jest.config.json"
trap 'rm -rf "$WORK"' EXIT

python3 - "$EDITOR" "$HERE" "$CONFIG" <<'PY'
import json
import sys

editor, here, target = sys.argv[1:4]
with open(f"{editor}/jest.config.json", encoding="utf-8") as handle:
    config = json.load(handle)
config.update(
    {
        "rootDir": editor,
        "roots": [here],
        "testMatch": [f"{here}/*.test.ts"],
        "testPathIgnorePatterns": ["/node_modules/"],
        "testEnvironment": "node",
        "collectCoverage": False,
    }
)
config["transform"] = {
    "\\.(ts|tsx|js|jsx)$": ["ts-jest", {"tsconfig": f"{editor}/tsconfig.json", "diagnostics": False}]
}
config.pop("coverageThreshold", None)
with open(target, "w", encoding="utf-8") as handle:
    json.dump(config, handle)
PY

cd "$EDITOR"
MQTT_E2E_PAYLOAD_OUT="$OUT" npx jest --config "$CONFIG" --runInBand
