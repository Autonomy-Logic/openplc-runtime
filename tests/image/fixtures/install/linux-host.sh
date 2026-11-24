# shellcheck shell=bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

# Reports Linux to the package manager detection of install.sh and MSYS elsewhere, so it needs no root and writes no system directory.
uname() {
    case "${1:-}" in
        -m) echo x86_64 ;;
        *)
            case " ${FUNCNAME[*]} " in
                *" install_dependencies "*) echo Linux ;;
                *) echo MSYS_NT-10.0-26100 ;;
            esac
            ;;
    esac
}
