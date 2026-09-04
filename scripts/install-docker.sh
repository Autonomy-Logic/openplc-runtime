#!/usr/bin/env bash
# OpenPLC Runtime installer -- container edition (RTOP-283).
#
# Two ways in, one script:
#
#   curl -fsSL https://runtime.getedge.me | sudo bash      no checkout, no toolchain
#   sudo ./install.sh                                      from a clone; execs this
#
# It installs no build toolchain and compiles nothing: it ensures a container
# engine, writes the bootloader's spec, and starts the bootloader. The
# bootloader then pulls the runtime image and brings it up.
#
# Docker is the ONLY dependency this path adds. Nothing of ours goes into
# systemd -- Docker's own restart policy starts the bootloader at boot, and the
# bootloader starts the runtime. That is deliberate: the fewer moving parts
# between power-on and a running PLC, the fewer ways it fails.
#
# `sudo ./install.sh --native` keeps the source build, for MSYS2 and for
# targets that cannot host a container engine. That path needs the repository
# on disk, so it is not reachable from the piped one-liner.
#
# `--uninstall` removes everything this script created and puts back whatever
# it displaced, so a device ends up as it started.
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

# Units this run stood down, for rollback. Distinct from the persisted record,
# which accumulates across installs and is what --uninstall reads.
declare -a DISPLACED_THIS_RUN=()

# Where this script lives, for the self-elevation path below. A piped run has
# no file to re-exec, so it fetches a fresh copy rather than trying to re-read
# a stdin bash has already consumed.
# Where the piped one-liner fetches this script.
#
# runtime.getedge.me serves this file verbatim from the release branch. Until
# that host is stood up the raw GitHub URL below works identically, and
# OPENPLC_INSTALLER_URL overrides both -- which is also how a fork or an
# internal mirror points it somewhere else.
INSTALLER_URL="${OPENPLC_INSTALLER_URL:-https://runtime.getedge.me}"
INSTALLER_URL_FALLBACK="https://raw.githubusercontent.com/Autonomy-Logic/openplc-runtime/main/scripts/install-docker.sh"

MODE=install
ASSUME_YES=false
KEEP_IMAGES=false
PURGE_DATA=false
REPO_ROOT=""

# systemd units that run a pre-container OpenPLC. Two of them are real and in
# the field: openplc.service is the v3 runtime, openplc-runtime.service is a v4
# source install. Both bind 8443, so leaving one running means the container
# starts and then fails to serve, with nothing obviously wrong on either side.
LEGACY_UNITS=(openplc-runtime.service openplc.service openplc_v3.service openplc-v3.service)

# What we stopped, so --uninstall can put it back. Kept in the bootloader's
# state directory because that survives an "erase all data" of the runtime's.
disabled_units_file() { printf '%s/displaced-systemd-units' "$BOOTLOADER_STATE_DIR"; }

usage() {
    cat <<'EOF'
OpenPLC Runtime installer (container edition)

Usage:
  curl -fsSL https://runtime.getedge.me | sudo bash
  curl -fsSL https://runtime.getedge.me | sudo bash -s -- --uninstall --yes
  sudo ./install.sh [options]

Install options:
  --native                    Build from source instead (needs a checkout)
  --runtime-version VERSION   Runtime image tag (default: the VERSION file, else latest)
  --bootloader-version VER    Bootloader image tag (default: latest)
  --mount HOST:CONTAINER[:ro] Extra bind mount for the runtime; repeatable
  --env KEY=VALUE             Extra environment variable for the runtime; repeatable
  --data-dir PATH             Runtime persistent data directory
  --port PORT                 Bootloader control port (default: 8445)
  --repo-root PATH            Checkout to read VERSION from (set by install.sh)

Uninstall options:
  --uninstall                 Remove the containers, images and data this
                              installed, and re-enable any systemd runtime it
                              displaced
  -y, --yes                   Do not ask for confirmation (required when piped)
  --keep-images               Leave the pulled images on disk
  --purge                     Also delete the runtime's data directory (users,
                              credentials, stored project, retained variables).
                              Refused when a systemd runtime is being restored,
                              because it shares that directory

  -h, --help                  Show this help

Re-running the install is safe: it rewrites the spec and restarts the
bootloader without touching runtime data, so adding a mount does not mean
reinstalling anything.
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
            --repo-root)          REPO_ROOT="$2"; shift 2 ;;
            --uninstall)          MODE=uninstall; shift ;;
            -y|--yes)             ASSUME_YES=true; shift ;;
            --keep-images)        KEEP_IMAGES=true; shift ;;
            --purge)              PURGE_DATA=true; shift ;;
            -h|--help)            usage; exit 0 ;;
            *) log_error "unknown option: $1"; usage; exit 1 ;;
        esac
    done

    if [ "$MODE" = install ] && { [ "$KEEP_IMAGES" = true ] || [ "$PURGE_DATA" = true ]; }; then
        log_warning "--keep-images and --purge only apply to --uninstall; ignoring."
    fi
}

# require_root re-runs this script under sudo, or explains how to.
#
# The piped case has no file to re-exec: bash has already consumed the script
# from stdin, so re-reading it would save a truncated copy. Fetching a fresh
# one is the honest way to get a file, and when that is not possible the exact
# command to run is more use than a partial install.
require_root() {
    [ "$(id -u)" -eq 0 ] && return 0

    if [ -f "${BASH_SOURCE[0]:-}" ]; then
        log_info "Root is required; re-running under sudo"
        exec sudo -E bash "${BASH_SOURCE[0]}" "$@"
    fi

    # Piped, so there is no file to re-exec. Fetching a fresh copy and running
    # it under sudo is the only way to elevate from here -- and it means
    # downloading code and running it as root, so the URL and the hash of what
    # arrived are printed first. An operator who did not expect a download can
    # see it happen, and can compare the hash against the release.
    if command -v curl >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
        local copy
        copy="$(mktemp)"
        local fetched_from="$INSTALLER_URL"
        if ! { curl -fsSL "$INSTALLER_URL" -o "$copy" 2>/dev/null && [ -s "$copy" ]; }; then
            # The branded host may not resolve yet; GitHub always does.
            fetched_from="$INSTALLER_URL_FALLBACK"
            curl -fsSL "$INSTALLER_URL_FALLBACK" -o "$copy" 2>/dev/null || true
        fi
        if [ -s "$copy" ]; then
            log_warning "Root is required. Re-fetching this installer and running it as root:"
            log_warning "  source: $fetched_from"
            if command -v sha256sum >/dev/null 2>&1; then
                log_warning "  sha256: $(sha256sum "$copy" | cut -d' ' -f1)"
            fi
            exec sudo -E bash "$copy" "$@"
        fi
        rm -f "$copy"
    fi

    log_error "This installer must run as root. Re-run it as:"
    log_error "  curl -fsSL $INSTALLER_URL | sudo bash"
    exit 1
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

# --- displaced systemd runtimes -------------------------------------------

have_systemd() {
    command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}

unit_exists() {
    systemctl list-unit-files "$1" >/dev/null 2>&1 &&
        [ -n "$(systemctl list-unit-files --no-legend "$1" 2>/dev/null)" ]
}

# stop_legacy_runtimes clears the way for the container.
#
# A source or v3 install binds 8443 from systemd. Left running, the runtime
# container starts, fails to bind, and the bootloader reports a runtime that
# will not come up -- while the editor still reaches *something* on 8443,
# because the old one answered. That is a genuinely confusing failure, and it
# is the single most likely thing to go wrong on a device that has been in the
# field, so it is handled rather than documented.
#
# The unit is stopped and disabled, never deleted: uninstall puts it back.
stop_legacy_runtimes() {
    have_systemd || return 0

    local unit displaced=()
    for unit in "${LEGACY_UNITS[@]}"; do
        unit_exists "$unit" || continue

        local was_active=no was_enabled=no
        systemctl is-active --quiet "$unit" 2>/dev/null && was_active=yes
        systemctl is-enabled --quiet "$unit" 2>/dev/null && was_enabled=yes
        [ "$was_active" = no ] && [ "$was_enabled" = no ] && continue

        local state_desc="stopped"
        [ "$was_active" = yes ] && state_desc="running"
        [ "$was_enabled" = yes ] && state_desc="$state_desc, starts at boot"

        log_warning "Found $unit ($state_desc)"
        log_info "  It binds port 8443, which the runtime container needs. Standing it down."

        [ "$was_active" = yes ] && systemctl stop "$unit" >/dev/null 2>&1 || true
        [ "$was_enabled" = yes ] && systemctl disable "$unit" >/dev/null 2>&1 || true
        displaced+=("$unit:$was_active:$was_enabled")
        log_success "  $unit stopped and disabled"
    done

    [ ${#displaced[@]} -eq 0 ] && return 0

    # What THIS run displaced, kept separately from the persisted record.
    #
    # Rollback used to read the persisted file, which on a re-run holds what
    # the FIRST install displaced -- units already disabled and not touched
    # this time. A failing re-run therefore started the old native runtime
    # beside the container it had deliberately left alone, and then deleted
    # the record, so a later --uninstall could no longer restore anything.
    DISPLACED_THIS_RUN=("${displaced[@]}")

    mkdir -p "$BOOTLOADER_STATE_DIR"
    # Merged with anything already recorded, so an earlier install's
    # displacement is not forgotten by a later one that displaced nothing.
    {
        [ -f "$(disabled_units_file)" ] && cat "$(disabled_units_file)"
        printf '%s\n' "${displaced[@]}"
    } | awk 'NF && !seen[$0]++' > "$(disabled_units_file).tmp"
    mv "$(disabled_units_file).tmp" "$(disabled_units_file)"
    chmod 640 "$(disabled_units_file)"
}

# restore_legacy_runtimes undoes exactly what stop_legacy_runtimes did.
#
# Only units this installer stood down, and only to the state they were in:
# a unit that was enabled-but-stopped is re-enabled and left stopped. Guessing
# any wider than that would be inventing a configuration the device never had.
restore_legacy_runtimes() {
    local record; record="$(disabled_units_file)"
    [ -f "$record" ] || return 0
    have_systemd || { log_warning "No systemd here; cannot restore $record"; return 0; }

    local unit was_active was_enabled
    while IFS=: read -r unit was_active was_enabled; do
        [ -n "$unit" ] || continue
        unit_exists "$unit" || { log_warning "  $unit is gone; nothing to restore"; continue; }
        if [ "$was_enabled" = yes ]; then
            systemctl enable "$unit" >/dev/null 2>&1 && log_success "  re-enabled $unit"
        fi
        if [ "$was_active" = yes ]; then
            systemctl start "$unit" >/dev/null 2>&1 && log_success "  restarted $unit"
        fi
    done < "$record"
    rm -f "$record"
}

# rollback_on_failure puts the device back if the install dies partway.
#
# Between standing the old runtime down and the container reporting healthy
# there is a window where the device has no PLC. Anything that fails in it --
# a spec that cannot be written, a container that will not start -- must hand
# the device back the runtime it had, rather than leaving it with neither.
rollback_on_failure() {
    local status=$?
    [ "$status" -eq 0 ] && return 0
    [ ${#DISPLACED_THIS_RUN[@]} -eq 0 ] && return 0

    log_error "Install failed; restoring the runtime that was here before."
    local entry unit was_active was_enabled
    for entry in "${DISPLACED_THIS_RUN[@]}"; do
        IFS=: read -r unit was_active was_enabled <<<"$entry"
        [ -n "$unit" ] || continue
        if [ "$was_enabled" = yes ]; then
            systemctl enable "$unit" >/dev/null 2>&1 && log_success "  re-enabled $unit"
        fi
        if [ "$was_active" = yes ]; then
            systemctl start "$unit" >/dev/null 2>&1 && log_success "  restarted $unit"
        fi
    done
    # The persisted record is deliberately left in place: --uninstall still
    # needs it, and this rollback is not the end of the device's life.
}

# --- uninstall -------------------------------------------------------------

confirm_uninstall() {
    [ "$ASSUME_YES" = true ] && return 0
    if [ ! -t 0 ]; then
        log_error "Refusing to uninstall without confirmation when nothing is attached"
        log_error "to answer. Re-run with --yes:"
        log_error "  curl -fsSL $INSTALLER_URL | sudo bash -s -- --uninstall --yes"
        exit 1
    fi
    echo
    echo "This removes the OpenPLC bootloader and runtime containers from this device."
    if [ "$PURGE_DATA" = true ]; then
        echo "It also deletes $RUNTIME_DATA_DIR -- users, credentials, the stored"
        echo "project, retained variables and any VPP licences."
    else
        echo "$RUNTIME_DATA_DIR is kept; pass --purge to delete it too."
    fi
    printf 'Continue? [y/N] '
    local answer; read -r answer
    case "$answer" in [yY]|[yY][eE][sS]) return 0 ;; esac
    echo "Nothing was changed."
    exit 0
}

remove_container() {
    local name="$1"
    docker inspect "$name" >/dev/null 2>&1 || return 0
    # Force, because the restart policy would otherwise bring the bootloader
    # back between the stop and the remove.
    docker rm -f "$name" >/dev/null 2>&1 && log_success "  removed container $name"
}

remove_installed_images() {
    [ "$KEEP_IMAGES" = true ] && { log_info "  keeping images (--keep-images)"; return 0; }
    local ref
    # Every tag of ours, not just the one currently recorded: a device that has
    # been through a version change or two holds several, and leaving them
    # behind is most of the disk this installer ever used.
    for ref in $(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null |
                 grep -E "^(${RUNTIME_REPOSITORY}|${BOOTLOADER_REPOSITORY}):" || true); do
        docker rmi "$ref" >/dev/null 2>&1 && log_success "  removed image $ref"
    done
}

do_uninstall() {
    log_info "Uninstalling the OpenPLC Runtime"
    confirm_uninstall

    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        log_info "Removing containers"
        remove_container "$RUNTIME_CONTAINER"
        remove_container "$BOOTLOADER_CONTAINER"
        log_info "Removing images"
        remove_installed_images
    else
        log_warning "Docker is not available; skipping container and image removal."
    fi

    # Data BEFORE restoring the old runtime: restarting it first would have it
    # recreate this directory, and the delete would then take out files the
    # runtime we just handed back had already written.
    #
    # Kept by default, because it is NOT exclusively ours. A native install
    # reads and writes the same path (webserver/config.py resolves
    # /var/lib/openplc-runtime on native Linux), which is what makes moving a
    # device to containers keep its users and project -- and what makes
    # deleting it on the way out destroy the data of the very runtime this
    # uninstall is about to restore.
    if [ "$PURGE_DATA" = true ] && [ -f "$(disabled_units_file)" ]; then
        log_warning "Not deleting $RUNTIME_DATA_DIR: a systemd runtime is being"
        log_warning "restored and shares that directory. Remove it by hand if you"
        log_warning "are certain nothing else needs it."
    elif [ "$PURGE_DATA" = true ]; then
        rm -rf "$RUNTIME_DATA_DIR"
        log_success "  removed $RUNTIME_DATA_DIR"
    else
        log_info "  keeping $RUNTIME_DATA_DIR (pass --purge to delete it)"
    fi

    # The bootloader's own state is unambiguously ours, so it always goes --
    # but only after the record inside it has been used to restore units.
    log_info "Restoring anything this installer displaced"
    restore_legacy_runtimes
    rm -rf "$BOOTLOADER_STATE_DIR"
    log_success "  removed $BOOTLOADER_STATE_DIR"

    # Docker itself is left alone. It may well predate this install, and other
    # things on the device may depend on it -- removing a container engine
    # because one of its tenants moved out is not ours to decide.
    cat <<EOF

OpenPLC Runtime has been removed.

$RUNTIME_DATA_DIR was left in place unless --purge was given: a native
install uses the same directory, so it is not safe to assume it is ours.

Docker was left installed: it may have been here first, and other containers
may depend on it. Remove it yourself if this device no longer needs it.
EOF
}

# --- spec ----------------------------------------------------------------

resolve_runtime_version() {
    if [ -n "$RUNTIME_VERSION" ]; then
        return 0
    fi
    # The repo's VERSION file, so a plain checkout installs the matching
    # runtime rather than whatever "latest" happens to be that day.
    # Empty when piped: there is no checkout, so "latest" is the only sensible
    # answer and the warning below says so.
    local version_file="${1:-}/VERSION"
    if [ -n "${1:-}" ] && [ -f "$version_file" ]; then
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

# pull_bootloader_image runs BEFORE anything on the device is disturbed.
#
# Found by testing: with the pull inside start_bootloader, a device that could
# not reach the registry had already had its systemd runtime stopped and
# disabled by the time the download failed -- so a failed install left it with
# no PLC at all. Nothing destructive happens until the image is on disk.
pull_bootloader_image() {
    local image="$BOOTLOADER_REPOSITORY:$BOOTLOADER_VERSION"

    log_info "Pulling $image"
    if docker pull "$image" >/dev/null 2>&1; then
        log_success "Bootloader image ready"
        return 0
    fi

    # A local copy is a legitimate answer: an air-gapped device, or one
    # side-loaded with `docker load`. Same policy the updater applies.
    if docker image inspect "$image" >/dev/null 2>&1; then
        log_warning "Could not reach the registry; using the copy already on this device."
        return 0
    fi

    log_error "Could not pull $image, and no local copy is present."
    log_error "Check the device's internet access, or use --native to build from source."
    log_error "Nothing on this device has been changed."
    exit 1
}

start_bootloader() {
    local image="$BOOTLOADER_REPOSITORY:$BOOTLOADER_VERSION"

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
    # Help before anything else, so `--help` never needs root and never has to
    # reach the network.
    for arg in "$@"; do
        case "$arg" in -h|--help) usage; exit 0 ;; esac
    done

    # After --help, so the usage text is readable from a developer machine.
    if [ "${OSTYPE:-}" != "" ] && [[ ${OSTYPE} != linux-gnu* ]]; then
        log_error "This installer supports Linux only."
        exit 1
    fi

    require_root "$@"
    parse_args "$@"

    if [ "$MODE" = uninstall ]; then
        do_uninstall
        return 0
    fi

    resolve_runtime_version "$REPO_ROOT"

    log_info "Installing the OpenPLC Runtime with Docker"
    log_info "  runtime:    $RUNTIME_REPOSITORY:$RUNTIME_VERSION"
    log_info "  bootloader: $BOOTLOADER_REPOSITORY:$BOOTLOADER_VERSION"
    if [ ${#EXTRA_MOUNTS[@]} -gt 0 ]; then
        log_info "  extra mounts: ${EXTRA_MOUNTS[*]}"
    fi

    detect_engine || install_engine
    start_engine
    # Everything above this line is additive, and the pull below is too. The
    # device keeps working throughout.
    pull_bootloader_image

    # From here the old runtime is gone and the new one is not yet up, so a
    # failure has to hand the device back what it had.
    trap rollback_on_failure EXIT
    stop_legacy_runtimes
    write_spec
    start_bootloader
    wait_for_runtime
    trap - EXIT

    print_summary
}

main "$@"
