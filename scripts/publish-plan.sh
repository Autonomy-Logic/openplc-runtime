#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

set -u
set -o pipefail

RELEASE_IMAGE="ghcr.io/autonomy-logic/openplc-runtime"
TEST_IMAGE="ghcr.io/autonomy-logic/openplc-runtime-test"

# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

usage() {
    cat <<EOF
usage: publish-plan.sh

Decides the image, tags and version of a runtime image build from GITHUB_EVENT_NAME, GITHUB_REF and,
for workflow_dispatch, RELEASE_TAG and TEST_TAG, and prints them as image=, tags= and version= lines
for GITHUB_OUTPUT:

- push of a v* tag: $RELEASE_IMAGE, the tag, plus latest when the tag has no '-';
- workflow_dispatch with RELEASE_TAG: $RELEASE_IMAGE, that tag only;
- workflow_dispatch with TEST_TAG: $TEST_IMAGE, that tag only.

Refuses, with exit 1, any other event, a dispatch with neither or both tags, a tag that is not a valid
image tag or is latest, and a tag that already exists in its package. Exits 2 when the registry answer
(docker buildx imagetools inspect) does not say clearly that the tag is absent.
EOF
}

# Prints an error on stderr and exits: refuse <exit code> <message>.
refuse() {
    printf 'error: %s\n' "$2" >&2
    exit "$1"
}

# Refuses a tag that is not a valid image tag, or is latest: check_tag_name <what> <tag>.
check_tag_name() {
    [[ "$2" =~ $TAG_PATTERN ]] || refuse 1 "$1 '$2' is not a valid image tag"
    [ "$2" != latest ] || refuse 1 "$1 cannot be latest"
}

# Returns only when the registry answers that the reference does not exist: require_absent <image:tag>.
require_absent() {
    local err status=0
    err="$(docker buildx imagetools inspect "$1" --format '{{.Manifest.Digest}}' 2>&1 >/dev/null)" || status=$?
    [ "$status" != 0 ] || refuse 1 "$1 already exists; a published tag is never overwritten"
    if printf '%s\n' "$err" | grep -Fxq -e "ERROR: $1: not found" -e "$1: not found"; then
        return 0
    fi
    refuse 2 "cannot tell whether $1 exists (docker exit $status): $(one_line "$err")"
}

case "${1:-}" in
    "") ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

case "${GITHUB_EVENT_NAME:-}" in
    push)
        case "${GITHUB_REF:-}" in
            refs/tags/v*) tag="${GITHUB_REF#refs/tags/}" ;;
            *) refuse 1 "a push of '${GITHUB_REF:-}' publishes no runtime image; only a v* tag push does" ;;
        esac
        check_tag_name "pushed tag" "$tag"
        image="$RELEASE_IMAGE"
        tags="$image:$tag"
        case "$tag" in
            *-*) ;;
            *) tags="$tags,$image:latest" ;;
        esac
        ;;
    workflow_dispatch)
        release_tag="${RELEASE_TAG:-}"
        test_tag="${TEST_TAG:-}"
        if [ -n "$release_tag" ] && [ -n "$test_tag" ]; then
            refuse 1 "set release_tag or test_tag, not both"
        elif [ -n "$release_tag" ]; then
            check_tag_name release_tag "$release_tag"
            image="$RELEASE_IMAGE"
            tag="$release_tag"
        elif [ -n "$test_tag" ]; then
            check_tag_name test_tag "$test_tag"
            image="$TEST_IMAGE"
            tag="$test_tag"
        else
            refuse 1 "a manual run needs release_tag or test_tag"
        fi
        tags="$image:$tag"
        ;;
    *) refuse 1 "unsupported event '${GITHUB_EVENT_NAME:-}'; expected push or workflow_dispatch" ;;
esac

require_absent "$image:$tag"

printf 'image=%s\n' "$image"
printf 'tags=%s\n' "$tags"
printf 'version=%s\n' "$tag"
printf 'publishing %s\n' "$tags" >&2
