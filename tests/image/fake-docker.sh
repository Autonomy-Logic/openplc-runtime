#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

set -u
set -o pipefail

printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"

# Succeeds when the tag reference is one of the space-separated FAKE_DOCKER_EXISTING references.
tag_exists() {
    case " $FAKE_DOCKER_EXISTING " in
        *" $1 "*) return 0 ;;
    esac
    return 1
}

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
