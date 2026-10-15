#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®
#
# Runs the MQTT transport/codec spike against a throwaway Mosquitto broker.
# Usage: tests/spikes/mqtt_transport/run.sh [pytest args]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
NET="openplc-mqtt-spike-net"
BROKER="openplc-mqtt-spike-broker"
VOL="openplc-mqtt-spike-pki"
IMAGE="openplc-mqtt-spike:local"
MOSQUITTO="eclipse-mosquitto:2.0.22@sha256:199ea8ef2e35ec2b1b37e59cfd1dbae538ed4dfa4a2251a121a52215a6248a21"

cleanup() {
    docker rm -f "$BROKER" >/dev/null 2>&1 || true
    docker volume rm -f "$VOL" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

docker build -q -t "$IMAGE" "$HERE" >/dev/null
docker network create "$NET" >/dev/null
docker volume create "$VOL" >/dev/null
docker run --rm -v "$VOL":/pki -v "$HERE":/spike:ro "$IMAGE" python /spike/pki.py /pki broker
docker run -d --name "$BROKER" --hostname broker --network "$NET" --network-alias broker -v "$VOL":/pki:ro \
    -v "$HERE/mosquitto.conf":/mosquitto/config/mosquitto.conf:ro "$MOSQUITTO" \
    sh -c 'mosquitto_passwd -b -c /mosquitto/passwd spike-user "$(cat /pki/password)" && chown mosquitto:mosquitto /mosquitto/passwd && chmod 0700 /mosquitto/passwd && exec mosquitto -c /mosquitto/config/mosquitto.conf' >/dev/null
for _ in $(seq 1 50); do
    if docker logs "$BROKER" 2>&1 | grep -q "Opening ipv4 listen socket on port 8887"; then break; fi
    sleep 0.2
done

docker run --rm --network "$NET" -v "$VOL":/pki:ro -v "$HERE":/spike:ro \
    -v "$REPO/core/src/drivers/plugins/python/mqtt/contract":/contract:ro \
    -v "$REPO/core/src/drivers/plugins/python":/plugins:ro \
    -e SPIKE_BROKER=broker -e SPIKE_PKI=/pki -e SPIKE_CONTRACT=/contract -e SPIKE_PLUGINS=/plugins \
    "$IMAGE" python -m pytest -p no:cacheprovider -o python_files='spike_*.py' -o addopts='' \
    -o testpaths='' --rootdir=/spike /spike "$@"
