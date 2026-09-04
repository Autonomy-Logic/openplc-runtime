#!/usr/bin/env bash
# Integration harness for the RTOP-283 bootloader.
#
# Runs a Debian container with its own Docker daemon (see Dockerfile.testhost),
# stands up a registry inside it, and seeds that registry with runtime images.
# The bootloader then does real pulls over a real registry, so the update path
# -- including progress streaming and layer reuse -- is exercised rather than
# stubbed.
#
# What this harness cannot cover, and what the device round is for: hardware.
# There is no /dev/spidev6.0 or /dev/gpiochip0 here, so VPP plugin behaviour
# and real SCHED_FIFO latency must be validated on an SLM-RP4.
#
# Usage:
#   ./harness.sh up      # build and start the test host
#   ./harness.sh seed    # load images and fill the inner registry
#   ./harness.sh shell   # interactive shell on the test host
#   ./harness.sh down    # tear everything down
set -euo pipefail

HOST_CONTAINER=openplc-testhost
HOST_IMAGE=openplc-testhost:latest
DOCKER_VOLUME=openplc-testhost-docker
REGISTRY=localhost:5000

# Repository the bootloader pulls from inside the harness.
STUB_REPO="$REGISTRY/openplc-stub"
REAL_REPO="$REGISTRY/openplc-runtime"

# Base for the end-to-end case against a real runtime.
#
# A published image by default, so this harness reproduces anywhere. It used to
# default to a tag that existed only on the author's machine, which meant the
# reported pass count could not be reproduced by anyone else -- and quietly
# meant the repository's own Dockerfile was never exercised.
#
# REAL_BASE=build builds from the repository Dockerfile instead. Slower by
# minutes (it is a full source install), and the only setting that covers the
# Dockerfile itself -- which is where `./install.sh` silently switching to the
# container path broke the release build.
REAL_BASE="${REAL_BASE:-ghcr.io/autonomy-logic/openplc-runtime:v4.2.1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

log() { printf '\033[0;34m[harness]\033[0m %s\n' "$*"; }
die() { printf '\033[0;31m[harness]\033[0m %s\n' "$*" >&2; exit 1; }

# inner runs a command on the test host's Docker daemon.
inner() { docker exec "$HOST_CONTAINER" "$@"; }

# inner_stdin is inner for commands that read from a pipe. `docker exec`
# does not forward stdin unless it is given -i, so without this a piped
# `docker load` silently reads an empty stream and reports
# "invalid archive: does not contain a manifest.json".
inner_stdin() { docker exec -i "$HOST_CONTAINER" "$@"; }

cmd_up() {
    log "building the test host image"
    docker build -q -f "$SCRIPT_DIR/Dockerfile.testhost" -t "$HOST_IMAGE" "$SCRIPT_DIR" >/dev/null

    docker rm -f "$HOST_CONTAINER" >/dev/null 2>&1 || true
    docker volume create "$DOCKER_VOLUME" >/dev/null

    log "starting the test host"
    # Privileged because it runs a Docker daemon. The repo is mounted
    # read-only so image builds inside can use it as a build context without
    # any risk of a test writing to the working tree.
    # The runtime and bootloader run with --network host INSIDE this
    # container, so publishing here is what lets a browser on the developer's
    # machine reach them -- which is how the editor and web UI get tested
    # against a real device without one on the desk.
    docker run -d --name "$HOST_CONTAINER" --privileged \
        -p 8443:8443 -p 8445:8445 \
        -v "$DOCKER_VOLUME":/var/lib/docker \
        -v "$REPO_ROOT":/workspace:ro \
        "$HOST_IMAGE" sleep infinity >/dev/null

    log "waiting for the inner daemon"
    for _ in $(seq 1 120); do
        if inner docker info >/dev/null 2>&1; then
            log "inner daemon ready: $(inner docker version --format '{{.Server.Version}}')"
            return 0
        fi
        sleep 0.5
    done
    docker logs "$HOST_CONTAINER" | tail -30
    die "the inner daemon did not come up"
}

# transfer pipes an EXISTING image from the host daemon into the inner one,
# skipping the work when it is already there. The real runtime image is ~1 GB,
# and re-piping it on every run would dominate the suite's runtime.
transfer() {
    local image="$1"
    if inner docker image inspect "$image" >/dev/null 2>&1; then
        log "already inside: $image"
        return 0
    fi
    docker image inspect "$image" >/dev/null 2>&1 \
        || die "$image is not present on this machine"
    log "transferring $image into the test host"
    docker save "$image" | inner_stdin docker load >/dev/null
}

# build_into_host builds an image from this repo and loads it into the inner
# daemon, ALWAYS fresh -- these are the artefacts under test, so a stale copy
# would quietly test the previous commit.
#
# buildx with `--output type=docker` rather than `docker build` + `docker save`:
# Docker 29 exports a buildx-built image as an OCI layout, and the inner
# daemon rejects that with "does not contain a manifest.json". This output type
# writes the legacy docker-archive both daemons agree on.
build_into_host() {
    local image="$1" context="$2" dockerfile="$3"
    shift 3
    local tar
    tar="$(mktemp -t openplc-img-XXXXXX).tar"
    log "building $image"
    docker buildx build \
        --output "type=docker,dest=$tar" \
        -f "$dockerfile" \
        -t "$image" \
        "$@" \
        "$context" >/dev/null 2>&1 \
        || { rm -f "$tar"; die "building $image failed"; }
    inner_stdin docker load < "$tar" >/dev/null
    rm -f "$tar"
}

cmd_seed() {
    inner docker info >/dev/null 2>&1 || die "run './harness.sh up' first"

    transfer registry:2
    build_into_host openplc-bootloader:test \
        "$REPO_ROOT/bootloader" "$REPO_ROOT/bootloader/Dockerfile" \
        --build-arg BOOTLOADER_VERSION=bootloader-v1.0.0-test
    build_into_host openplc-stubruntime:build \
        "$SCRIPT_DIR/stubruntime" "$SCRIPT_DIR/stubruntime/Dockerfile"

    log "starting the inner registry"
    inner docker rm -f registry >/dev/null 2>&1 || true
    # --network host so the bootloader, which also uses host networking, can
    # reach it on localhost:5000. Docker treats localhost registries as
    # insecure by default, so no daemon configuration is needed.
    inner docker run -d --name registry --restart always --network host registry:2 >/dev/null

    for _ in $(seq 1 60); do
        if inner curl -fsS "http://$REGISTRY/v2/" >/dev/null 2>&1; then break; fi
        sleep 0.5
    done
    inner curl -fsS "http://$REGISTRY/v2/" >/dev/null 2>&1 \
        || die "the inner registry did not come up"

    log "seeding stub runtime versions"
    # Several tags of the same tiny image. Behaviour is chosen at RUN time by
    # environment, not baked per tag, so one image covers every failure mode
    # and the pulls stay fast.
    for tag in v1.0.0 v1.0.1 v1.0.2 v0.9.0; do
        inner docker tag openplc-stubruntime:build "$STUB_REPO:$tag"
        inner docker push -q "$STUB_REPO:$tag" >/dev/null
    done

    if [ "$REAL_BASE" = build ]; then
        log "building the real runtime image from the repository Dockerfile (slow)"
        # The only path that covers the Dockerfile. Its `RUN ./install.sh` has
        # to reach the source build; when install.sh started defaulting to the
        # container path, nothing here noticed and the release build broke.
        inner docker build -q -t "$REAL_REPO:v4.2.1" /workspace >/dev/null
    else
        log "building the real runtime image from $REAL_BASE"
        # A thin layer over a published runtime, carrying the webserver files
        # this ticket touches. Seconds instead of a full source install.
        transfer "$REAL_BASE"
        inner sh -c "cat > /tmp/real.Dockerfile <<'EOF'
FROM $REAL_BASE
COPY webserver/restapi.py webserver/app.py /workdir/webserver/
HEALTHCHECK --interval=10s --timeout=10s --start-period=90s --retries=3 \\
    CMD curl -kfsS https://127.0.0.1:8443/api/version >/dev/null || exit 1
EOF
docker build -q -f /tmp/real.Dockerfile -t $REAL_REPO:v4.2.1 /workspace >/dev/null"
    fi
    inner docker push -q "$REAL_REPO:v4.2.1" >/dev/null

    log "registry contents:"
    inner curl -fsS "http://$REGISTRY/v2/_catalog" | tr -d '\n'; echo
    for repo in openplc-stub openplc-runtime; do
        printf '  %s: ' "$repo"
        inner curl -fsS "http://$REGISTRY/v2/$repo/tags/list" | tr -d '\n'; echo
    done
}

cmd_shell() {
    exec docker exec -it "$HOST_CONTAINER" bash
}

cmd_down() {
    log "tearing down"
    docker rm -f "$HOST_CONTAINER" >/dev/null 2>&1 || true
    if [ "${KEEP_VOLUME:-0}" != "1" ]; then
        docker volume rm -f "$DOCKER_VOLUME" >/dev/null 2>&1 || true
    else
        log "keeping $DOCKER_VOLUME (KEEP_VOLUME=1)"
    fi
}

case "${1:-}" in
    up)    cmd_up ;;
    seed)  cmd_seed ;;
    shell) cmd_shell ;;
    down)  cmd_down ;;
    *)     die "usage: $0 {up|seed|shell|down}" ;;
esac
