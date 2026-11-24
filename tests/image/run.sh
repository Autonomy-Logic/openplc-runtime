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
# shellcheck source=tests/image/publish-plan.sh
. "$TESTS_DIR/publish-plan.sh"
# shellcheck source=tests/image/security.sh
. "$TESTS_DIR/security.sh"

usage() {
    cat <<EOF
usage: run.sh [--build]

Runs the static checks on the repository Dockerfile and its fixtures, the base image check script
against fake gh and docker, the publish plan script against a fake registry, and the Python version
of the webserver CI job.
--build also builds the image once and runs the built-image checks and, against a running container
of it, the TLS, upload, plugin and OPC-UA checks; see built.sh for what they need. It also runs the
base image check against the real docker BuildKit with a fake gh.
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
run_publish_checks
run_security_static_checks
if [ "$build" = 1 ]; then
    run_base_buildkit_checks
    run_built_checks
    run_security_checks "${BUILT_IMAGE:-}"
    built_cleanup
fi
finish
