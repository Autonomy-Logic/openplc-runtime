#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

set -u
set -o pipefail

printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"

if [ -n "${FAKE_DOCKER_REAL:-}" ]; then
    exec "$FAKE_DOCKER_REAL" "$@"
fi

# Succeeds when the tag reference is one of the space-separated FAKE_DOCKER_EXISTING references.
tag_exists() {
    case " $FAKE_DOCKER_EXISTING " in
        *" $1 "*) return 0 ;;
    esac
    return 1
}

if [ "$*" = "buildx build --check --progress=plain -" ]; then
    answer="${FAKE_DOCKER_CHECK:?fake docker: FAKE_DOCKER_CHECK names no check answer}"
    cat >"${FAKE_DOCKER_CHECK_STDIN:-/dev/null}"
    cat "$answer/stderr" >&2
    cat "$answer/stdout"
    exit "$(cat "$answer/exit")"
fi

if [ "${1:-} ${2:-}" = "buildx build" ]; then
    if [ "$*" != "buildx build --call=targets,format=json -" ]; then
        printf 'fake docker: unsupported command: %s\n' "$*" >&2
        exit 1
    fi
    cat >"${FAKE_DOCKER_STDIN:-/dev/null}"
    printf '#0 building with "desktop-linux" instance using docker driver\n\n' >&2
    printf '#1 [internal] load build definition from Dockerfile\n#1 DONE 0.0s\n' >&2
    if [ "${FAKE_DOCKER_TARGETS_EXIT:-0}" != 0 ]; then
        printf 'ERROR: failed to build: failed to solve: dockerfile parse error on line 1: unknown instruction: FROMM (did you mean FROM?)\n' >&2
        exit "$FAKE_DOCKER_TARGETS_EXIT"
    fi
    cat "${FAKE_DOCKER_TARGETS:?fake docker: FAKE_DOCKER_TARGETS names no targets answer}"
    exit 0
fi

if [ "${1:-} ${2:-}" = "manifest inspect" ] && [ -n "${FAKE_DOCKER_EXISTING+set}" ]; then
    if [ "$#" -ne 3 ] || [ "${3#-}" != "$3" ]; then
        printf 'fake docker: unsupported command: %s\n' "$*" >&2
        exit 1
    fi
    if ! tag_exists "$3"; then
        printf 'no such manifest: %s\n' "$3" >&2
        exit 1
    fi
    printf '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[]}\n'
    exit 0
fi

if [ "${1:-} ${2:-} ${3:-}" != "buildx imagetools inspect" ]; then
    printf 'fake docker: unsupported command: %s\n' "$*" >&2
    exit 1
fi
shift 3

ref=""
format=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --format)
            format="${2:-}"
            shift
            ;;
        --format=*) format="${1#--format=}" ;;
        -*)
            printf 'fake docker: unsupported flag: %s\n' "$1" >&2
            exit 1
            ;;
        *) ref="$1" ;;
    esac
    shift
done

repo="${ref#*/}"
repo="${repo%%[@:]*}"
case "${FAKE_DOCKER_ERROR:-}" in
    "") ;;
    unauthorized)
        printf 'ERROR: failed to authorize: failed to fetch oauth token: unexpected status from GET request to https://ghcr.io/token?scope=repository%%3A%s%%3Apull&service=ghcr.io: 401 Unauthorized\n' "$repo" >&2
        exit 1
        ;;
    token-not-found)
        printf 'ERROR: failed to authorize: failed to fetch anonymous token: unexpected status from GET request to https://ghcr.io/token?scope=repository%%3A%s%%3Apull&service=ghcr.io: 404 Not Found\n' "$repo" >&2
        exit 1
        ;;
    rate-limited)
        printf 'ERROR: unexpected status from HEAD request to https://ghcr.io/v2/%s/manifests/%s: 429 Too Many Requests\n' "$repo" "${ref##*:}" >&2
        exit 1
        ;;
    network)
        printf 'ERROR: failed to do request: Head "https://ghcr.io/v2/%s/manifests/%s": dial tcp: lookup ghcr.io: no such host\n' "$repo" "${ref##*:}" >&2
        exit 1
        ;;
    other-ref-not-found)
        printf 'ERROR: %s-other: not found\n' "$ref" >&2
        exit 1
        ;;
    empty) exit 1 ;;
    *)
        printf 'fake docker: unsupported FAKE_DOCKER_ERROR: %s\n' "$FAKE_DOCKER_ERROR" >&2
        exit 1
        ;;
esac

if [ "${FAKE_DOCKER_EXIT:-0}" != 0 ]; then
    printf 'ERROR: %s: not found\n' "$ref" >&2
    exit "$FAKE_DOCKER_EXIT"
fi

if [ -n "${FAKE_DOCKER_EXISTING+set}" ] && [ "${ref#*@}" = "$ref" ] && ! tag_exists "$ref"; then
    printf 'ERROR: %s: not found\n' "$ref" >&2
    exit 1
fi

case "$ref" in
    *@*) digest="${ref#*@}" ;;
    *) digest="${FAKE_DOCKER_DIGEST:-}" ;;
esac
media=application/vnd.oci.image.index.v1+json

case "$format" in
    "")
        printf 'Name:      %s\nMediaType: %s\nDigest:    %s\n\nManifests:\n' "$ref" "$media" "$digest"
        printf '  Platform:  linux/amd64\n'
        ;;
    *'.Manifest.Digest'*) printf '%s\n' "$digest" ;;
    *'json .Manifest'*) printf '{"mediaType":"%s","digest":"%s","size":1609}\n' "$media" "$digest" ;;
    *)
        printf 'fake docker: unsupported format: %s\n' "$format" >&2
        exit 1
        ;;
esac
