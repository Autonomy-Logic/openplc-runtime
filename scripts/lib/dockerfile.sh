# shellcheck shell=bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

BASE_NAME_LABEL="org.opencontainers.image.base.name"
BASE_DIGEST_LABEL="org.opencontainers.image.base.digest"
UTF8_BOM="$(printf '\357\273\277')"

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
BEGIN { stage = -1; buf = "" }
{
    sub(/\r$/, "")
    line = $0
    if (NR == 1 && index(line, bom) == 1) line = substr(line, length(bom) + 1)
    if (line ~ /^[ \t]*#/) next
    if (buf == "" && line ~ /^[ \t]*$/) next
    if (line ~ /\\[ \t]*$/) { sub(/\\[ \t]*$/, "", line); buf = buf " " line; next }
    buf = buf " " line
    emit()
}
END { emit() }
AWK

# Prints the logical instructions of a Dockerfile, one per line: <stage index> TAB <KEYWORD> TAB <arguments>.
df_instructions() {
    awk -v bom="$UTF8_BOM" "$DF_AWK" "$1"
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

# Prints the image a stage builds on, following stage names back to an image that is not a stage of the file.
df_stage_image() {
    local file="$1" index="$2" image ref
    while :; do
        image="$(df_stages "$file" | awk -F '\t' -v s="$index" '$1 == s { print $3; exit }')"
        ref="$(df_stage_index "$file" "$(lower "$image")")"
        { [ -n "$ref" ] && [ "$ref" -lt "$index" ]; } || break
        index="$ref"
    done
    printf '%s\n' "$image"
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

# Prints the key and value of each label LABEL arguments set, one per line: <key> TAB <value>.
label_pairs() {
    local args="$1" sq="'" pair_re key value
    pair_re="^[[:space:]]*(\"[^\"]*\"|[^\"=[:space:]]+)=(\"[^\"]*\"|${sq}[^${sq}]*${sq}|[^[:space:]]*)(.*)\$"
    if ! [[ "$args" =~ $pair_re ]]; then
        key="${args%% *}"
        value=""
        [ "$key" = "$args" ] || value="${args#* }"
        printf '%s\t%s\n' "$key" "$value"
        return
    fi
    while [[ "$args" =~ $pair_re ]]; do
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        args="${BASH_REMATCH[3]}"
        key="${key#\"}"
        key="${key%\"}"
        case "$value" in
            \"*\" | "$sq"*"$sq") value="${value:1:${#value}-2}" ;;
        esac
        printf '%s\t%s\n' "$key" "$value"
    done
}

lower() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

check_no_parser_directive() {
    local found
    found="$(awk -v bom="$UTF8_BOM" '
        { sub(/\r$/, "") }
        NR == 1 && index($0, bom) == 1 { $0 = substr($0, length(bom) + 1) }
        /^[ \t]*#/ { if ($0 ~ /^[ \t]*#[ \t]*[A-Za-z][A-Za-z0-9_.-]*[ \t]*=/) print "line " NR ": " $0; next }
        /^[ \t]*$/ { next }
        { exit }' "$1")"
    [ -z "$found" ] || { echo "parser directive before the first instruction: $found"; return 1; }
}

check_no_heredoc() {
    local found
    found="$(awk '{ sub(/\r$/, "") } /^[ \t]*#/ { next } /<</ { print "line " NR ": " $0 }' "$1")"
    [ -z "$found" ] || { echo "heredoc: $found"; return 1; }
}

check_from_pinned_by_digest() {
    local index name image rc=0
    while IFS=$'\t' read -r index name image; do
        [[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || { echo "FROM $image is not pinned by digest"; rc=1; }
    done < <(df_external_stages "$1")
    return "$rc"
}

check_from_without_variable() {
    local stage kw args rc=0
    while IFS=$'\t' read -r stage kw args; do
        if [ "$kw" = FROM ] && [[ "$args" == *'$'* ]]; then
            echo "FROM uses a variable: $args"
            rc=1
        fi
        if [ "$kw" = ARG ] && [ "$stage" = -1 ]; then
            echo "ARG before the first FROM: $args"
            rc=1
        fi
    done < <(df_instructions "$1")
    return "$rc"
}

check_from_single_pin() {
    local images count
    images="$(df_external_stages "$1" | cut -f 3 | sort -u)"
    count="$(printf '%s' "$images" | grep -c .)"
    [ "$count" = 1 ] || { echo "expected one base reference, found $count: $(echo "$images" | tr '\n' ' ')"; return 1; }
}

# Succeeds when a --from or mount from= value names a stage declared before the given stage.
names_earlier_stage() {
    local file="$1" stage="$2" ref index
    ref="$(lower "$3")"
    if [[ "$ref" =~ ^[0-9]+$ ]]; then
        [ "$ref" -lt "$stage" ]
        return
    fi
    index="$(df_stage_index "$file" "$ref")"
    [ -n "$index" ] && [ "$index" -lt "$stage" ]
}

check_copy_from_stages_only() {
    local stage kw args ref mount tok rc=0
    local -a toks
    while IFS=$'\t' read -r stage kw args; do
        case "$kw" in
            COPY | ADD)
                ref="$(flag_value "$args" from)"
                if [ -n "$ref" ] && ! names_earlier_stage "$1" "$stage" "$ref"; then
                    echo "$kw --from=$ref is not a stage of this file"
                    rc=1
                fi
                ;;
            RUN)
                read -r -a toks <<<"$args"
                for tok in ${toks[@]+"${toks[@]}"}; do
                    case "$tok" in
                        --mount=*) mount="${tok#--mount=}" ;;
                        --*) continue ;;
                        *) break ;;
                    esac
                    ref="$(printf '%s' ",$mount," | sed -n 's/.*,from=\([^,]*\),.*/\1/p')"
                    if [ -n "$ref" ] && ! names_earlier_stage "$1" "$stage" "$ref"; then
                        echo "RUN --mount from=$ref is not a stage of this file"
                        rc=1
                    fi
                done
                ;;
        esac
    done < <(df_instructions "$1")
    return "$rc"
}

check_base_labels() {
    local file="$1" final image kw args key value name="" digest="" has_name=0 has_digest=0 rc=0
    final="$(df_final_stage "$file")"
    [ -n "$final" ] || { echo "no stage"; return 1; }
    image="$(df_stage_image "$file" "$final")"
    [[ "$image" == *@* ]] || { echo "the final stage base $image has no digest to label"; return 1; }
    while IFS=$'\t' read -r kw args; do
        [ "$kw" = LABEL ] || continue
        while IFS=$'\t' read -r key value; do
            case "$key" in
                "$BASE_NAME_LABEL") name="$value"; has_name=1 ;;
                "$BASE_DIGEST_LABEL") digest="$value"; has_digest=1 ;;
            esac
        done < <(label_pairs "$args")
    done < <(df_stage_instructions "$file" "$final")
    if [ "$has_name" = 0 ]; then
        echo "final stage sets no LABEL $BASE_NAME_LABEL"
        rc=1
    elif [ "$name" != "${image%@*}" ]; then
        echo "LABEL $BASE_NAME_LABEL is '$name', not the pinned ${image%@*}"
        rc=1
    fi
    if [ "$has_digest" = 0 ]; then
        echo "final stage sets no LABEL $BASE_DIGEST_LABEL"
        rc=1
    elif [ "$digest" != "${image#*@}" ]; then
        echo "LABEL $BASE_DIGEST_LABEL is '$digest', not the pinned ${image#*@}"
        rc=1
    fi
    return "$rc"
}
