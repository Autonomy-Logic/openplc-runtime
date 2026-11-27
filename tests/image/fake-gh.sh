#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

set -u
set -o pipefail

printf '%s\n' "$*" >>"$FAKE_GH_LOG"

if [ "${1:-} ${2:-}" != "attestation verify" ]; then
    printf 'fake gh: unsupported command: %s\n' "$*" >&2
    exit 1
fi

subject=""
format=""
jq_expr=""
prev=""
for arg in "$@"; do
    case "$prev" in
        --format) format="$arg" ;;
        --jq | -q) jq_expr="$arg" ;;
    esac
    case "$arg" in
        oci://*) subject="${arg#oci://}" ;;
        --format=*) format="${arg#--format=}" ;;
        --jq=*) jq_expr="${arg#--jq=}" ;;
    esac
    prev="$arg"
done

if [ "${FAKE_GH_EXIT:-0}" != 0 ]; then
    printf 'Error: no attestations found for subject %s\n' "$subject" >&2
    exit "$FAKE_GH_EXIT"
fi

if [ -n "$jq_expr" ] && [ "$format" != json ]; then
    printf 'cannot use --jq without --format json\n' >&2
    exit 1
fi

subjects="${FAKE_GH_SUBJECTS-${subject%@*} ${subject#*@}}"

json="["
sep=""
while read -r name digest; do
    [ -n "$name" ] || continue
    json="$json$sep{\"verificationResult\":{\"statement\":{\"_type\":\"https://in-toto.io/Statement/v1\","
    json="$json\"subject\":[{\"name\":\"$name\",\"digest\":{\"sha256\":\"${digest#sha256:}\"}}],"
    json="$json\"predicateType\":\"https://slsa.dev/provenance/v1\"}}}"
    sep=","
done <<<"$subjects"
json="$json]"

if [ "$format" = json ]; then
    if [ -n "$jq_expr" ]; then
        printf '%s\n' "$json" | jq -r "$jq_expr"
    else
        printf '%s\n' "$json"
    fi
else
    printf 'Loaded digest %s for oci://%s\n' "${subject#*@}" "$subject"
    printf 'Verification succeeded!\n'
fi
