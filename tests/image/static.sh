# shellcheck shell=bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

STATIC_CHECKS="
no-syntax-directive
from-pinned-by-digest
from-without-variable
from-single-pin
from-base-pin
stage-layout
builder-and-final-on-base
copy-from-stages-only
final-no-apt
final-no-pip-install
final-no-network
final-no-dpkg-excludes
final-no-runtime-dir
final-layer-order
final-metadata-last
venvs-stage-inputs
final-workdir
final-runtime-version
final-expose
final-healthcheck
final-cmd
"

FIXTURE_CASES="
unpinned-from.Dockerfile from-pinned-by-digest
from-variable.Dockerfile from-without-variable
global-arg.Dockerfile from-without-variable
two-pins.Dockerfile from-single-pin
other-base.Dockerfile from-base-pin
syntax-line.Dockerfile no-syntax-directive
extra-stage.Dockerfile stage-layout
named-final.Dockerfile stage-layout
final-from-builder.Dockerfile builder-and-final-on-base
copy-from-image.Dockerfile copy-from-stages-only
mount-from-image.Dockerfile copy-from-stages-only
final-apt-get.Dockerfile final-no-apt
final-apt-get-heredoc.Dockerfile final-no-apt
final-pip-install.Dockerfile final-no-pip-install
final-curl.Dockerfile final-no-network
final-add-url.Dockerfile final-no-network
final-dpkg-excludes.Dockerfile final-no-dpkg-excludes
final-runtime-dir.Dockerfile final-no-runtime-dir
venv-copy-after-app.Dockerfile final-layer-order
build-copy-after-app.Dockerfile final-layer-order
metadata-before-app.Dockerfile final-metadata-last
venvs-copies-app.Dockerfile venvs-stage-inputs
venvs-copies-scripts-dir.Dockerfile venvs-stage-inputs
venvs-mounts-context.Dockerfile venvs-stage-inputs
workdir-changed.Dockerfile final-workdir
runtime-version-default.Dockerfile final-runtime-version
expose-changed.Dockerfile final-expose
healthcheck-changed.Dockerfile final-healthcheck
cmd-changed.Dockerfile final-cmd
"

EXPECTED_HEALTHCHECK='--interval=30s --timeout=10s --start-period=90s --retries=3 CMD curl -kfsS https://127.0.0.1:8443/api/version >/dev/null || exit 1'
EXPECTED_CMD='["bash","./start_openplc.sh"]'

check_no_syntax_directive() {
    local line
    line="$(grep -Ei '^[[:space:]]*#[[:space:]]*syntax[[:space:]]*=' "$1" | head -1)"
    [ -z "$line" ] || { echo "parser directive present: $line"; return 1; }
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

check_from_base_pin() {
    local index name image rc=0
    while IFS=$'\t' read -r index name image; do
        [ "$image" = "$BASE_PIN" ] || { echo "FROM $image is not $BASE_PIN"; rc=1; }
    done < <(df_external_stages "$1")
    return "$rc"
}

check_stage_layout() {
    local names
    names="$(df_stages "$1" | cut -f 2 | tr '\n' ' ')"
    case "$names" in
        "venvs builder - " | "builder venvs - ") return 0 ;;
    esac
    echo "expected stages venvs, builder and an unnamed final stage, found: $names"
    return 1
}

check_builder_and_final_on_base() {
    local final index name image rc=0
    final="$(df_final_stage "$1")"
    while IFS=$'\t' read -r index name image; do
        if [ "$name" = builder ] || [ "$index" = "$final" ]; then
            [ "$image" = "$BASE_PIN" ] || { echo "stage ${name/#-/final} starts FROM $image"; rc=1; }
        fi
    done < <(df_stages "$1")
    [ -n "$(df_stage_index "$1" builder)" ] || { echo "no builder stage"; rc=1; }
    return "$rc"
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

# Prints the final stage's instructions of the given keywords, one per line: <KEYWORD> TAB <arguments>.
final_instructions() {
    local file="$1"
    shift
    df_stage_instructions "$file" "$(df_final_stage "$file")" | while IFS=$'\t' read -r kw args; do
        case " $* " in
            *" $kw "*) printf '%s\t%s\n' "$kw" "$args" ;;
        esac
    done
}

# Fails, naming the instruction, when a final-stage instruction of the given keywords matches a regex.
final_must_not_match() {
    local file="$1" regex="$2" what="$3" kw args cmd rc=0
    shift 3
    while IFS=$'\t' read -r kw args; do
        cmd="$args"
        [ "$kw" = RUN ] && cmd="$(run_command "$args")"
        if [[ "$cmd" =~ $regex ]]; then
            echo "final stage $what: $(clip "$kw $args")"
            rc=1
        fi
    done < <(final_instructions "$file" "$@")
    return "$rc"
}

check_final_no_apt() {
    final_must_not_match "$1" '(^|[^[:alnum:]_-])(apt-get|apt|aptitude)([[:space:]]|$)' "runs apt" RUN
}

check_final_no_pip_install() {
    final_must_not_match "$1" '(^|[^[:alnum:]_.-])pip[0-9.]*[[:space:]]+install([[:space:]]|$)' "runs pip install" RUN
}

check_final_no_network() {
    local rc=0
    final_must_not_match "$1" \
        '(^|[;&|(`]|\$\()[[:space:]]*(sudo[[:space:]]+)?(curl|wget|git[[:space:]]+(clone|fetch|pull|ls-remote|submodule))([[:space:]]|$)' \
        "runs a network command" RUN || rc=1
    final_must_not_match "$1" '(^|[[:space:]])(https?://|git@)' "fetches a remote source" ADD || rc=1
    return "$rc"
}

check_final_no_dpkg_excludes() {
    final_must_not_match "$1" '(path-exclude|path-include|dpkg\.cfg\.d)' "writes dpkg path-excludes" RUN COPY ADD
}

check_final_no_runtime_dir() {
    final_must_not_match "$1" '/run/runtime' "creates /var/run/runtime" RUN COPY ADD
}

# Prints the destination of COPY or ADD arguments with a trailing slash and a leading ./ removed.
copy_destination() {
    local dest
    dest="$(copy_operands "$1" | tail -1)"
    dest="${dest%/}"
    printf '%s\n' "${dest#./}"
}

check_final_layer_order() {
    local file="$1" n=0 kw args from dest rc=0
    while IFS=$'\t' read -r kw args; do
        n=$((n + 1))
        from="$(lower "$(flag_value "$args" from)")"
        dest="$(copy_destination "$args")"
        case "$n" in
            1)
                if [ "$kw" != COPY ] || [ "$from" != venvs ] ||
                    { [ "$dest" != /workdir/venvs ] && [ "$dest" != venvs ]; }; then
                    echo "first layer is not the venvs copy into /workdir/venvs: $(clip "$kw $args")"
                    rc=1
                fi
                ;;
            2)
                if [ "$kw" != COPY ] || [ "$from" != builder ] ||
                    { [ "$dest" != /workdir/build ] && [ "$dest" != build ]; }; then
                    echo "second layer is not the build output copy into /workdir/build: $(clip "$kw $args")"
                    rc=1
                fi
                ;;
            *)
                if [ "$from" = venvs ]; then
                    echo "venvs copied again after the first layer: $(clip "$kw $args")"
                    rc=1
                fi
                ;;
        esac
    done < <(final_instructions "$file" COPY ADD RUN)
    [ "$n" -ge 3 ] || { echo "expected venvs, build output and application layers, found $n layer instructions"; rc=1; }
    return "$rc"
}

check_final_metadata_last() {
    local file="$1" kw args seen_meta="" rc=0
    while IFS=$'\t' read -r kw args; do
        case "$kw" in
            ENV | EXPOSE | HEALTHCHECK | CMD)
                seen_meta="$kw $args"
                ;;
            ARG)
                case "$args" in
                    RUNTIME_VERSION | RUNTIME_VERSION=*) seen_meta="$kw $args" ;;
                esac
                ;;
            COPY | ADD | RUN)
                if [ -n "$seen_meta" ]; then
                    echo "metadata '$(clip "$seen_meta")' comes before the layer '$(clip "$kw $args")'"
                    rc=1
                fi
                ;;
        esac
    done < <(df_stage_instructions "$file" "$(df_final_stage "$file")")
    return "$rc"
}

# Prints the repository files the venvs stage may copy in, relative to the repository root.
venvs_allowed_inputs() {
    printf '%s\n' requirements.txt pyproject.toml install.sh scripts/manage_plugin_venvs.sh
    plugin_requirements
}

# Fails, naming the source, when a venvs-stage source is not exactly one of the allowed files.
venvs_source_allowed() {
    local src="$1" allowed="$2" path found=0
    case "$src" in
        *://* | git@*) echo "venvs stage fetches $src"; return 1 ;;
    esac
    src="${src#./}"
    src="${src#/}"
    [ -n "$src" ] || src=.
    while IFS= read -r path; do
        found=1
        if [ -d "$REPO_ROOT/$path" ]; then
            echo "venvs stage copies the directory $path"
            return 1
        fi
        printf '%s\n' "$allowed" | grep -Fqx -- "$path" || { echo "venvs stage copies $path"; return 1; }
    done < <(cd "$REPO_ROOT" && shopt -s nullglob && for p in $src; do printf '%s\n' "${p%/}"; done)
    [ "$found" = 1 ] || { echo "venvs stage copies $src, which matches no file"; return 1; }
}

check_venvs_stage_inputs() {
    local file="$1" stage allowed kw args src tok mount source rc=0
    local -a toks
    stage="$(df_stage_index "$file" venvs)"
    [ -n "$stage" ] || { echo "no venvs stage"; return 1; }
    allowed="$(venvs_allowed_inputs)"
    while IFS=$'\t' read -r kw args; do
        case "$kw" in
            COPY | ADD)
                if [ -n "$(flag_value "$args" from)" ]; then
                    echo "venvs stage copies from another stage or image: $kw $args"
                    rc=1
                    continue
                fi
                while IFS= read -r src; do
                    venvs_source_allowed "$src" "$allowed" || rc=1
                done < <(copy_operands "$args" | sed '$d')
                ;;
            RUN)
                read -r -a toks <<<"$args"
                for tok in ${toks[@]+"${toks[@]}"}; do
                    case "$tok" in
                        --mount=*) mount=",${tok#--mount=}," ;;
                        --*) continue ;;
                        *) break ;;
                    esac
                    case "$mount" in
                        *,type=bind,* | *,type=*) ;;
                        *) mount="${mount}type=bind," ;;
                    esac
                    case "$mount" in
                        *,type=bind,*) ;;
                        *) continue ;;
                    esac
                    if [[ "$mount" == *,from=* ]]; then
                        echo "venvs stage mounts another stage or image: $tok"
                        rc=1
                        continue
                    fi
                    source="$(printf '%s' "$mount" | sed -n 's/.*,source=\([^,]*\),.*/\1/p')"
                    [ -n "$source" ] || source="$(printf '%s' "$mount" | sed -n 's/.*,src=\([^,]*\),.*/\1/p')"
                    venvs_source_allowed "${source:-.}" "$allowed" || rc=1
                done
                ;;
        esac
    done < <(df_stage_instructions "$file" "$stage")
    return "$rc"
}

check_final_workdir() {
    final_instructions "$1" WORKDIR | cut -f 2 | grep -Eqx '/workdir/?' ||
        { echo "final stage has no WORKDIR /workdir"; return 1; }
}

check_final_runtime_version() {
    local rc=0
    final_instructions "$1" ARG | cut -f 2 | grep -Fqx 'RUNTIME_VERSION=dev' ||
        { echo "final stage has no ARG RUNTIME_VERSION=dev"; rc=1; }
    final_instructions "$1" ENV | cut -f 2 | grep -Eqx 'RUNTIME_VERSION[= ]\$\{?RUNTIME_VERSION\}?' ||
        { echo "final stage has no ENV RUNTIME_VERSION=\${RUNTIME_VERSION}"; rc=1; }
    return "$rc"
}

check_final_expose() {
    final_instructions "$1" EXPOSE | cut -f 2 | tr ' ' '\n' | grep -Eqx '8443(/tcp)?' ||
        { echo "final stage does not EXPOSE 8443"; return 1; }
}

check_final_healthcheck() {
    local got
    got="$(final_instructions "$1" HEALTHCHECK | tail -1 | cut -f 2)"
    [ "$got" = "$EXPECTED_HEALTHCHECK" ] || { echo "HEALTHCHECK is '$got'"; return 1; }
}

check_final_cmd() {
    local got
    got="$(final_instructions "$1" CMD | tail -1 | cut -f 2 | tr -d ' ')"
    [ "$got" = "$EXPECTED_CMD" ] || { echo "CMD is '$got'"; return 1; }
}

# Runs every static check against one Dockerfile, recording one line per check.
static_checks_on() {
    local file="$1" label="$2" id
    for id in $STATIC_CHECKS; do
        run_check "$label/$id" "check_${id//-/_}" "$file"
    done
}

# Prints the static checks that fail on a Dockerfile, separated by spaces.
static_failures_on() {
    local id failed=""
    for id in $STATIC_CHECKS; do
        "check_${id//-/_}" "$1" >/dev/null 2>&1 || failed="$failed $id"
    done
    printf '%s\n' "${failed# }"
}

# Records that the valid fixture passes every static check and that each rule fixture is rejected by its check.
fixture_checks() {
    local dir="$TESTS_DIR/fixtures" fixture id failed
    failed="$(static_failures_on "$dir/valid.Dockerfile")"
    if [ -z "$failed" ]; then
        report "fixture/valid.Dockerfile passes every static check" 0
    else
        report "fixture/valid.Dockerfile passes every static check" 1 "rejected by: $failed"
    fi
    while read -r fixture id; do
        [ -n "$fixture" ] || continue
        if [ ! -f "$dir/$fixture" ]; then
            report "fixture/$fixture is rejected by $id" 1 "missing fixture"
        elif "check_${id//-/_}" "$dir/$fixture" >/dev/null 2>&1; then
            report "fixture/$fixture is rejected by $id" 1 "the check passed"
        else
            report "fixture/$fixture is rejected by $id" 0
        fi
    done <<<"$FIXTURE_CASES"
}

# Fails with a reason when an installer sandbox path is missing.
expect_path() {
    [ -e "$1/$2" ] || { echo "missing $2"; return 1; }
}

# Fails with a reason when the fake tool log holds no line containing every given string.
expect_logged() {
    local log="$1" line needle all
    shift
    while IFS= read -r line; do
        all=1
        for needle in "$@"; do
            [[ "$line" == *"$needle"* ]] || { all=0; break; }
        done
        [ "$all" = 1 ] && return 0
    done <"$log"
    echo "no call with: $*"
    return 1
}

# Copies install.sh, its scripts, the requirements files and the native plugin CMake files into a sandbox root.
install_sandbox() {
    local root="$1" f
    (cd "$REPO_ROOT" && {
        printf '%s\n' install.sh requirements.txt pyproject.toml
        find scripts -type f
        plugin_requirements
        find core/src/drivers/plugins/native -mindepth 2 -maxdepth 2 -name CMakeLists.txt
    }) | while IFS= read -r f; do
        mkdir -p "$root/$(dirname "$f")"
        cp -p "$REPO_ROOT/$f" "$root/$f"
    done
}

# Fails with a reason when a step of the full native install left no trace in the sandbox.
install_expectations() {
    local root="$1" log="$2" req name
    expect_path "$root" venvs/runtime/bin/python3 || return 1
    expect_logged "$log" "venv runtime" "pip install -r $root/requirements.txt" || return 1
    expect_logged "$log" "venv runtime" "pip install -e ." || return 1
    while IFS= read -r req; do
        name="$(basename "$(dirname "$req")")"
        expect_path "$root" "venvs/$name/bin/pip" || return 1
        expect_logged "$log" "venv $name pip install" "-r $root/$req" || return 1
    done < <(plugin_requirements)
    expect_path "$root" build/plc_main || return 1
    expect_path "$root" build/plugins/libethercat_plugin.so || return 1
    expect_path "$root" build/plugins/libs7comm_plugin.so || return 1
    expect_path "$root" build/etherdog || return 1
    expect_path "$root" .installed
}

# Runs install.sh --native with no step in a sandbox with fake tools and checks every install step ran.
check_install_native_default() {
    local t root log rc=0
    t="$(mktemp -d)"
    t="$(cd "$t" && pwd -P)"
    root="$t/root"
    log="$t/calls.log"
    mkdir -p "$root" "$t/bin"
    cp "$TESTS_DIR"/fixtures/install/bin/* "$t/bin/"
    cp "$TESTS_DIR/fixtures/install/venv-tool" "$t/venv-tool"
    chmod +x "$t"/bin/* "$t/venv-tool"
    install_sandbox "$root"
    (cd "$t" && env -u ETHERDOG_SRC -u ETHERDOG_REPO -u ETHERDOG_REF \
        PATH="$t/bin:$PATH" FAKE_LOG="$log" FAKE_VENV_TOOL="$t/venv-tool" \
        bash "$root/install.sh" --native) >"$t/out" 2>&1 </dev/null || rc=$?
    : >>"$log"
    if [ "$rc" != 0 ]; then
        echo "install.sh --native exited $rc: $(tail -3 "$t/out" | tr '\n' ' ')"
    else
        install_expectations "$root" "$log" || rc=1
    fi
    rm -rf "$t"
    return "$rc"
}

# Runs the static checks on the repository Dockerfile, the fixture self-checks and the installer default check.
run_static_checks() {
    static_checks_on "$REPO_ROOT/Dockerfile" static
    fixture_checks
    run_check "install/native-without-step-runs-every-step" check_install_native_default
}
