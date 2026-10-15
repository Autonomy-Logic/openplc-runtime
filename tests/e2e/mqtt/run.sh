#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®
#
# MQTT end-to-end tests against the runtime image built from this checkout.
# Usage: tests/e2e/mqtt/run.sh <openplc-editor checkout> [pytest args]
# Env: MQTT_E2E_RUNTIME_IMAGE (default openplc-runtime:mqtt-e2e, built here when absent),
#      MQTT_E2E_MOSQUITTO (broker image), MQTT_E2E_LOG_DIR (keeps the runtime log).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
EDITOR="$1"
shift
IMAGE="${MQTT_E2E_RUNTIME_IMAGE:-openplc-runtime:mqtt-e2e}"
PAYLOADS="$(mktemp -d "${TMPDIR:-/tmp}/mqtt-e2e-payloads.XXXXXX")"
trap 'rm -rf "$PAYLOADS"' EXIT

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" "$REPO"
fi
"$HERE/payload/generate.sh" "$EDITOR" "$PAYLOADS"

cd "$REPO"
MQTT_E2E_RUNTIME_IMAGE="$IMAGE" MQTT_E2E_PAYLOADS="$PAYLOADS" \
    "${PYTHON:-venvs/test-env/bin/python}" -m pytest -p no:cacheprovider -o addopts='' \
    -o log_cli=false --rootdir "$HERE" "$HERE" "$@"
