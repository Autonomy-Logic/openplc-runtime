# shellcheck shell=bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

BASE_SCRIPT="$REPO_ROOT/scripts/verify-base-image.sh"
BASE_REF="${BASE_PIN%@*}"
BASE_DIGEST="${BASE_PIN#*@}"
BASE_NAME="${BASE_REF%:*}"
OTHER_DIGEST="sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
ATTEST_REPO="Autonomy-Logic/autonomy-docker"
ATTEST_SIGNER="Autonomy-Logic/autonomy-docker/.github/workflows/build.yml"
ATTEST_SOURCE_REF="refs/heads/main"
BASE_WORKFLOW="$REPO_ROOT/.github/workflows/base-image-check.yml"
DOCKER_WORKFLOW="$REPO_ROOT/.github/workflows/docker.yml"
BASE_WORK=""

# Makes a case directory with the fake gh and docker and resets their answers: base_setup <case>.
base_setup() {
    CASE_DIR="$BASE_WORK/$1"
    mkdir -p "$CASE_DIR/bin"
    cp "$TESTS_DIR/fake-gh.sh" "$CASE_DIR/bin/gh"
    cp "$TESTS_DIR/fake-docker.sh" "$CASE_DIR/bin/docker"
    chmod +x "$CASE_DIR/bin/gh" "$CASE_DIR/bin/docker"
    FAKE_GH_LOG="$CASE_DIR/gh.log"
    FAKE_DOCKER_LOG="$CASE_DIR/docker.log"
    : >"$FAKE_GH_LOG"
    : >"$FAKE_DOCKER_LOG"
    FAKE_DOCKER_DIGEST="$BASE_DIGEST"
    export FAKE_GH_LOG FAKE_DOCKER_LOG FAKE_DOCKER_DIGEST
    unset FAKE_GH_EXIT FAKE_GH_SUBJECTS FAKE_DOCKER_EXIT
}

# Runs the script from a directory with the fakes first on PATH, setting BASE_RC and BASE_OUT.
base_run() {
    local cwd="$1"
    shift
    BASE_RC=0
    BASE_OUT="$(cd "$cwd" && PATH="$CASE_DIR/bin:$PATH" "$BASH" "$BASE_SCRIPT" "$@" 2>&1 </dev/null)" || BASE_RC=$?
}

# Prints the last run's exit code and output on one line.
base_result() {
    local out="${BASE_OUT//"$REPO_ROOT"\//}"
    printf 'exit %s: %s\n' "$BASE_RC" "$(clip "$(printf '%s' "$out" | tr '\n' ' ')")"
}

# Prints the number of output lines of the last run that carry an error: mark.
base_error_lines() {
    printf '%s\n' "$BASE_OUT" | grep -c 'error:'
}

expect_pass() {
    [ "$BASE_RC" = 0 ] && [ "$(base_error_lines)" = 0 ] || { echo "expected exit 0 and no error: line, got $(base_result)"; return 1; }
}

expect_rejected() {
    [ "$BASE_RC" != 0 ] && [ "$(base_error_lines)" -ge 1 ] ||
        { echo "expected a non-zero exit with an error: line, got $(base_result)"; return 1; }
}

expect_not_called() {
    [ ! -s "$2" ] || { echo "$1 was called: $(tr '\n' ';' <"$2")"; return 1; }
}

expect_called() {
    [ -s "$2" ] || { echo "$1 was not called; $(base_result)"; return 1; }
}

# Writes the repository Dockerfile with every pin moved to another package into the case directory.
lookalike_package_dockerfile() {
    sed "s|$BASE_NAME:|$BASE_NAME-dev:|g" "$REPO_ROOT/Dockerfile" >"$CASE_DIR/Dockerfile"
}

check_base_offline_repository_passes() {
    base_setup offline-pass
    base_run "$REPO_ROOT" --no-attestation "$REPO_ROOT/Dockerfile"
    expect_pass
}

check_base_offline_calls_no_network_tool() {
    local rc=0
    base_setup offline-no-network
    base_run "$REPO_ROOT" --no-attestation "$REPO_ROOT/Dockerfile"
    [ "$BASE_RC" != 127 ] || { echo "script missing: $(base_result)"; return 1; }
    expect_not_called gh "$FAKE_GH_LOG" || rc=1
    expect_not_called docker "$FAKE_DOCKER_LOG" || rc=1
    return "$rc"
}

check_base_default_file_repository_passes() {
    base_setup default-pass
    base_run "$REPO_ROOT" --no-attestation
    expect_pass
}

check_base_default_file_is_read_from_working_directory() {
    base_setup default-cwd
    cp "$TESTS_DIR/fixtures/unpinned-from.Dockerfile" "$CASE_DIR/Dockerfile"
    base_run "$CASE_DIR" --no-attestation
    expect_rejected
}

# Runs the offline check on one phase-01 fixture and expects a rejection: check_base_offline_fixture <fixture>.
check_base_offline_fixture() {
    base_setup "fixture-$1"
    base_run "$REPO_ROOT" --no-attestation "$TESTS_DIR/fixtures/$1"
    expect_rejected
}

check_base_error_line_per_broken_rule() {
    local count
    base_setup two-rules
    {
        printf '# syntax=docker/dockerfile:1\n'
        /bin/cat "$TESTS_DIR/fixtures/copy-from-image.Dockerfile"
    } >"$CASE_DIR/Dockerfile"
    base_run "$REPO_ROOT" --no-attestation "$CASE_DIR/Dockerfile"
    count="$(base_error_lines)"
    [ "$BASE_RC" != 0 ] && [ "$count" -ge 2 ] ||
        { echo "expected a non-zero exit with an error: line for each of two broken rules, got $count; $(base_result)"; return 1; }
}

check_base_other_package_rejected() {
    local rc=0
    base_setup other-package
    lookalike_package_dockerfile
    base_run "$REPO_ROOT" --no-attestation "$CASE_DIR/Dockerfile"
    expect_rejected || rc=1
    expect_not_called gh "$FAKE_GH_LOG" || rc=1
    return "$rc"
}

check_base_attested_pin_passes() {
    base_setup attested
    base_run "$REPO_ROOT" "$REPO_ROOT/Dockerfile"
    expect_pass
}

check_base_gh_verifies_signer_ref_and_hosted_runners() {
    local line needle missing
    base_setup gh-flags
    base_run "$REPO_ROOT" "$REPO_ROOT/Dockerfile"
    while IFS= read -r line; do
        line=" $line "
        missing=""
        [[ "$line" == " attestation verify "* ]] || missing=" [attestation verify]"
        for needle in "oci://$BASE_NAME@$BASE_DIGEST" "--repo $ATTEST_REPO" \
            "--signer-workflow $ATTEST_SIGNER" "--source-ref $ATTEST_SOURCE_REF" "--deny-self-hosted-runners"; do
            [[ "$line" == *" $needle "* ]] || missing="$missing [$needle]"
        done
        [ -n "$missing" ] || return 0
    done <"$FAKE_GH_LOG"
    echo "no gh call carries every required argument${missing:+, last call lacks$missing}; gh calls: $(tr '\n' ';' <"$FAKE_GH_LOG")"
    return 1
}

check_base_unattested_digest_rejected() {
    local rc=0
    base_setup unattested
    export FAKE_GH_EXIT=1
    base_run "$REPO_ROOT" "$REPO_ROOT/Dockerfile"
    expect_rejected || rc=1
    expect_called gh "$FAKE_GH_LOG" || rc=1
    return "$rc"
}

# Runs the full check with gh answering one attestation per given subject line and expects a rejection.
attestation_rejected() {
    local rc=0
    base_setup "$1"
    export FAKE_GH_SUBJECTS="$2"
    base_run "$REPO_ROOT" "$REPO_ROOT/Dockerfile"
    expect_rejected || rc=1
    expect_called gh "$FAKE_GH_LOG" || rc=1
    return "$rc"
}

check_base_attestation_of_another_package_rejected() {
    attestation_rejected other-subject "ghcr.io/autonomy-logic/autonomy-python-base $BASE_DIGEST"
}

check_base_attestation_of_lookalike_package_rejected() {
    attestation_rejected lookalike-subject "$BASE_NAME-dev $BASE_DIGEST"
}

check_base_attestation_of_another_digest_rejected() {
    attestation_rejected other-digest "$BASE_NAME $OTHER_DIGEST"
}

check_base_matching_attestation_among_others_passes() {
    base_setup among-others
    export FAKE_GH_SUBJECTS="ghcr.io/autonomy-logic/autonomy-python-base $BASE_DIGEST
$BASE_NAME $OTHER_DIGEST
$BASE_NAME $BASE_DIGEST"
    base_run "$REPO_ROOT" "$REPO_ROOT/Dockerfile"
    expect_pass
}

check_base_tag_on_another_digest_rejected() {
    local rc=0
    base_setup tag-moved
    export FAKE_DOCKER_DIGEST="$OTHER_DIGEST"
    base_run "$REPO_ROOT" "$REPO_ROOT/Dockerfile"
    expect_rejected || rc=1
    expect_called docker "$FAKE_DOCKER_LOG" || rc=1
    return "$rc"
}

check_base_tag_missing_from_registry_rejected() {
    local rc=0
    base_setup tag-missing
    export FAKE_DOCKER_EXIT=1
    base_run "$REPO_ROOT" "$REPO_ROOT/Dockerfile"
    expect_rejected || rc=1
    expect_called docker "$FAKE_DOCKER_LOG" || rc=1
    return "$rc"
}

# Prints the body of a key of the YAML block on stdin, at that block's top indentation; an inline value is printed as its body.
yaml_child() {
    awk -v key="$1" '
        function indent(s) { match(s, /^ */); return RLENGTH }
        /^[ \t]*#/ || /^[ \t]*$/ { next }
        {
            ind = indent($0)
            if (top < 0) top = ind
            if (inside) { if (ind <= top) exit; print; next }
            if (ind != top) next
            line = substr($0, ind + 1)
            k = line
            sub(/:.*/, "", k)
            gsub(/["\047]/, "", k)
            if (k != key || line !~ /:/) next
            inside = 1
            rest = line
            sub(/^[^:]*:[ \t]*/, "", rest)
            sub(/[ \t]+#.*/, "", rest)
            if (rest != "") print rest
        }' top=-1
}

# Prints the body of a key path of a YAML file: yaml_path <file> <key>...
yaml_path() {
    local file="$1" block key
    shift
    block="$(/bin/cat "$file")"
    for key in "$@"; do
        block="$(printf '%s\n' "$block" | yaml_child "$key")"
    done
    printf '%s\n' "$block"
}

# Prints each item of the YAML list on stdin as one line.
yaml_items() {
    awk '
        function indent(s) { match(s, /^ */); return RLENGTH }
        /^[ \t]*#/ || /^[ \t]*$/ { next }
        {
            ind = indent($0)
            if (top < 0) top = ind
            line = substr($0, ind + 1)
            if (ind == top && line ~ /^- /) { if (item != "") print item; item = substr(line, 3); next }
            item = item " " line
        }
        END { if (item != "") print item }' top=-1
}

# Prints the 1-based index of the first step of the docker.yml build job matching an extended regex.
build_step_index() {
    yaml_path "$DOCKER_WORKFLOW" jobs build steps | yaml_items | grep -nE -m 1 -- "$1" | cut -d: -f1
}

check_workflow_docker_build_checks_base_after_checkout() {
    local checkout verify
    checkout="$(build_step_index 'actions/checkout@')"
    verify="$(build_step_index 'verify-base-image\.sh')"
    [ -n "$verify" ] || { echo "no step of the build job runs scripts/verify-base-image.sh"; return 1; }
    [ -n "$checkout" ] && [ "$verify" = $((checkout + 1)) ] ||
        { echo "the base check is step $verify, checkout is step ${checkout:-missing}"; return 1; }
}

check_workflow_docker_build_checks_base_before_qemu_login_build() {
    local verify action at rc=0
    verify="$(build_step_index 'verify-base-image\.sh')"
    [ -n "$verify" ] || { echo "no step of the build job runs scripts/verify-base-image.sh"; return 1; }
    for action in docker/setup-qemu-action docker/login-action docker/build-push-action; do
        at="$(build_step_index "$action@")"
        [ -z "$at" ] || [ "$verify" -lt "$at" ] || { echo "$action (step $at) runs before the base check (step $verify)"; rc=1; }
    done
    return "$rc"
}

check_workflow_docker_build_checks_attestation() {
    local step
    step="$(yaml_path "$DOCKER_WORKFLOW" jobs build steps | yaml_items | grep -m 1 -- 'verify-base-image\.sh')"
    [ -n "$step" ] || { echo "no step of the build job runs scripts/verify-base-image.sh"; return 1; }
    [[ "$step" != *--no-attestation* ]] || { echo "the build job skips the attestation: $(clip "$step")"; return 1; }
}

check_workflow_base_check_triggers() {
    local on pr push rc=0
    [ -f "$BASE_WORKFLOW" ] || { echo "missing .github/workflows/base-image-check.yml"; return 1; }
    on="$(yaml_path "$BASE_WORKFLOW" on)"
    printf '%s\n' "$on" | grep -Eq '(^|[^[:alnum:]_])pull_request([^[:alnum:]_]|$)' || { echo "no pull_request trigger"; rc=1; }
    pr="$(printf '%s\n' "$on" | yaml_child pull_request)"
    printf '%s\n' "$pr" | grep -Eq '^[[:space:]]*(branches|branches-ignore|paths|paths-ignore):' &&
        { echo "pull_request trigger is filtered: $(printf '%s' "$pr" | tr '\n' ' ')"; rc=1; }
    push="$(printf '%s\n' "$on" | yaml_child push)"
    printf '%s\n' "$push" | grep -Ewq development || { echo "no push trigger on development"; rc=1; }
    printf '%s\n' "$push" | grep -Ewq main || { echo "no push trigger on main"; rc=1; }
    return "$rc"
}

check_workflow_base_check_job() {
    local job body
    [ -f "$BASE_WORKFLOW" ] || { echo "missing .github/workflows/base-image-check.yml"; return 1; }
    for job in $(yaml_path "$BASE_WORKFLOW" jobs | awk '
            function indent(s) { match(s, /^ */); return RLENGTH }
            /^[ \t]*#/ || /^[ \t]*$/ { next }
            { ind = indent($0); if (top < 0) top = ind; if (ind == top) { k = substr($0, ind + 1); sub(/:.*/, "", k); print k } }' top=-1); do
        body="$(yaml_path "$BASE_WORKFLOW" jobs "$job")"
        printf '%s\n' "$body" | yaml_child name | grep -Eqx "[\"']?Verify Base Image Attestation[\"']?" || continue
        printf '%s\n' "$body" | grep -q 'verify-base-image\.sh' || { echo "job $job does not run scripts/verify-base-image.sh"; return 1; }
        printf '%s\n' "$body" | grep -q -- '--no-attestation' && { echo "job $job skips the attestation"; return 1; }
        printf '%s\n' "$body" | grep -Eq '^[[:space:]]*GH_TOKEN:' || { echo "job $job sets no GH_TOKEN"; return 1; }
        return 0
    done
    echo "no job named Verify Base Image Attestation"
    return 1
}

check_workflow_base_check_read_only() {
    local rc=0
    [ -f "$BASE_WORKFLOW" ] || { echo "missing .github/workflows/base-image-check.yml"; return 1; }
    grep -Eq '^[[:space:]]*packages:[[:space:]]*read' "$BASE_WORKFLOW" || { echo "no packages: read permission"; rc=1; }
    grep -Eq '^[[:space:]]*attestations:[[:space:]]*read' "$BASE_WORKFLOW" || { echo "no attestations: read permission"; rc=1; }
    if grep -Eq '(:[[:space:]]*write([[:space:]]|$)|write-all)' "$BASE_WORKFLOW"; then
        echo "write permission: $(grep -E '(:[[:space:]]*write([[:space:]]|$)|write-all)' "$BASE_WORKFLOW" | tr -s ' ' | tr '\n' ';')"
        rc=1
    fi
    return "$rc"
}

check_workflow_base_check_no_login() {
    [ -f "$BASE_WORKFLOW" ] || { echo "missing .github/workflows/base-image-check.yml"; return 1; }
    ! grep -Eq '(docker/login-action|docker login|GHCR_TOKEN|GHCR_USERNAME)' "$BASE_WORKFLOW" ||
        { echo "logs in: $(grep -E '(docker/login-action|docker login|GHCR_TOKEN|GHCR_USERNAME)' "$BASE_WORKFLOW" | tr -s ' ' | tr '\n' ';')"; return 1; }
}

# Runs the base check script against fake gh and docker and checks the workflows that run it.
run_base_checks() {
    local fixture
    BASE_WORK="$(mktemp -d)"
    BASE_WORK="$(cd "$BASE_WORK" && pwd -P)"
    run_check "base/offline-repository-dockerfile-passes" check_base_offline_repository_passes
    run_check "base/offline-calls-neither-gh-nor-docker" check_base_offline_calls_no_network_tool
    run_check "base/default-file-repository-passes" check_base_default_file_repository_passes
    run_check "base/default-file-is-read-from-working-directory" check_base_default_file_is_read_from_working_directory
    for fixture in unpinned-from.Dockerfile two-pins.Dockerfile syntax-line.Dockerfile copy-from-image.Dockerfile; do
        run_check "base/offline-rejects-$fixture" check_base_offline_fixture "$fixture"
    done
    run_check "base/error-line-per-broken-rule" check_base_error_line_per_broken_rule
    run_check "base/other-package-name-rejected" check_base_other_package_rejected
    run_check "base/attested-pin-passes" check_base_attested_pin_passes
    run_check "base/gh-verifies-signer-ref-and-hosted-runners" check_base_gh_verifies_signer_ref_and_hosted_runners
    run_check "base/unattested-digest-rejected" check_base_unattested_digest_rejected
    run_check "base/attestation-of-another-package-rejected" check_base_attestation_of_another_package_rejected
    run_check "base/attestation-of-lookalike-package-rejected" check_base_attestation_of_lookalike_package_rejected
    run_check "base/attestation-of-another-digest-rejected" check_base_attestation_of_another_digest_rejected
    run_check "base/matching-attestation-among-others-passes" check_base_matching_attestation_among_others_passes
    run_check "base/tag-on-another-digest-rejected" check_base_tag_on_another_digest_rejected
    run_check "base/tag-missing-from-registry-rejected" check_base_tag_missing_from_registry_rejected
    run_check "workflow/docker-build-checks-base-after-checkout" check_workflow_docker_build_checks_base_after_checkout
    run_check "workflow/docker-build-checks-base-before-qemu-login-build" check_workflow_docker_build_checks_base_before_qemu_login_build
    run_check "workflow/docker-build-checks-attestation" check_workflow_docker_build_checks_attestation
    run_check "workflow/base-check-triggers" check_workflow_base_check_triggers
    run_check "workflow/base-check-job" check_workflow_base_check_job
    run_check "workflow/base-check-read-only" check_workflow_base_check_read_only
    run_check "workflow/base-check-no-login" check_workflow_base_check_no_login
    rm -rf "$BASE_WORK"
}
