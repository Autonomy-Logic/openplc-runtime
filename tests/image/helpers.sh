# shellcheck shell=bash
# shellcheck disable=SC2034
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

BASE_PIN="ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b"
CHECKS_RUN=0
CHECKS_FAILED=0

IFS= read -r -d '' DF_AWK <<'AWK'
function emit(   n, kw, rest) {
    gsub(/[ \t]+/, " ", buf)
    sub(/^ /, "", buf)
    sub(/ $/, "", buf)
    if (buf == "") return
    n = index(buf, " ")
    if (n == 0) { kw = buf; rest = "" } else { kw = substr(buf, 1, n - 1); rest = substr(buf, n + 1) }
    kw = toupper(kw)
    if (kw == "FROM") stage++
    printf "%d\t%s\t%s\n", stage, kw, rest
    buf = ""
}
BEGIN { stage = -1; buf = ""; term = "" }
{
    sub(/\r$/, "")
    line = $0
    if (term != "") {
        probe = line
        if (dash) sub(/^\t+/, "", probe)
        buf = buf " " line
        if (probe == term) { term = ""; emit() }
        next
    }
    if (line ~ /^[ \t]*#/) next
    if (buf == "" && line ~ /^[ \t]*$/) next
    if (line ~ /\\[ \t]*$/) { sub(/\\[ \t]*$/, "", line); buf = buf " " line; next }
    buf = buf " " line
    if (match(buf, /<<-?["']?[A-Za-z_][A-Za-z0-9_]*/)) {
        hd = substr(buf, RSTART, RLENGTH)
        dash = (substr(hd, 3, 1) == "-")
        sub(/^<<-?/, "", hd)
        gsub(/["']/, "", hd)
        term = hd
        next
    }
    emit()
}
END { emit() }
AWK

# Prints the logical instructions of a Dockerfile, one per line: <stage index> TAB <KEYWORD> TAB <arguments>.
df_instructions() {
    awk "$DF_AWK" "$1"
}

# Prints one line per stage of a Dockerfile: <index> TAB <name or -> TAB <image>.
df_stages() {
    local stage kw args tok image name want_name
    local -a toks
    df_instructions "$1" | while IFS=$'\t' read -r stage kw args; do
        [ "$kw" = FROM ] || continue
        read -r -a toks <<<"$args"
        image=""
        name="-"
        want_name=0
        for tok in ${toks[@]+"${toks[@]}"}; do
            if [ "$want_name" = 1 ]; then
                name="$(lower "$tok")"
                break
            fi
            case "$tok" in
                --*) continue ;;
            esac
            if [ -z "$image" ]; then
                image="$tok"
            elif [ "$(lower "$tok")" = as ]; then
                want_name=1
            fi
        done
        printf '%s\t%s\t%s\n' "$stage" "$name" "$image"
    done
}

# Prints the stages whose image is not an earlier stage of the same file: <index> TAB <name> TAB <image>.
df_external_stages() {
    local index name image seen=" "
    while IFS=$'\t' read -r index name image; do
        case "$seen" in
            *" $(lower "$image") "*) ;;
            *) printf '%s\t%s\t%s\n' "$index" "$name" "$image" ;;
        esac
        [ "$name" = - ] || seen="$seen$name "
    done < <(df_stages "$1")
}

# Prints the index of the last stage of a Dockerfile.
df_final_stage() {
    df_stages "$1" | awk -F '\t' 'END { if (NR) print $1 }'
}

# Prints the index of the stage with the given name, or nothing.
df_stage_index() {
    df_stages "$1" | awk -F '\t' -v n="$2" '$2 == n { print $1; exit }'
}

# Prints the instructions of one stage: <KEYWORD> TAB <arguments>.
df_stage_instructions() {
    df_instructions "$1" | awk -F '\t' -v s="$2" '$1 == s { print $2 "\t" $3 }'
}

# Prints the value of a --name=value flag in an instruction's arguments, or nothing.
flag_value() {
    local tok
    local -a toks
    read -r -a toks <<<"$1"
    for tok in ${toks[@]+"${toks[@]}"}; do
        case "$tok" in
            "--$2="*) tok="${tok#--"$2"=}"; tok="${tok%\"}"; printf '%s\n' "${tok#\"}"; return 0 ;;
        esac
    done
}

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

lower() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
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
