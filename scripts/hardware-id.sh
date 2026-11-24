#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

# Hardware ID v2 reader (spec: docs/HARDWARE_ID.md). Canonical copy: installers embed the
# block between the BEGIN/END markers verbatim, and their parity checks compare it byte for byte.
#
# Usage: hardware-id.sh               print the ID (hw2:<64 hex>), exit 2 when none can be derived
#        hardware-id.sh --elements    print "<kind> <element hash>" lines
#        hardware-id.sh --record      print the ID line followed by the element lines
#        hardware-id.sh --store FILE  write the record to FILE unless it already holds it; keep a
#                                     well-formed record when no ID can be derived
# OPENPLC_HWID_ROOT replaces / for every read (tests only). Element lines in
# /etc/openplc-runtime/elements (OPENPLC_HWID_ELEMENTS_FILE overrides the path) are added.

# BEGIN hardware-id v2
hw2_root() { printf '%s' "${OPENPLC_HWID_ROOT:-}"; }

hw2_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        shasum -a 256 | cut -d' ' -f1
    fi
}

hw2_lower() { tr '[:upper:]' '[:lower:]'; }

# Reads a file, drops NUL bytes, trims surrounding whitespace and lower-cases it.
hw2_read() {
    [ -r "$1" ] || return 1
    tr -d '\000' <"$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | hw2_lower
}

hw2_cpuinfo_field() {
    sed -n "s/^$1[[:space:]]*:[[:space:]]*//p" "$(hw2_root)/proc/cpuinfo" 2>/dev/null |
        head -n 1 | sed 's/[[:space:]]*$//' | hw2_lower
}

hw2_refused_host() {
    local r f
    r="$(hw2_root)"
    for f in "$r/proc/sys/kernel/osrelease" "$r/proc/version"; do
        if [ -r "$f" ] && grep -qi -e microsoft -e wsl -e linuxkit "$f"; then
            return 0
        fi
    done
    return 1
}

hw2_is_vm() {
    local r vendor product
    r="$(hw2_root)"
    if grep -qiw hypervisor "$r/proc/cpuinfo" 2>/dev/null; then
        return 0
    fi
    vendor="$(hw2_read "$r/sys/class/dmi/id/sys_vendor")"
    product="$(hw2_read "$r/sys/class/dmi/id/product_name")"
    case "$vendor" in
        *vmware* | *qemu* | *kvm* | *innotek* | *virtualbox* | *xen* | *"amazon ec2"* | *google* | *parallels*) return 0 ;;
    esac
    case "$product" in
        *vmware* | *qemu* | *kvm* | *virtualbox* | *xen* | *"google compute engine"* | *parallels*) return 0 ;;
    esac
    if [ "$vendor" = "microsoft corporation" ] && [ "$product" = "virtual machine" ]; then
        return 0
    fi
    return 1
}

hw2_uuid_ok() {
    local bare
    bare="$(printf '%s' "$1" | tr -d '-')"
    case "$bare" in
        "" | *[!0-9a-f]*) return 1 ;;
    esac
    [ -n "$(printf '%s' "$bare" | tr -d '0')" ] || return 1
    [ -n "$(printf '%s' "$bare" | tr -d 'f')" ] || return 1
    [ "$1" != "03000200-0400-0500-0006-000700080009" ]
}

hw2_board_serial_ok() {
    case "$1" in
        "" | "to be filled by o.e.m." | "default string" | "not specified" | "0123456789") return 1 ;;
    esac
    return 0
}

# Succeeds when the NIC's device path or an ancestor is marked removable, or sits on USB
# without a hardwired port.
hw2_nic_detachable() {
    local dev on_usb hardwired
    dev="$(cd -P "$1/device" 2>/dev/null && pwd)" || return 0
    on_usb=0
    hardwired=0
    while [ -n "$dev" ] && [ "$dev" != "/" ] && [ "$dev" != "$(hw2_root)" ]; do
        if [ "$(hw2_read "$dev/removable")" = "removable" ]; then
            return 0
        fi
        case "${dev##*/}" in
            usb[0-9]*) on_usb=1 ;;
        esac
        if [ "$(hw2_read "$dev/port/connect_type")" = "hardwired" ]; then
            hardwired=1
        fi
        dev="${dev%/*}"
    done
    [ "$on_usb" -eq 1 ] && [ "$hardwired" -eq 0 ]
}

hw2_macs() {
    local r nic mac first
    r="$(hw2_root)"
    for nic in "$r"/sys/class/net/*; do
        [ -e "$nic/device" ] || continue
        [ -e "$nic/wireless" ] && continue
        [ -e "$nic/phy80211" ] && continue
        [ "$(hw2_read "$nic/addr_assign_type")" = "0" ] || continue
        mac="$(hw2_read "$nic/address")"
        case "$mac" in
            [0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]) ;;
            *) continue ;;
        esac
        [ "$mac" = "00:00:00:00:00:00" ] && continue
        first=$((16#${mac%%:*}))
        [ $((first & 2)) -eq 0 ] || continue
        hw2_nic_detachable "$nic" && continue
        printf '%s\n' "$mac"
    done | sort -u
}

hw2_element() { printf '%s %s\n' "$1" "$(printf 'openplc-hw-v2|%s|%s' "$1" "$2" | hw2_sha256)"; }

# Prints "<kind> <hash>" per element read from this hardware.
hw2_hw_elements() {
    local r v mac vendor model
    r="$(hw2_root)"
    v="$(hw2_read "$r/proc/device-tree/serial-number")"
    if [ -n "$v" ]; then
        hw2_element serial "$v"
    fi
    v="$(hw2_read "$r/sys/class/dmi/id/product_uuid")"
    if hw2_uuid_ok "$v"; then
        hw2_element uuid "$v"
    fi
    v="$(hw2_read "$r/sys/class/dmi/id/board_serial")"
    if hw2_board_serial_ok "$v"; then
        hw2_element board "$v"
    fi
    if ! hw2_is_vm; then
        for mac in $(hw2_macs); do
            hw2_element mac "$mac"
        done
    fi
    vendor="$(hw2_cpuinfo_field vendor_id)"
    model="$(hw2_cpuinfo_field 'model name')"
    if [ -z "$vendor" ]; then
        vendor="$(hw2_cpuinfo_field 'CPU implementer')"
        model="$(hw2_cpuinfo_field 'CPU part')"
    fi
    if [ -n "$vendor$model" ]; then
        hw2_element cpu "$vendor|$model"
    fi
    return 0
}

# Prints the valid "<kind> <hash>" lines of the elements file (OPENPLC_HWID_ELEMENTS_FILE).
hw2_file_elements() {
    local f line
    f="${OPENPLC_HWID_ELEMENTS_FILE:-$(hw2_root)/etc/openplc-runtime/elements}"
    [ -f "$f" ] && [ -r "$f" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        if [[ "$line" =~ ^(serial|uuid|board|mac|cpu|instance)\ [0-9a-f]{64}$ ]]; then
            printf '%s\n' "$line"
        fi
    done <"$f"
    return 0
}

# Prints "<kind> <hash>" per element: this hardware's (none on a refused host), then the elements
# file's, without duplicate hashes. Returns 1 when no strong element is present.
hw2_elements() {
    local computed="" added all
    hw2_refused_host || computed="$(hw2_hw_elements)"
    added="$(hw2_file_elements)"
    all="$(printf '%s\n%s\n' "$computed" "$added" | awk 'NF && !seen[$2]++')"
    [ -z "$all" ] || printf '%s\n' "$all"
    grep -qE '^(serial|uuid|board|mac|instance) ' <<<"$all"
}

hw2_id_of() { printf 'hw2:%s\n' "$(printf '%s\n' "$1" | cut -d' ' -f2 | sort | hw2_sha256)"; }

# Prints hw2:<64 hex>; returns 1 when no ID can be derived.
hw2_id() {
    local elements
    elements="$(hw2_elements)" || return 1
    hw2_id_of "$elements"
}

# Prints the ID line followed by the element lines; returns 1 when no ID can be derived.
hw2_record() {
    local elements
    elements="$(hw2_elements)" || return 1
    hw2_id_of "$elements"
    printf '%s\n' "$elements"
}

# Succeeds when $1 holds a record: the ID line, then "<kind> <hash>" lines; blank lines and
# surrounding whitespace are ignored.
hw2_record_ok() {
    local line first=1
    [ -r "$1" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [ -z "$line" ] && continue
        if [ "$first" -eq 1 ]; then
            [[ "$line" =~ ^hw2:[0-9a-f]{64}$ ]] || return 1
            first=0
        else
            [[ "$line" =~ ^(serial|uuid|board|mac|cpu|instance)\ [0-9a-f]{64}$ ]] || return 1
        fi
    done <"$1"
    [ "$first" -eq 0 ]
}

# Writes the live record to $1 atomically unless $1 already holds it. When no ID can be derived,
# a well-formed record in $1 is kept. Returns 1 when $1 holds no record afterwards.
hw2_store() {
    local tmp
    tmp="$1.tmp.$$"
    if hw2_record >"$tmp"; then
        if hw2_record_ok "$1" && [ "$(<"$tmp")" = "$(<"$1")" ]; then
            rm -f "$tmp"
            return 0
        fi
        chmod 644 "$tmp" && mv -f "$tmp" "$1"
    else
        rm -f "$tmp"
        hw2_record_ok "$1"
    fi
}
# END hardware-id v2

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    set -u
    case "${1:-}" in
        --elements) hw2_elements || exit 2 ;;
        --record) hw2_record || exit 2 ;;
        --store)
            [ -n "${2:-}" ] || { echo "usage: $0 --store FILE" >&2; exit 64; }
            hw2_store "$2" || exit 2
            ;;
        *) hw2_id || exit 2 ;;
    esac
fi
