#!/usr/bin/env bash
# Docker-based install of the OpenPLC Runtime (RTOP-283).
#
# This is what `sudo ./install.sh` does by default. It installs no build
# toolchain and compiles nothing: it ensures a container engine, writes the
# bootloader's spec, and starts the bootloader. The bootloader then pulls the
# runtime image and brings it up.
#
# Docker is the ONLY dependency this path adds. Nothing of ours goes into
# systemd -- Docker's own restart policy starts the bootloader at boot, and the
# bootloader starts the runtime. That is deliberate: the fewer moving parts
# between power-on and a running PLC, the fewer ways it fails.
#
# `sudo ./install.sh --native` keeps the source build for MSYS2 and for targets
# that cannot host a container engine.
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# --- defaults ------------------------------------------------------------

RUNTIME_REPOSITORY="${RUNTIME_REPOSITORY:-ghcr.io/autonomy-logic/openplc-runtime}"
BOOTLOADER_REPOSITORY="${BOOTLOADER_REPOSITORY:-ghcr.io/autonomy-logic/openplc-runtime-bootloader}"
RUNTIME_VERSION="${RUNTIME_VERSION:-}"
BOOTLOADER_VERSION="${BOOTLOADER_VERSION:-latest}"

# Two directories, deliberately separate.
#
# The runtime's holds everything a version change must preserve: .env,
# restapi.db, retain.bin, vpp/ licences and the stored project.
#
# The bootloader's holds the container spec, including this board's device
# mounts. It is separate because "erase all data" wipes the runtime's
# directory, and a board that came back from a data wipe with no SPI would be
# a miserable failure mode.
RUNTIME_DATA_DIR="${RUNTIME_DATA_DIR:-/var/lib/openplc-runtime}"
BOOTLOADER_STATE_DIR="${BOOTLOADER_STATE_DIR:-/var/lib/openplc-bootloader}"

BOOTLOADER_CONTAINER=openplc-bootloader
RUNTIME_CONTAINER=openplc-runtime
BOOTLOADER_PORT="${BOOTLOADER_PORT:-8445}"

# Extra bind mounts and env for the runtime container, gathered from --mount
# and --env. Board-specific needs land here rather than in a rebuilt image.
declare -a EXTRA_MOUNTS=()
declare -a EXTRA_ENV=()

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [options]

  --native                    Build and install from source instead (today's path)
  --runtime-version VERSION   Runtime image tag to install (default: the VERSION file)
  --bootloader-version VER    Bootloader image tag (default: latest)
  --mount HOST:CONTAINER[:ro] Extra bind mount for the runtime; repeatable
  --env KEY=VALUE             Extra environment variable for the runtime; repeatable
  --data-dir PATH             Runtime persistent data directory
  --port PORT                 Bootloader control port (default: 8445)
  -h, --help                  Show this help

Re-running is safe: it rewrites the spec and restarts the bootloader without
touching runtime data, so adding a mount does not mean reinstalling anything.
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --runtime-version)    RUNTIME_VERSION="$2"; shift 2 ;;
            --bootloader-version) BOOTLOADER_VERSION="$2"; shift 2 ;;
            --mount)              EXTRA_MOUNTS+=("$2"); shift 2 ;;
            --env)                EXTRA_ENV+=("$2"); shift 2 ;;
            --data-dir)           RUNTIME_DATA_DIR="$2"; shift 2 ;;
            --port)               BOOTLOADER_PORT="$2"; shift 2 ;;
            -h|--help)            usage; exit 0 ;;
            *) log_error "unknown option: $1"; usage; exit 1 ;;
        esac
    done
}

# --- engine --------------------------------------------------------------

detect_engine() {
    if command -v docker >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

install_engine() {
    log_info "Docker not found; installing it"

    # Docker's own convenience script rather than distro packages: it covers
    # every distro this runtime targets and always installs a version new
    # enough for the API the bootloader uses. Distro packages vary wildly --
    # Debian bookworm's docker.io is old enough to matter.
    if ! command -v curl >/dev/null 2>&1; then
        log_error "curl is required to install Docker. Install curl, or install"
        log_error "Docker yourself and re-run this script."
        exit 1
    fi

    local script
    script="$(mktemp)"
    if ! curl -fsSL https://get.docker.com -o "$script"; then
        rm -f "$script"
        log_error "Could not download the Docker installer."
        log_error "Install Docker manually, or use --native to build from source."
        exit 1
    fi
    sh "$script"
    rm -f "$script"

    command -v docker >/dev/null 2>&1 || {
        log_error "Docker installation did not produce a working 'docker' command."
        exit 1
    }
    log_success "Docker installed"
}

# start_engine brings the daemon up using whatever init this system has.
#
# We install no unit of our own, but the ENGINE's unit does have to be enabled,
# or nothing starts at boot and the whole design falls over.
start_engine() {
    if docker info >/dev/null 2>&1; then
        log_info "Docker daemon is running"
    elif command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        log_info "Starting the Docker daemon"
        systemctl enable --now docker
    else
        log_error "The Docker daemon is not running and this system has no systemd."
        log_error "Start Docker, then re-run this script."
        exit 1
    fi

    local waited=0
    while ! docker info >/dev/null 2>&1; do
        sleep 1
        waited=$((waited + 1))
        if [ "$waited" -ge 60 ]; then
            log_error "The Docker daemon did not become ready."
            exit 1
        fi
    done

    # Enable at boot even when it was already running: an engine that is up now
    # but disabled would leave the device dead after a power cycle, which is
    # exactly the failure nobody notices until it matters.
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        systemctl enable docker >/dev/null 2>&1 || \
            log_warning "Could not enable Docker at boot; check 'systemctl enable docker'."
    fi
}

# --- spec ----------------------------------------------------------------

resolve_runtime_version() {
    if [ -n "$RUNTIME_VERSION" ]; then
        return 0
    fi
    # The repo's VERSION file, so a plain checkout installs the matching
    # runtime rather than whatever "latest" happens to be that day.
    local version_file="$1/VERSION"
    if [ -f "$version_file" ]; then
        RUNTIME_VERSION="$(tr -d '[:space:]' < "$version_file")"
    fi
    if [ -z "$RUNTIME_VERSION" ]; then
        RUNTIME_VERSION="latest"
        log_warning "No VERSION file found; installing the 'latest' runtime tag."
    fi
}

# json_array renders arguments as a JSON string array.
json_array() {
    local first=1 item
    printf '['
    for item in "$@"; do
        [ $first -eq 1 ] || printf ', '
        first=0
        # Escape backslashes and quotes; paths should contain neither, but a
        # malformed spec would stop the bootloader from starting at all.
        printf '"%s"' "$(printf '%s' "$item" | sed 's/\\/\\\\/g; s/"/\\"/g')"
    done
    printf ']'
}

write_spec() {
    mkdir -p "$BOOTLOADER_STATE_DIR"
    chmod 750 "$BOOTLOADER_STATE_DIR"
    mkdir -p "$RUNTIME_DATA_DIR"
    chmod 755 "$RUNTIME_DATA_DIR"

    local spec="$BOOTLOADER_STATE_DIR/runtime-spec.json"
    local tmp="$spec.tmp"

    {
        printf '{\n'
        printf '  "repository": "%s",\n' "$RUNTIME_REPOSITORY"
        printf '  "version": "%s",\n' "$RUNTIME_VERSION"
        printf '  "dataDir": "%s",\n' "$RUNTIME_DATA_DIR"
        printf '  "bootloaderPort": %s' "$BOOTLOADER_PORT"
        if [ ${#EXTRA_MOUNTS[@]} -gt 0 ]; then
            printf ',\n  "extraBinds": %s' "$(json_array "${EXTRA_MOUNTS[@]}")"
        fi
        if [ ${#EXTRA_ENV[@]} -gt 0 ]; then
            printf ',\n  "extraEnv": %s' "$(json_array "${EXTRA_ENV[@]}")"
        fi
        printf '\n}\n'
    } > "$tmp"

    # Atomic, so an interrupted install cannot leave a spec the bootloader
    # refuses to parse -- which would stop it starting at all.
    mv "$tmp" "$spec"
    log_success "Wrote $spec"
}

# --- bootloader ----------------------------------------------------------

start_bootloader() {
    local image="$BOOTLOADER_REPOSITORY:$BOOTLOADER_VERSION"

    log_info "Pulling $image"
    if ! docker pull "$image"; then
        log_error "Could not pull the bootloader image."
        log_error "Check the device's internet access, or use --native."
        exit 1
    fi

    # Replace any previous bootloader. The RUNTIME container is deliberately
    # left alone: a re-run must not interrupt a running PLC, and the new
    # bootloader adopts whatever it finds healthy.
    if docker inspect "$BOOTLOADER_CONTAINER" >/dev/null 2>&1; then
        log_info "Replacing the existing bootloader container"
        docker rm -f "$BOOTLOADER_CONTAINER" >/dev/null
    fi

    log_info "Starting the bootloader"
    # --restart always is what makes this survive a reboot with no systemd
    # unit of ours. The bootloader must never exit on its own for that reason.
    #
    # The runtime data directory is mounted READ-ONLY here: the bootloader
    # authenticates against the runtime's accounts and must never be able to
    # create or modify one.
    docker run -d \
        --name "$BOOTLOADER_CONTAINER" \
        --restart always \
        --network host \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v "$BOOTLOADER_STATE_DIR:$BOOTLOADER_STATE_DIR" \
        -v "$RUNTIME_DATA_DIR:$RUNTIME_DATA_DIR:ro" \
        "$image" \
        -state-dir "$BOOTLOADER_STATE_DIR" \
        -port "$BOOTLOADER_PORT" >/dev/null

    log_success "Bootloader started"
}

wait_for_runtime() {
    log_info "Waiting for the runtime to come up (this pulls the image on first install)"
    local waited=0
    local state=""
    while [ "$waited" -lt 900 ]; do
        state="$(curl -sk "https://127.0.0.1:$BOOTLOADER_PORT/api/bootloader/capabilities" \
            2>/dev/null | sed -n 's/.*"state":"\([a-z]*\)".*/\1/p')"
        case "$state" in
            healthy)
                log_success "Runtime is up"
                return 0
                ;;
            recovery)
                log_warning "The bootloader is in recovery mode: the runtime did not start."
                log_warning "Connect the OpenPLC Editor to port $BOOTLOADER_PORT to see why"
                log_warning "and to install a different version."
                return 0
                ;;
        esac
        sleep 5
        waited=$((waited + 5))
    done
    log_warning "The runtime has not reported healthy yet. Check:"
    log_warning "  docker logs $BOOTLOADER_CONTAINER"
    return 0
}

print_summary() {
    cat <<EOF

OpenPLC Runtime is installed.

  Runtime image     $RUNTIME_REPOSITORY:$RUNTIME_VERSION
  Bootloader image  $BOOTLOADER_REPOSITORY:$BOOTLOADER_VERSION
  Runtime API       https://<device>:8443
  Bootloader API    https://<device>:$BOOTLOADER_PORT
  Runtime data      $RUNTIME_DATA_DIR
  Bootloader state  $BOOTLOADER_STATE_DIR

Useful commands:
  docker logs -f $BOOTLOADER_CONTAINER    Bootloader activity
  docker logs -f $RUNTIME_CONTAINER       Runtime output
  docker ps                               Both containers

The runtime version is changed from the OpenPLC Editor. Nothing needs to be
run on the device by hand.
EOF
}

main() {
    local repo_root="$1"; shift
    parse_args "$@"

    if [ "$(id -u)" -ne 0 ]; then
        log_error "This script must run as root (sudo ./install.sh)"
        exit 1
    fi

    resolve_runtime_version "$repo_root"

    log_info "Installing the OpenPLC Runtime with Docker"
    log_info "  runtime:    $RUNTIME_REPOSITORY:$RUNTIME_VERSION"
    log_info "  bootloader: $BOOTLOADER_REPOSITORY:$BOOTLOADER_VERSION"
    if [ ${#EXTRA_MOUNTS[@]} -gt 0 ]; then
        log_info "  extra mounts: ${EXTRA_MOUNTS[*]}"
    fi

    detect_engine || install_engine
    start_engine
    write_spec
    start_bootloader
    wait_for_runtime
    print_summary
}

main "$@"
