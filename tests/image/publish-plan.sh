# shellcheck shell=bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

PLAN_SCRIPT="$REPO_ROOT/scripts/publish-plan.sh"
RELEASE_IMAGE="ghcr.io/autonomy-logic/openplc-runtime"
TEST_IMAGE="ghcr.io/autonomy-logic/openplc-runtime-test"
TESTS_WORKFLOW="$REPO_ROOT/.github/workflows/tests.yml"
PLAN_DIGEST="sha256:1111111111111111111111111111111111111111111111111111111111111111"
REGISTRY="$RELEASE_IMAGE:latest $RELEASE_IMAGE:v4.3.0 $RELEASE_IMAGE:v4.3.1 $RELEASE_IMAGE:v4.3.2 $TEST_IMAGE:measure-0 $TEST_IMAGE:v4.4.0"
BOOTLOADER_TAGS='${{ steps.plan.outputs.releasable == '"'true'"' && format('"'{0}:{1},{0}:latest'"', steps.plan.outputs.image, steps.plan.outputs.version) || format('"'{0}:{1}'"', steps.plan.outputs.image, steps.plan.outputs.version) }}'
PLAN_WORK=""

PLAN_DOCKER_ERROR=""

# Links the tools the script may call, docker excluded, into a directory that becomes the whole PATH.
plan_tools_without_docker() {
    local tool path
    for tool in awk grep sed tr cat head tail cut sort env dirname basename; do
        path="$(command -v "$tool")" && ln -s "$path" "$1/$tool"
    done
}

# Runs the script in a clean environment with the fake docker first on PATH:
# plan_run <case> <event> <ref> <release_tag> <test_tag>; sets PLAN_RC, PLAN_STDOUT, PLAN_STDERR.
# PLAN_DOCKER_ERROR becomes the fake's FAKE_DOCKER_ERROR; "missing" runs with no docker on PATH at all.
plan_run() {
    local ref_type=branch path
    CASE_DIR="$PLAN_WORK/$1"
    mkdir -p "$CASE_DIR/bin"
    : >"$CASE_DIR/docker.log"
    if [ "$PLAN_DOCKER_ERROR" = missing ]; then
        plan_tools_without_docker "$CASE_DIR/bin"
        path="$CASE_DIR/bin"
    else
        cp "$TESTS_DIR/fake-docker.sh" "$CASE_DIR/bin/docker"
        chmod +x "$CASE_DIR/bin/docker"
        path="$CASE_DIR/bin:$PATH"
    fi
    case "$3" in
        refs/tags/*) ref_type=tag ;;
    esac
    PLAN_RC=0
    (cd "$REPO_ROOT" && env -i PATH="$path" HOME="$HOME" FAKE_DOCKER_ERROR="$PLAN_DOCKER_ERROR" \
        GITHUB_EVENT_NAME="$2" GITHUB_REF="$3" GITHUB_REF_NAME="${3#refs/*/}" GITHUB_REF_TYPE="$ref_type" \
        RELEASE_TAG="$4" INPUT_RELEASE_TAG="$4" TEST_TAG="$5" INPUT_TEST_TAG="$5" \
        FAKE_DOCKER_LOG="$CASE_DIR/docker.log" FAKE_DOCKER_EXISTING="$REGISTRY" FAKE_DOCKER_DIGEST="$PLAN_DIGEST" \
        "$BASH" "$PLAN_SCRIPT" >"$CASE_DIR/stdout" 2>"$CASE_DIR/stderr" </dev/null) || PLAN_RC=$?
    PLAN_STDOUT="$(/bin/cat "$CASE_DIR/stdout")"
    PLAN_STDERR="$(/bin/cat "$CASE_DIR/stderr")"
}

# Prints the last run's exit code, stdout and stderr on one line.
plan_result() {
    printf 'exit %s, stdout: %s, stderr: %s\n' "$PLAN_RC" "$(clip "$(printf '%s' "$PLAN_STDOUT" | tr '\n' ' ')")" \
        "$(clip "$(printf '%s' "${PLAN_STDERR//"$REPO_ROOT"\//}" | tr '\n' ' ')")"
}

script_exists() {
    [ -f "$PLAN_SCRIPT" ] || { echo "scripts/publish-plan.sh does not exist"; return 1; }
}

# Prints the value of the one <key>= line of the last run's stdout; fails when there is not exactly one.
plan_value() {
    local count
    count="$(printf '%s\n' "$PLAN_STDOUT" | grep -c "^$1=")"
    [ "$count" = 1 ] || { echo "expected one $1= line, got $count; $(plan_result)"; return 1; }
    printf '%s\n' "$PLAN_STDOUT" | sed -n "s/^$1=//p"
}

# Prints the tags of the last run, sorted, one per line; an item may be <image>:<tag> or a bare tag.
plan_tags() {
    local image="$1" tags item
    tags="$(plan_value tags)" || { printf '%s\n' "$tags"; return 1; }
    for item in $(printf '%s' "$tags" | tr ',' ' '); do
        case "$item" in
            */*)
                [ "${item%:*}" = "$image" ] || { echo "tag $item is not on the image $image"; return 1; }
                printf '%s\n' "${item##*:}"
                ;;
            *) printf '%s\n' "$item" ;;
        esac
    done | LC_ALL=C sort
}

# Checks a successful plan: expect_plan <image> <version or - for any> <tag>...
expect_plan() {
    local image="$1" version="$2" got want rc=0
    shift 2
    script_exists || return 1
    [ "$PLAN_RC" = 0 ] || { echo "expected exit 0, got $(plan_result)"; return 1; }
    got="$(plan_value image)" || { echo "$got"; return 1; }
    [ "$got" = "$image" ] || { echo "image: expected $image, got $got"; rc=1; }
    got="$(plan_tags "$image")" || { echo "$got"; return 1; }
    want="$(printf '%s\n' "$@" | LC_ALL=C sort)"
    [ "$got" = "$want" ] || { echo "tags: expected [$(printf '%s' "$want" | tr '\n' ' ')], got [$(printf '%s' "$got" | tr '\n' ' ')]"; rc=1; }
    got="$(plan_value version)" || { echo "$got"; return 1; }
    if [ "$version" = - ]; then
        [ -n "$got" ] || { echo "version: expected a value, got an empty one"; rc=1; }
    else
        [ "$got" = "$version" ] || { echo "version: expected $version, got $got"; rc=1; }
    fi
    return "$rc"
}

# Checks a refused plan: non-zero exit, a message on stderr and no image= or tags= line on stdout.
expect_plan_error() {
    script_exists || return 1
    [ "$PLAN_RC" != 0 ] || { echo "expected a non-zero exit, got $(plan_result)"; return 1; }
    ! printf '%s\n' "$PLAN_STDOUT" | grep -Eq '^(image|tags)=' || { echo "a refused run still prints a plan: $(plan_result)"; return 1; }
    [ -n "$PLAN_STDERR" ] || { echo "a refused run says nothing on stderr: $(plan_result)"; return 1; }
}

# Checks that docker was asked whether a reference exists.
expect_existence_checked() {
    grep -Eq "^(manifest inspect|buildx imagetools inspect) (.* )?$1( |$)" "$CASE_DIR/docker.log" ||
        { echo "docker was not asked about $1; docker calls: $(tr '\n' ';' <"$CASE_DIR/docker.log")"; return 1; }
}

check_plan_stable_tag_push() {
    plan_run stable-push push refs/tags/v4.3.3 "" ""
    expect_plan "$RELEASE_IMAGE" v4.3.3 v4.3.3 latest
}

check_plan_prerelease_tag_push() {
    plan_run prerelease-push push refs/tags/v4.4.0-rc.1 "" ""
    expect_plan "$RELEASE_IMAGE" v4.4.0-rc.1 v4.4.0-rc.1
}

check_plan_manual_test_tag() {
    plan_run test-tag workflow_dispatch refs/heads/development "" measure-a
    expect_plan "$TEST_IMAGE" - measure-a
}

check_plan_manual_test_tag_from_stable_tag_ref() {
    plan_run test-tag-on-tag-ref workflow_dispatch refs/tags/v4.3.2 "" measure-b
    expect_plan "$TEST_IMAGE" - measure-b
}

check_plan_manual_release_tag() {
    plan_run release-tag workflow_dispatch refs/tags/v4.4.1 v4.4.1 ""
    expect_plan "$RELEASE_IMAGE" v4.4.1 v4.4.1
}

check_plan_manual_release_tag_from_main() {
    plan_run release-tag-on-main workflow_dispatch refs/heads/main v4.4.2 ""
    expect_plan "$RELEASE_IMAGE" v4.4.2 v4.4.2
}

check_plan_manual_without_tag_fails() {
    plan_run no-tag workflow_dispatch refs/heads/development "" ""
    expect_plan_error
}

check_plan_manual_with_both_tags_fails() {
    plan_run both-tags workflow_dispatch refs/heads/development v4.4.3 measure-c
    expect_plan_error
}

check_plan_existing_test_tag_fails() {
    local rc=0
    plan_run test-tag-exists workflow_dispatch refs/heads/development "" measure-0
    expect_plan_error || rc=1
    expect_existence_checked "$TEST_IMAGE:measure-0" || rc=1
    return "$rc"
}

check_plan_existing_release_tag_fails() {
    local rc=0
    plan_run release-tag-exists workflow_dispatch refs/tags/v4.3.1 v4.3.1 ""
    expect_plan_error || rc=1
    expect_existence_checked "$RELEASE_IMAGE:v4.3.1" || rc=1
    return "$rc"
}

# Checks that an answer other than a clear "not found" for the reference refuses: exit 2, an error: line on
# stderr, no image= or tags= line on stdout: registry_error_refused <fake error>.
registry_error_refused() {
    local rc=0
    PLAN_DOCKER_ERROR="$1"
    plan_run "registry-$1" workflow_dispatch refs/heads/development v4.4.1 ""
    PLAN_DOCKER_ERROR=""
    expect_plan_error || return 1
    [ "$PLAN_RC" = 2 ] || { echo "expected exit 2, got $(plan_result)"; rc=1; }
    printf '%s\n' "$PLAN_STDERR" | grep -q '^error: ' || { echo "no error: line on stderr: $(plan_result)"; rc=1; }
    return "$rc"
}

check_plan_registry_unauthorized_refused() {
    registry_error_refused unauthorized
}

check_plan_registry_token_not_found_refused() {
    registry_error_refused token-not-found
}

check_plan_registry_rate_limited_refused() {
    registry_error_refused rate-limited
}

check_plan_registry_network_error_refused() {
    registry_error_refused network
}

check_plan_registry_not_found_for_other_reference_refused() {
    registry_error_refused other-ref-not-found
}

check_plan_registry_empty_error_refused() {
    registry_error_refused empty
}

check_plan_docker_missing_refused() {
    registry_error_refused missing
}

check_plan_test_tag_only_in_release_package_passes() {
    plan_run test-tag-in-release-package workflow_dispatch refs/heads/development "" v4.3.2
    expect_plan "$TEST_IMAGE" - v4.3.2
}

check_plan_release_tag_only_in_test_package_passes() {
    plan_run release-tag-in-test-package workflow_dispatch refs/tags/v4.4.0 v4.4.0 ""
    expect_plan "$RELEASE_IMAGE" v4.4.0 v4.4.0
}

check_plan_latest_only_for_stable_tag_push() {
    local spec name event ref release test rc=0
    script_exists || return 1
    plan_run latest-stable push refs/tags/v4.3.3 "" ""
    plan_tags "$RELEASE_IMAGE" 2>/dev/null | grep -qx latest || { echo "stable tag push v4.3.3 has no latest: $(plan_result)"; rc=1; }
    for spec in \
        "latest-rc push refs/tags/v4.4.0-rc.1 - -" \
        "latest-beta push refs/tags/v4.4.0-beta.2 - -" \
        "latest-test-main workflow_dispatch refs/heads/main - measure-d" \
        "latest-test-tag-ref workflow_dispatch refs/tags/v4.3.2 - measure-e" \
        "latest-release-tag-ref workflow_dispatch refs/tags/v4.4.1 v4.4.1 -" \
        "latest-release-main workflow_dispatch refs/heads/main v4.4.2 -" \
        "latest-release-latest workflow_dispatch refs/heads/main latest -"; do
        read -r name event ref release test <<<"$spec"
        [ "$release" != - ] || release=""
        [ "$test" != - ] || test=""
        plan_run "$name" "$event" "$ref" "$release" "$test"
        [ "$PLAN_RC" != 0 ] && continue
        printf '%s\n' "$PLAN_STDOUT" | sed -n 's/^tags=//p' | tr ',' '\n' | tr ' ' '\n' | grep -Eq '(^|:)latest$' &&
            { echo "$event $ref release_tag=$release test_tag=$test publishes latest: $(plan_result)"; rc=1; }
    done
    return "$rc"
}

check_plan_stdout_holds_only_output_lines() {
    local name bad rc=0
    script_exists || return 1
    for name in stdout-push stdout-dispatch; do
        if [ "$name" = stdout-push ]; then
            plan_run "$name" push refs/tags/v4.3.3 "" ""
        else
            plan_run "$name" workflow_dispatch refs/heads/development "" measure-f
        fi
        [ "$PLAN_RC" = 0 ] || { echo "$name: expected exit 0, got $(plan_result)"; rc=1; continue; }
        bad="$(printf '%s\n' "$PLAN_STDOUT" | grep -Ev '^[A-Za-z_][A-Za-z0-9_-]*=' | grep -v '^$')"
        [ -z "$bad" ] || { echo "$name: stdout has lines GITHUB_OUTPUT cannot take: $(clip "$(printf '%s' "$bad" | tr '\n' ';')")"; rc=1; }
    done
    return "$rc"
}

# Prints the block of the first docker.yml build step matching an extended regex, its keys at the top indentation.
build_step_block() {
    local n
    n="$(build_step_index "$1")"
    [ -n "$n" ] || return 0
    yaml_path "$DOCKER_WORKFLOW" jobs build steps | awk -v n="$n" '
        function indent(s) { match(s, /^ */); return RLENGTH }
        /^[ \t]*#/ || /^[ \t]*$/ { next }
        {
            ind = indent($0)
            if (top < 0) top = ind
            if (ind == top && substr($0, ind + 1, 2) == "- ") { item++; if (item == n) { print substr($0, 1, ind) "  " substr($0, ind + 3) }; next }
            if (item == n) print
        }' top=-1
}

# Prints the build job of docker.yml without comments.
build_job_text() {
    yaml_path "$DOCKER_WORKFLOW" jobs build | sed -e '/^[[:space:]]*#/d' -e 's/[[:space:]]#.*$//'
}

# Prints the id of the build step that runs scripts/publish-plan.sh.
plan_step_id() {
    build_step_block 'publish-plan\.sh' | yaml_child id | tr -d "\"'"
}

check_workflow_docker_dispatch_inputs() {
    local inputs input rc=0
    inputs="$(yaml_path "$DOCKER_WORKFLOW" on workflow_dispatch inputs)"
    for input in platforms release_tag test_tag; do
        printf '%s\n' "$inputs" | yaml_child "$input" | grep -q . || { echo "workflow_dispatch has no $input input"; rc=1; }
    done
    return "$rc"
}

check_workflow_docker_build_tags_from_plan() {
    local id tags
    [ -n "$(build_step_block 'publish-plan\.sh')" ] || { echo "no step of the build job runs scripts/publish-plan.sh"; return 1; }
    id="$(plan_step_id)"
    [ -n "$id" ] || { echo "the step running scripts/publish-plan.sh has no id"; return 1; }
    tags="$(build_step_block 'docker/build-push-action@' | yaml_child with | yaml_child tags)"
    [[ "$tags" == *"steps.$id.outputs.tags"* ]] || { echo "build-push-action tags do not come from steps.$id.outputs.tags: $(clip "$tags")"; return 1; }
}

check_workflow_docker_build_plan_gets_dispatch_inputs() {
    local step rc=0 input
    step="$(build_step_block 'publish-plan\.sh')"
    [ -n "$step" ] || { echo "no step of the build job runs scripts/publish-plan.sh"; return 1; }
    for input in release_tag test_tag; do
        printf '%s\n' "$step" | grep -Eq "inputs\.$input([^[:alnum:]_]|$)" || { echo "the plan step does not pass inputs.$input"; rc=1; }
    done
    return "$rc"
}

check_workflow_docker_build_version_from_plan() {
    local id args
    id="$(plan_step_id)"
    [ -n "$id" ] || { echo "no step of the build job with an id runs scripts/publish-plan.sh"; return 1; }
    args="$(build_step_block 'docker/build-push-action@' | yaml_child with | yaml_child build-args)"
    printf '%s\n' "$args" | grep -Eq "RUNTIME_VERSION=\\\$\\{\\{[[:space:]]*steps\\.$id\\.outputs\\.version[[:space:]]*\\}\\}" ||
        { echo "RUNTIME_VERSION is not steps.$id.outputs.version: $(clip "$(printf '%s' "$args" | tr '\n' ' ')")"; return 1; }
}

check_workflow_docker_build_names_no_latest() {
    local hits
    hits="$(build_job_text | grep -Ev '^[[:space:]]*-?[[:space:]]*name:' | grep -E '(^|[^[:alnum:]_-])latest([^[:alnum:]_-]|$)')"
    [ -z "$hits" ] || { echo "the build job names latest itself: $(clip "$(printf '%s' "$hits" | tr -s ' ' | tr '\n' ';')")"; return 1; }
}

check_workflow_docker_build_source_date_epoch_from_commit() {
    local job rc=0
    job="$(build_job_text)"
    printf '%s\n' "$job" | grep -q 'SOURCE_DATE_EPOCH' || { echo "the build job does not set SOURCE_DATE_EPOCH"; rc=1; }
    printf '%s\n' "$job" | grep -Eq 'git (log|show) .*%ct' || { echo "the build job does not read the commit time (git log or show with %ct)"; rc=1; }
    return "$rc"
}

check_workflow_docker_build_rewrites_timestamps() {
    local outputs
    outputs="$(build_step_block 'docker/build-push-action@' | yaml_child with | yaml_child outputs)"
    [[ "$outputs" == *rewrite-timestamp=true* ]] || { echo "build-push-action outputs do not set rewrite-timestamp=true: $(clip "$outputs")"; return 1; }
}

# Checks that build-push-action sets an input to a value other than false: build_push_enables <input>.
build_push_enables() {
    local value
    value="$(build_step_block 'docker/build-push-action@' | yaml_child with | yaml_child "$1" | tr -d "\"' ")"
    [ -n "$value" ] && [ "$value" != false ] || { echo "build-push-action does not enable $1 (value: ${value:-unset})"; return 1; }
}

check_workflow_docker_build_provenance() {
    build_push_enables provenance
}

check_workflow_docker_build_sbom() {
    build_push_enables sbom
}

check_workflow_docker_build_reads_attestations() {
    local rc=0
    yaml_path "$DOCKER_WORKFLOW" jobs build permissions | grep -Eq '^[[:space:]]*attestations:[[:space:]]*read[[:space:]]*$' ||
        { echo "the build job does not grant attestations: read"; rc=1; }
    build_step_block 'verify-base-image\.sh' | yaml_child env | grep -Eq '^[[:space:]]*GH_TOKEN:[[:space:]]*[^[:space:]]' ||
        { echo "the base check step sets no GH_TOKEN"; rc=1; }
    return "$rc"
}

check_workflow_docker_bootloader_keeps_own_plan() {
    local job steps decide rc=0
    job="$(yaml_path "$DOCKER_WORKFLOW" jobs bootloader)"
    steps="$(printf '%s\n' "$job" | yaml_child steps | yaml_items)"
    decide="$(printf '%s\n' "$steps" | grep -F 'name: Decide what needs publishing')"
    [ -n "$decide" ] || { echo "the bootloader job has no Decide what needs publishing step"; return 1; }
    [[ "$decide" == *'id: plan'* && "$decide" == *'docker manifest inspect "$image:$version"'* ]] ||
        { echo "the bootloader Decide step changed: $(clip "$decide")"; rc=1; }
    ! printf '%s\n' "$job" | grep -q 'publish-plan\.sh' || { echo "the bootloader job runs scripts/publish-plan.sh"; rc=1; }
    printf '%s\n' "$job" | sed 's/^[[:space:]]*//' | grep -Fxq "tags: $BOOTLOADER_TAGS" ||
        { echo "the bootloader tags expression changed: $(clip "$(printf '%s\n' "$job" | grep -E '^[[:space:]]*tags:' | tr -s ' ')")"; rc=1; }
    return "$rc"
}

check_workflow_tests_shellcheck_publish_plan() {
    awk '{ if (sub(/\\[[:space:]]*$/, "")) { buf = buf $0; next } print buf $0; buf = "" }' "$TESTS_WORKFLOW" |
        grep -E '(^|[[:space:]])shellcheck[[:space:]]' | grep -Eq '(^|[[:space:]])scripts/(publish-plan\.sh|\*\.sh)([[:space:]]|$)' ||
        { echo "tests.yml does not shellcheck scripts/publish-plan.sh"; return 1; }
}

# Runs the publish plan script against a fake registry and checks the docker.yml build job that uses it.
run_publish_checks() {
    PLAN_WORK="$(mktemp -d)"
    PLAN_WORK="$(cd "$PLAN_WORK" && pwd -P)"
    run_check "plan/stable-tag-push" check_plan_stable_tag_push
    run_check "plan/prerelease-tag-push" check_plan_prerelease_tag_push
    run_check "plan/manual-test-tag" check_plan_manual_test_tag
    run_check "plan/manual-test-tag-from-stable-tag-ref" check_plan_manual_test_tag_from_stable_tag_ref
    run_check "plan/manual-release-tag" check_plan_manual_release_tag
    run_check "plan/manual-release-tag-from-main" check_plan_manual_release_tag_from_main
    run_check "plan/manual-without-tag-fails" check_plan_manual_without_tag_fails
    run_check "plan/manual-with-both-tags-fails" check_plan_manual_with_both_tags_fails
    run_check "plan/existing-test-tag-fails" check_plan_existing_test_tag_fails
    run_check "plan/existing-release-tag-fails" check_plan_existing_release_tag_fails
    run_check "plan/test-tag-only-in-release-package-passes" check_plan_test_tag_only_in_release_package_passes
    run_check "plan/release-tag-only-in-test-package-passes" check_plan_release_tag_only_in_test_package_passes
    run_check "plan/latest-only-for-stable-tag-push" check_plan_latest_only_for_stable_tag_push
    run_check "plan/stdout-holds-only-output-lines" check_plan_stdout_holds_only_output_lines
    run_check "plan/registry-unauthorized-refused" check_plan_registry_unauthorized_refused
    run_check "plan/registry-token-not-found-refused" check_plan_registry_token_not_found_refused
    run_check "plan/registry-rate-limited-refused" check_plan_registry_rate_limited_refused
    run_check "plan/registry-network-error-refused" check_plan_registry_network_error_refused
    run_check "plan/registry-not-found-for-other-reference-refused" check_plan_registry_not_found_for_other_reference_refused
    run_check "plan/registry-empty-error-refused" check_plan_registry_empty_error_refused
    run_check "plan/docker-missing-refused" check_plan_docker_missing_refused
    run_check "workflow/docker-dispatch-inputs" check_workflow_docker_dispatch_inputs
    run_check "workflow/docker-build-tags-from-plan" check_workflow_docker_build_tags_from_plan
    run_check "workflow/docker-build-plan-gets-dispatch-inputs" check_workflow_docker_build_plan_gets_dispatch_inputs
    run_check "workflow/docker-build-version-from-plan" check_workflow_docker_build_version_from_plan
    run_check "workflow/docker-build-names-no-latest" check_workflow_docker_build_names_no_latest
    run_check "workflow/docker-build-source-date-epoch-from-commit" check_workflow_docker_build_source_date_epoch_from_commit
    run_check "workflow/docker-build-rewrites-timestamps" check_workflow_docker_build_rewrites_timestamps
    run_check "workflow/docker-build-provenance" check_workflow_docker_build_provenance
    run_check "workflow/docker-build-sbom" check_workflow_docker_build_sbom
    run_check "workflow/docker-build-reads-attestations" check_workflow_docker_build_reads_attestations
    run_check "workflow/docker-bootloader-keeps-own-plan" check_workflow_docker_bootloader_keeps_own_plan
    run_check "workflow/tests-shellcheck-publish-plan" check_workflow_tests_shellcheck_publish_plan
    rm -rf "$PLAN_WORK"
}
