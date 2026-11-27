# shellcheck shell=bash
# shellcheck disable=SC2034
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

TAG_RE='[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}'
TAG_PATTERN="^$TAG_RE\$"

# Prints a text on one line, its non-empty lines joined by "; ".
one_line() {
    printf '%s\n' "$1" | awk 'NF { printf "%s%s", sep, $0; sep = "; " } END { print "" }'
}
