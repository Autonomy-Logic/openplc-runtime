# shellcheck shell=bash
# shellcheck disable=SC2034
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

BASE_PIN="ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b"
CHECKS_RUN=0
CHECKS_FAILED=0

# shellcheck source=scripts/lib/dockerfile.sh
. "$REPO_ROOT/scripts/lib/dockerfile.sh"

# Prints the non-flag operands of COPY or ADD arguments, one per line; the last one is the destination.
copy_operands() {
    local args="$1" tok
    local -a toks
    case "$args" in
        *'['*) args="$(printf '%s' "$args" | sed 's/^.*\[//; s/\].*$//; s/[",]/ /g')" ;;
    esac
    read -r -a toks <<<"$args"
    for tok in ${toks[@]+"${toks[@]}"}; do
        case "$tok" in
            --*) ;;
            *) printf '%s\n' "$tok" ;;
        esac
    done
}

# Prints the command of RUN arguments without its leading flags.
run_command() {
    local cmd="$1"
    while :; do
        case "$cmd" in
            --*' '*) cmd="${cmd#* }" ;;
            *) break ;;
        esac
    done
    case "$cmd" in
        '['*) cmd="$(printf '%s' "$cmd" | sed 's/^\[//; s/\]$//; s/"//g; s/,/ /g')" ;;
    esac
    printf '%s\n' "$cmd"
}

# Prints a text cut to 80 characters, marking the cut.
clip() {
    if [ "${#1}" -gt 80 ]; then printf '%s...\n' "${1:0:80}"; else printf '%s\n' "$1"; fi
}

# Prints the plugin requirements files the installer builds a venv for, relative to the repository root.
plugin_requirements() {
    (cd "$REPO_ROOT" && find core/src/drivers/plugins/python \
        \( -path '*/examples/*' -o -path '*/shared/*' \) -prune -o \
        -name requirements.txt -type f -print) | LC_ALL=C sort
}

# Prints the names of the venvs the image holds: runtime and one per plugin with requirements.
venv_names() {
    printf 'runtime\n'
    plugin_requirements | awk -F / '{ print $(NF - 1) }'
}

# Records one check: report <name> <0 for pass> [reason]; prints one line.
report() {
    CHECKS_RUN=$((CHECKS_RUN + 1))
    if [ "$2" = 0 ]; then
        printf 'ok %s\n' "$1"
    else
        CHECKS_FAILED=$((CHECKS_FAILED + 1))
        printf 'not ok %s - %s\n' "$1" "${3:-failed}"
    fi
}

# Runs a check command and records it; the command's output becomes the failure reason.
run_check() {
    local name="$1" out rc=0
    shift
    out="$("$@" 2>&1)" || rc=$?
    report "$name" "$rc" "$(printf '%s' "$out" | tr '\n' ' ' | sed 's/ *$//')"
}

# Prints the summary line and returns non-zero when any check failed.
finish() {
    if [ "$CHECKS_FAILED" -gt 0 ]; then
        printf '# %s of %s checks failed\n' "$CHECKS_FAILED" "$CHECKS_RUN"
        return 1
    fi
    printf '# all %s checks passed\n' "$CHECKS_RUN"
}
