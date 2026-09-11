#!/usr/bin/env bash
# Start the inner Docker daemon, then hand over to the command.
#
# The daemon has to be up before anything else runs, and "up" means the socket
# answers -- not merely that dockerd was spawned. Racing it is the classic way
# an integration harness fails intermittently and gets blamed on the code under
# test.
set -euo pipefail

log() { printf '[testhost] %s\n' "$*" >&2; }

# Marks this container as the disposable host the integration suite may wipe.
# test_bootloader.py refuses to run without it, so the suite cannot destroy a
# real device's data if it is ever collected somewhere it should not be.
mkdir -p /run && : > /run/openplc-testhost

if [ ! -w /var/run ]; then
    log "ERROR: /var/run is not writable; the container needs --privileged"
    exit 1
fi

log "starting dockerd"
dockerd >/var/log/dockerd.log 2>&1 &
DOCKERD_PID=$!

# Wait for the socket to actually respond. 60s: a cold vfs daemon on a laptop
# under load takes longer than the couple of seconds it usually needs.
for _ in $(seq 1 120); do
    if docker info >/dev/null 2>&1; then
        log "dockerd ready ($(docker version --format '{{.Server.Version}}'))"
        break
    fi
    if ! kill -0 "$DOCKERD_PID" 2>/dev/null; then
        log "ERROR: dockerd exited during start-up. Last lines:"
        tail -30 /var/log/dockerd.log >&2 || true
        exit 1
    fi
    sleep 0.5
done

if ! docker info >/dev/null 2>&1; then
    log "ERROR: dockerd did not become ready. Last lines:"
    tail -30 /var/log/dockerd.log >&2 || true
    exit 1
fi

exec "$@"
