#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

set -u
set -o pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$TESTS_DIR/../.." && pwd)"
export TESTS_DIR REPO_ROOT

# shellcheck source=tests/image/helpers.sh
. "$TESTS_DIR/helpers.sh"
# shellcheck source=tests/image/static.sh
. "$TESTS_DIR/static.sh"
# shellcheck source=tests/image/built.sh
. "$TESTS_DIR/built.sh"
# shellcheck source=tests/image/base-check.sh
. "$TESTS_DIR/base-check.sh"

usage() {
    cat <<EOF
usage: run.sh [--build]

Runs the static checks on the repository Dockerfile and its fixtures, and the base image check
script against fake gh and docker.
--build also builds the image and runs the built-image checks; see built.sh for what they need.
EOF
}

build=0
for arg in "$@"; do
    case "$arg" in
        --build) build=1 ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

run_static_checks
run_base_checks
[ "$build" = 0 ] || run_built_checks
finish
