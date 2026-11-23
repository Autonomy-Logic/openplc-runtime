#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

set -u
set -o pipefail

RULES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../tests/image" && pwd)"
# shellcheck source=tests/image/helpers.sh
. "$RULES_DIR/helpers.sh"
# shellcheck source=tests/image/static.sh
. "$RULES_DIR/static.sh"

BASE_PACKAGE="ghcr.io/autonomy-logic/openplc-runtime-base"
SIGNER_REPO="Autonomy-Logic/autonomy-docker"
SIGNER_WORKFLOW="$SIGNER_REPO/.github/workflows/build.yml"
SOURCE_REF="refs/heads/main"
PIN_RULES="no-syntax-directive from-pinned-by-digest from-without-variable from-single-pin copy-from-stages-only"
FAILURES=0
WORK_DIR=""

usage() {
    cat <<EOF
usage: verify-base-image.sh [--no-attestation] [Dockerfile]

Checks that the Dockerfile (default: Dockerfile in the working directory) builds every stage on one
$BASE_PACKAGE image pinned by tag and digest. Unless --no-attestation is given, also checks
that the tag resolves to that digest in the registry and that the digest is attested by
$SIGNER_WORKFLOW on $SOURCE_REF, built on a GitHub-hosted runner.
Prints one error: line per broken rule and exits non-zero when any rule is broken.
EOF
}

# Prints a text on one line, its non-empty lines joined by "; ".
one_line() {
    printf '%s\n' "$1" | awk 'NF { printf "%s%s", sep, $0; sep = "; " } END { print "" }'
}

# Prints the error line of a broken rule and counts it: fail <rule> <message>.
fail() {
    printf 'error: %s: %s\n' "$1" "$(one_line "$2")" >&2
    FAILURES=$((FAILURES + 1))
}

# Applies the Dockerfile pin rules shared with tests/image/static.sh.
check_pin_rules() {
    local id out
    for id in $PIN_RULES; do
        out="$("check_${id//-/_}" "$1" 2>&1)" || fail "$id" "${out:-rule broken}"
    done
}

# Prints each base image reference of a Dockerfile once.
base_references() {
    df_external_stages "$1" | cut -f 3 | sort -u
}

# Fails unless every base image is the runtime base package pinned as <name>:<tag>@sha256:<digest>.
check_package() {
    local image name rest bad=""
    while IFS= read -r image; do
        [ -n "$image" ] || continue
        name="${image%%:*}"
        rest="${image#*:}"
        if [ "$name" != "$BASE_PACKAGE" ] || ! [[ "$rest" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}@sha256:[0-9a-f]{64}$ ]]; then
            bad="$bad
FROM $image is not $BASE_PACKAGE:<tag>@sha256:<digest>"
        fi
    done < <(base_references "$1")
    [ -z "$bad" ] || fail package "$bad"
}

# Fails unless the tag resolves to the pinned digest in the registry: check_tag <name> <tag> <digest>.
check_tag() {
    local got
    if ! got="$(docker buildx imagetools inspect "$1:$2" --format '{{.Manifest.Digest}}' 2>"$WORK_DIR/docker.err")"; then
        fail tag "$1:$2 cannot be resolved in the registry: $(one_line "$(<"$WORK_DIR/docker.err")")"
        return
    fi
    [ "$got" = "$3" ] || fail tag "$1:$2 resolves to ${got:-nothing}, not to the pinned $3"
}

# Fails unless a verified attestation from the signer workflow names the pinned digest: check_attestation <name> <digest>.
check_attestation() {
    local subjects
    if ! subjects="$(gh attestation verify "oci://$1@$2" --repo "$SIGNER_REPO" --signer-workflow "$SIGNER_WORKFLOW" \
        --source-ref "$SOURCE_REF" --deny-self-hosted-runners --format json \
        --jq '.[].verificationResult.statement.subject[] | "\(.name) sha256:\(.digest.sha256)"' \
        2>"$WORK_DIR/gh.err")"; then
        fail attestation "$1@$2 is not attested by $SIGNER_WORKFLOW on $SOURCE_REF: $(one_line "$(<"$WORK_DIR/gh.err")")"
        return
    fi
    printf '%s\n' "$subjects" | grep -Fxq -- "$1 $2" ||
        fail attestation "no verified attestation names $1@$2 as its subject"
}

attest=1
case "${1:-}" in
    --no-attestation) attest=0; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) usage >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { usage >&2; exit 2; }
dockerfile="${1:-Dockerfile}"

if [ ! -f "$dockerfile" ]; then
    fail file "$dockerfile does not exist"
    exit 1
fi

check_pin_rules "$dockerfile"
check_package "$dockerfile"
[ "$FAILURES" = 0 ] || exit 1

pin="$(base_references "$dockerfile")"
if [ "$attest" = 0 ]; then
    printf 'ok: %s pins %s (attestation not checked)\n' "$dockerfile" "$pin"
    exit 0
fi

ref="${pin%@*}"
digest="${pin#*@}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
check_tag "${ref%:*}" "${ref##*:}" "$digest"
check_attestation "${ref%:*}" "$digest"
[ "$FAILURES" = 0 ] || exit 1
printf 'ok: %s pins %s, attested by %s on %s\n' "$dockerfile" "$pin" "$SIGNER_WORKFLOW" "$SOURCE_REF"
