#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

set -u
set -o pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)"
# shellcheck source=scripts/lib/common.sh
. "$LIB_DIR/common.sh"
# shellcheck source=scripts/lib/dockerfile.sh
. "$LIB_DIR/dockerfile.sh"

BASE_PACKAGE="ghcr.io/autonomy-logic/openplc-runtime-base"
SIGNER_REPO="Autonomy-Logic/autonomy-docker"
SIGNER_WORKFLOW="$SIGNER_REPO/.github/workflows/build.yml"
SOURCE_REF="refs/heads/main"
PIN_RULES="no-blank-in-continuation no-parser-directive no-heredoc plain-flags no-onbuild from-pinned-by-digest from-without-variable from-single-pin copy-from-stages-only base-labels"
TARGETS_JQ='if length == 1 and (.[0].targets | type) == "array" and (.[0].targets | length) > 0
    then .[0].targets[] | [(.name // "-"), (if has("base") then "=" + .base else "" end)] | @tsv
    else error("not a BuildKit targets answer") end'
FAILURES=0
WORK_DIR=""

usage() {
    cat <<EOF
usage: verify-base-image.sh [--no-attestation] [Dockerfile]

Checks that the Dockerfile (default: Dockerfile in the working directory) builds every stage on one
$BASE_PACKAGE image pinned by tag and digest. Unless --no-attestation is given, also checks
that the tag resolves to that digest in the registry, that the digest is attested by
$SIGNER_WORKFLOW on $SOURCE_REF, built on a GitHub-hosted runner, that BuildKit
(docker buildx build --call=targets) reads the same stages, names and bases as this check, and
that every image BuildKit resolves (docker buildx build --check) is the pin, a BuildKit lint
warning failing this last step; both BuildKit steps only read the Dockerfile and build and push
nothing. --no-attestation skips these four and runs offline.
Prints one error: line per broken rule and exits non-zero when any rule is broken.
EOF
}

# Prints the error line of a broken rule and counts it: fail <rule> <message>.
fail() {
    printf 'error: %s: %s\n' "$1" "$(one_line "$2")" >&2
    FAILURES=$((FAILURES + 1))
}

# Applies one rule of scripts/lib/dockerfile.sh and fails it when broken: check_rule <rule> <Dockerfile>.
check_rule() {
    local out
    out="$("check_${1//-/_}" "$2" 2>&1)" || { fail "$1" "${out:-rule broken}"; return 1; }
}

# Applies the Dockerfile pin rules of scripts/lib/dockerfile.sh.
check_pin_rules() {
    local id
    for id in $PIN_RULES; do
        check_rule "$id" "$1"
    done
}

# Prints each base image reference of a Dockerfile once.
base_references() {
    df_external_stages "$1" | cut -f 3 | sort -u
}

# Fails unless every base image is the runtime base package pinned as <name>:<tag>@sha256:<digest>.
check_package() {
    local image name rest bad="" pin_re="^$TAG_RE@sha256:[0-9a-f]{64}\$"
    while IFS= read -r image; do
        [ -n "$image" ] || continue
        name="${image%%:*}"
        rest="${image#*:}"
        if [ "$name" != "$BASE_PACKAGE" ] || ! [[ "$rest" =~ $pin_re ]]; then
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

# Prints the first ERROR or lint summary line of BuildKit output files, else the last line of the first one.
buildkit_error() {
    local line
    line="$(grep -h -m 1 -e '^ERROR' -e '^Check complete' "$@" 2>/dev/null)"
    line="${line%%$'\n'*}"
    [ -n "$line" ] || line="$(tail -n 1 "$1")"
    printf '%s\n' "${line:-no message}"
}

# Fails unless BuildKit lists the stages of the Dockerfile in the same order, with the same names and bases.
check_buildkit_stages() {
    local file="$1" diff
    command -v jq >/dev/null 2>&1 || { fail buildkit-stages "jq is needed to read the BuildKit answer and is not on PATH"; return; }
    if ! docker buildx build --call=targets,format=json - <"$file" >"$WORK_DIR/targets.json" 2>"$WORK_DIR/buildkit.err"; then
        fail buildkit-stages "docker buildx build --call=targets failed: $(buildkit_error "$WORK_DIR/buildkit.err")"
        return
    fi
    if ! jq -rs "$TARGETS_JQ" "$WORK_DIR/targets.json" >"$WORK_DIR/buildkit.tsv" 2>"$WORK_DIR/jq.err" ||
        [ ! -s "$WORK_DIR/buildkit.tsv" ]; then
        fail buildkit-stages "the BuildKit targets answer cannot be read: $(one_line "$(<"$WORK_DIR/jq.err")")"
        return
    fi
    df_stages "$file" | cut -f 2,3 >"$WORK_DIR/script.tsv"
    diff="$(awk -F '\t' '
        NR == FNR { n++; name[n] = $1; image[n] = $2; next }
        {
            m++
            if (m > n) { printf "BuildKit has stage %d (%s) that this check does not\n", m, $1; next }
            if ($1 != name[m]) printf "stage %d is %s for BuildKit, %s for this check\n", m, $1, name[m]
            if ($2 != "" && substr($2, 2) != image[m]) printf "stage %d builds on %s for BuildKit, %s for this check\n", m, substr($2, 2), image[m]
        }
        END { if (m < n) printf "BuildKit has %d stages, this check %d\n", m, n }' "$WORK_DIR/script.tsv" "$WORK_DIR/buildkit.tsv")"
    [ -z "$diff" ] || fail buildkit-stages "BuildKit reads the stages differently: $diff"
}

# Fails unless BuildKit's plain progress output names at least one resolved image and only the pin: check_buildkit_images <Dockerfile> <pin>.
check_buildkit_images() {
    local file="$1" pin="$2" image bad=""
    if ! docker buildx build --check --progress=plain - <"$file" >"$WORK_DIR/check.out" 2>"$WORK_DIR/check.err"; then
        fail buildkit-images "docker buildx build --check failed: $(buildkit_error "$WORK_DIR/check.err" "$WORK_DIR/check.out")"
        return
    fi
    sed -n 's/^#[0-9][0-9]* \[internal\] load metadata for \([^[:space:]][^[:space:]]*\)$/\1/p' "$WORK_DIR/check.err" |
        sort -u >"$WORK_DIR/images.txt"
    if [ ! -s "$WORK_DIR/images.txt" ]; then
        fail buildkit-images "docker buildx build --check reported no resolved image in its plain progress output"
        return
    fi
    while IFS= read -r image; do
        [ "$image" = "$pin" ] || bad="$bad $image"
    done <"$WORK_DIR/images.txt"
    [ -z "$bad" ] || fail buildkit-images "BuildKit resolves images other than the pin $pin:$bad"
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

check_rule plain-text-only "$dockerfile" || exit 1
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
check_buildkit_stages "$dockerfile"
check_buildkit_images "$dockerfile" "$pin"
check_tag "${ref%:*}" "${ref##*:}" "$digest"
check_attestation "${ref%:*}" "$digest"
[ "$FAILURES" = 0 ] || exit 1
printf 'ok: %s pins %s, attested by %s on %s\n' "$dockerfile" "$pin" "$SIGNER_WORKFLOW" "$SOURCE_REF"
