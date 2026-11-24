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
REVIEW_INPUTS="
form-feed-appended plain-text-only
vertical-tab plain-text-only
no-break-space plain-text-only
form-feed-in-from plain-text-only
form-feed-comment-continuation plain-text-only
form-feed-escape-directive plain-text-only
blank-line-in-continuation no-blank-in-continuation
split-heredoc-marker no-heredoc
mount-from-upper-key copy-from-stages-only
mount-from-quoted-pair copy-from-stages-only
"
HIDDEN_IMAGE_INPUTS="
copy-from-flag-backslash plain-flags
copy-from-flag-double-quoted plain-flags
copy-from-flag-single-quoted plain-flags
mount-flag-name-quoted plain-flags
mount-from-key-single-quoted plain-flags
mount-from-key-backslash plain-flags
mount-value-quoted-space plain-flags
chown-quote-hides-from plain-flags
from-platform-quoted-space plain-flags
onbuild-copy-from-image no-onbuild
onbuild-mount-from-image no-onbuild
repository-escaped-from plain-flags
"
TARGETS_CALL="buildx build --call=targets,format=json -"
CHECK_CALL="buildx build --check --progress=plain -"
ESCAPED_FROM_LINE='COPY --fr\om=debian:trixie /etc/os-release /etc/os-release'
LABEL_LINE="LABEL org.opencontainers.image.base.name=\"$BASE_REF\" org.opencontainers.image.base.digest=\"$BASE_DIGEST\""

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
    FAKE_DOCKER_TARGETS="$TESTS_DIR/fixtures/targets/valid.json"
    FAKE_DOCKER_STDIN="$CASE_DIR/targets-input"
    FAKE_DOCKER_CHECK="$TESTS_DIR/fixtures/check/valid"
    FAKE_DOCKER_CHECK_STDIN="$CASE_DIR/check-input"
    export FAKE_GH_LOG FAKE_DOCKER_LOG FAKE_DOCKER_DIGEST FAKE_DOCKER_TARGETS FAKE_DOCKER_STDIN FAKE_DOCKER_CHECK FAKE_DOCKER_CHECK_STDIN
    unset FAKE_GH_EXIT FAKE_GH_SUBJECTS FAKE_DOCKER_EXIT FAKE_DOCKER_TARGETS_EXIT FAKE_DOCKER_REAL
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

expect_rule_error() {
    printf '%s\n' "$BASE_OUT" | grep -q "^error: $1: " || { echo "no error: $1: line; $(base_result)"; return 1; }
}

# Runs the offline check on a fixture and expects a rejection naming the rule: check_base_offline_rule <fixture> <rule>.
check_base_offline_rule() {
    local rc=0
    base_setup "rule-$1"
    base_run "$REPO_ROOT" --no-attestation "$TESTS_DIR/fixtures/$1"
    expect_rejected || rc=1
    expect_rule_error "$2" || rc=1
    return "$rc"
}

# Writes the repository Dockerfile with a sed edit into the case directory and fails when the edit changed nothing.
edited_dockerfile() {
    sed "$1" "$REPO_ROOT/Dockerfile" >"$CASE_DIR/Dockerfile"
    ! cmp -s "$REPO_ROOT/Dockerfile" "$CASE_DIR/Dockerfile" || { echo "the edit '$1' left the Dockerfile unchanged"; return 1; }
}

# Expects the check, offline or not, to reject the repository Dockerfile edited by sed with a base-labels error.
base_labels_rejected() {
    local mode="$1" edit="$2" rc=0
    base_setup "labels-$3"
    edited_dockerfile "$edit" || return 1
    if [ "$mode" = offline ]; then
        base_run "$REPO_ROOT" --no-attestation "$CASE_DIR/Dockerfile"
    else
        base_run "$REPO_ROOT" "$CASE_DIR/Dockerfile"
    fi
    expect_rejected || rc=1
    expect_rule_error base-labels || rc=1
    return "$rc"
}

check_base_offline_changed_base_digest_label_rejected() {
    base_labels_rejected offline "s|base.digest=\"$BASE_DIGEST\"|base.digest=\"$OTHER_DIGEST\"|" digest
}

check_base_offline_missing_base_labels_rejected() {
    base_labels_rejected offline '/^LABEL /,/[^\\]$/d' missing
}

check_base_changed_base_name_label_rejected() {
    base_labels_rejected attested "s|base.name=\"$BASE_REF\"|base.name=\"$BASE_NAME:trixie-2\"|" name
}

check_base_invalid_tag_in_pin_rejected() {
    local rc=0
    base_setup invalid-tag
    edited_dockerfile "s|$BASE_REF@|$BASE_NAME:.trixie-1@|" || return 1
    base_run "$REPO_ROOT" --no-attestation "$CASE_DIR/Dockerfile"
    expect_rejected || rc=1
    expect_rule_error package || rc=1
    return "$rc"
}

check_base_runs_from_scripts_alone() {
    base_setup scripts-alone
    cp -R "$REPO_ROOT/scripts" "$CASE_DIR/scripts"
    BASE_RC=0
    BASE_OUT="$(cd "$CASE_DIR" && PATH="$CASE_DIR/bin:$PATH" "$BASH" "$CASE_DIR/scripts/verify-base-image.sh" \
        --no-attestation "$REPO_ROOT/Dockerfile" 2>&1 </dev/null)" || BASE_RC=$?
    expect_pass
}

check_base_scripts_do_not_reference_tests() {
    local hits
    hits="$(cd "$REPO_ROOT" && grep -rn 'tests/' scripts/verify-base-image.sh scripts/publish-plan.sh scripts/lib 2>&1)"
    [ -z "$hits" ] || { echo "$hits"; return 1; }
}

# Prints the file of each line under scripts/ that matches, on one line: scripts_matching <-E or -F> <pattern>.
scripts_matching() {
    (cd "$REPO_ROOT" && grep -rc "$1" -- "$2" scripts) |
        awk -F : '{ for (i = 0; i < $NF; i++) printf "%s ", $1 }'
}

check_base_one_line_and_tag_pattern_defined_once() {
    local where rc=0
    where="$(scripts_matching -E '^[[:space:]]*(function[[:space:]]+)?one_line[[:space:]]*\(\)')"
    [ "$where" = "scripts/lib/common.sh " ] || { echo "one_line is defined in: ${where:-nothing}"; rc=1; }
    where="$(scripts_matching -F '[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}')"
    [ "$where" = "scripts/lib/common.sh " ] || { echo "the tag pattern is written in: ${where:-nothing}"; rc=1; }
    return "$rc"
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

# Writes one review input that BuildKit reads differently from the script's parser: review_input <case> <path>.
review_input() {
    local p="$BASE_PIN" l="$LABEL_LINE"
    case "$1" in
        form-feed-appended) { cat "$REPO_ROOT/Dockerfile"; printf '\fFROM debian:trixie\nCMD ["bash", "./start_openplc.sh"]\n'; } ;;
        vertical-tab) printf 'FROM %s AS venvs\nFROM %s AS builder\nFROM %s\n%s\n\vFROM debian:trixie\n' "$p" "$p" "$p" "$l" ;;
        no-break-space) printf 'FROM %s AS venvs\nFROM %s AS builder\nFROM %s\n%s\n\302\240FROM debian:trixie\n' "$p" "$p" "$p" "$l" ;;
        form-feed-in-from) printf 'FROM %s AS venvs\nFROM\fdebian:trixie AS sneaky\nFROM %s AS builder\nFROM %s\n%s\n' "$p" "$p" "$p" "$l" ;;
        form-feed-comment-continuation)
            printf 'FROM %s AS venvs\n\f# x \\\nFROM debian:trixie AS sneaky\nFROM %s AS builder\nFROM %s\n\f# y \\\nCOPY --from=sneaky /etc/os-release /tmp/os-release\n%s\n' \
                "$p" "$p" "$p" "$l"
            ;;
        form-feed-escape-directive)
            printf '\f# escape=`\nFROM %s AS builder\n\nRUN true \\\nFROM debian:trixie AS sneaky\n\nFROM %s\n\nRUN true \\\nCOPY --from=sneaky /etc/os-release /etc/os-release\n%s\n' \
                "$p" "$p" "$l"
            ;;
        blank-line-in-continuation) cat "$TESTS_DIR/fixtures/blank-line-in-continuation.Dockerfile" ;;
        split-heredoc-marker) cat "$TESTS_DIR/fixtures/heredoc-split-marker.Dockerfile" ;;
        mount-from-upper-key) cat "$TESTS_DIR/fixtures/mount-from-image-upper-key.Dockerfile" ;;
        mount-from-quoted-pair) cat "$TESTS_DIR/fixtures/mount-from-image-quoted-pair.Dockerfile" ;;
        *) echo "no review input $1" >&2; return 1 ;;
    esac >"$2"
}

# Runs the offline check on a review input and expects a rejection naming the rule: check_base_review_input <case> <rule>.
check_base_review_input() {
    local rc=0
    base_setup "review-$1"
    review_input "$1" "$CASE_DIR/Dockerfile" || return 1
    base_run "$REPO_ROOT" --no-attestation "$CASE_DIR/Dockerfile"
    expect_rejected || rc=1
    expect_rule_error "$2" || rc=1
    return "$rc"
}

check_base_form_feed_rejected_before_network() {
    local rc=0
    base_setup review-form-feed-attested
    review_input form-feed-appended "$CASE_DIR/Dockerfile" || return 1
    base_run "$REPO_ROOT" "$CASE_DIR/Dockerfile"
    expect_rejected || rc=1
    expect_rule_error plain-text-only || rc=1
    expect_not_called gh "$FAKE_GH_LOG" || rc=1
    expect_not_called docker "$FAKE_DOCKER_LOG" || rc=1
    return "$rc"
}

check_base_plain_text_failure_reports_only_that_rule() {
    base_setup plain-text-first
    printf '# syntax=docker/dockerfile:1\nFROM debian:trixie\n\fFROM debian:trixie AS sneaky\n' >"$CASE_DIR/Dockerfile"
    base_run "$REPO_ROOT" --no-attestation "$CASE_DIR/Dockerfile"
    if [ "$BASE_RC" = 0 ] || [ "$(base_error_lines)" != 1 ] || ! expect_rule_error plain-text-only >/dev/null; then
        echo "expected a non-zero exit with only an error: plain-text-only: line, got $(base_result)"
        return 1
    fi
}

check_base_usage_names_buildkit_check() {
    local out
    out="$("$BASH" "$BASE_SCRIPT" --help 2>&1)"
    printf '%s\n' "$out" | grep -q -- '--check' ||
        { echo "the usage does not name the BuildKit --check run: $(clip "$(printf '%s' "$out" | tr '\n' ' ')")"; return 1; }
}

check_base_usage_says_no_attestation_skips_buildkit() {
    local out
    out="$("$BASH" "$BASE_SCRIPT" --help 2>&1)"
    printf '%s\n' "$out" | grep -qi buildkit ||
        { echo "the usage does not mention the BuildKit cross-check: $(clip "$(printf '%s' "$out" | tr '\n' ' ')")"; return 1; }
}

expect_targets_asked() {
    grep -Fxq -- "$TARGETS_CALL" "$FAKE_DOCKER_LOG" ||
        { echo "docker was not asked '$TARGETS_CALL'; docker calls: $(tr '\n' ';' <"$FAKE_DOCKER_LOG")"; return 1; }
}

# Runs the full check on the repository Dockerfile with BuildKit answering a targets file: buildkit_run <case> <answer> [docker exit].
buildkit_run() {
    base_setup "buildkit-$1"
    cp "$REPO_ROOT/Dockerfile" "$CASE_DIR/Dockerfile"
    export FAKE_DOCKER_TARGETS="$2"
    [ -z "${3:-}" ] || export FAKE_DOCKER_TARGETS_EXIT="$3"
    base_run "$REPO_ROOT" "$CASE_DIR/Dockerfile"
}

# Expects the buildkit-stages rule to reject the repository Dockerfile for a BuildKit answer: buildkit_rejected <case> <answer> [docker exit].
buildkit_rejected() {
    local rc=0
    buildkit_run "$@"
    expect_rejected || rc=1
    expect_rule_error buildkit-stages || rc=1
    expect_targets_asked || rc=1
    return "$rc"
}

check_base_buildkit_asked_about_checked_file() {
    local rc=0
    base_setup buildkit-asked
    { cat "$REPO_ROOT/Dockerfile"; printf '# checked copy\n'; } >"$CASE_DIR/Dockerfile"
    base_run "$REPO_ROOT" "$CASE_DIR/Dockerfile"
    expect_pass || rc=1
    expect_targets_asked || rc=1
    cmp -s "$CASE_DIR/Dockerfile" "$FAKE_DOCKER_STDIN" || { echo "BuildKit did not get the checked Dockerfile on stdin"; rc=1; }
    return "$rc"
}

check_base_buildkit_extra_stage_rejected() {
    buildkit_rejected extra-stage "$TESTS_DIR/fixtures/targets/extra-stage.json"
}

check_base_buildkit_missing_stage_rejected() {
    buildkit_rejected missing-stage "$TESTS_DIR/fixtures/targets/missing-stage.json"
}

check_base_buildkit_renamed_stage_rejected() {
    buildkit_rejected renamed-stage "$TESTS_DIR/fixtures/targets/renamed-stage.json"
}

check_base_buildkit_named_final_stage_rejected() {
    buildkit_rejected named-final "$TESTS_DIR/fixtures/targets/named-final.json"
}

check_base_buildkit_reordered_stages_rejected() {
    buildkit_rejected reordered "$TESTS_DIR/fixtures/targets/reordered.json"
}

check_base_buildkit_other_base_rejected() {
    buildkit_rejected other-base "$TESTS_DIR/fixtures/targets/other-base.json"
}

check_base_buildkit_failure_rejected() {
    buildkit_rejected docker-fails "$TESTS_DIR/fixtures/targets/valid.json" 1
}

check_base_buildkit_text_answer_rejected() {
    local answer="$BASE_WORK/targets-text.txt"
    printf 'TARGET    DESCRIPTION\nvenvs     \nbuilder   \n(default)\n' >"$answer"
    buildkit_rejected text-answer "$answer"
}

check_base_buildkit_empty_answer_rejected() {
    local answer="$BASE_WORK/targets-empty.json"
    : >"$answer"
    buildkit_rejected empty-answer "$answer"
}

check_base_buildkit_targets_without_base_compared_by_name() {
    local rc=0
    buildkit_run no-base "$TESTS_DIR/fixtures/targets/no-base.json"
    expect_pass || rc=1
    expect_targets_asked || rc=1
    return "$rc"
}

check_base_buildkit_stage_on_earlier_stage_passes() {
    local rc=0
    base_setup buildkit-stage-on-stage
    edited_dockerfile 's|^FROM [^ ]* AS builder$|FROM venvs AS builder|' || return 1
    export FAKE_DOCKER_TARGETS="$TESTS_DIR/fixtures/targets/stage-on-stage.json"
    base_run "$REPO_ROOT" "$CASE_DIR/Dockerfile"
    expect_pass || rc=1
    expect_targets_asked || rc=1
    return "$rc"
}

check_base_buildkit_two_documents_rejected() {
    local answer="$BASE_WORK/targets-two-documents.json"
    cat "$TESTS_DIR/fixtures/targets/valid.json" "$TESTS_DIR/fixtures/targets/valid.json" >"$answer"
    buildkit_rejected two-documents "$answer"
}

# Writes the repository Dockerfile with one line added after the last build output copy into the case directory.
repository_with_line() {
    LINE="$1" awk '{ print } /^COPY --from=builder \/workdir\/\.installed / { print ENVIRON["LINE"] }' \
        "$REPO_ROOT/Dockerfile" >"$CASE_DIR/Dockerfile"
    ! cmp -s "$REPO_ROOT/Dockerfile" "$CASE_DIR/Dockerfile" || { echo "no line was added to the Dockerfile"; return 1; }
}

# Copies scripts/ into the case directory with the given rules always passing and points the caller's BASE_SCRIPT at the copy.
scripts_with_rules_off() {
    local id
    cp -R "$REPO_ROOT/scripts" "$CASE_DIR/scripts"
    for id in "$@"; do
        printf '\ncheck_%s() {\n    return 0\n}\n' "${id//-/_}" >>"$CASE_DIR/scripts/lib/dockerfile.sh"
    done
    BASE_SCRIPT="$CASE_DIR/scripts/verify-base-image.sh"
}

# Writes one input that hides an image in a flag or an ONBUILD into the case directory: hidden_image_input <case>.
hidden_image_input() {
    case "$1" in
        repository-escaped-from) repository_with_line "$ESCAPED_FROM_LINE" ;;
        *) cp "$TESTS_DIR/fixtures/$1.Dockerfile" "$CASE_DIR/Dockerfile" ;;
    esac
}

# Expects the check to reject a hidden-image input with the named rule: check_base_hidden_image_rejected <offline|full> <case> <rule>.
check_base_hidden_image_rejected() {
    local rc=0
    base_setup "hidden-$1-$2"
    hidden_image_input "$2" || return 1
    if [ "$1" = offline ]; then
        base_run "$REPO_ROOT" --no-attestation "$CASE_DIR/Dockerfile"
    else
        case "$2" in
            onbuild-*) export FAKE_DOCKER_TARGETS="$TESTS_DIR/fixtures/targets/stage-on-stage.json" ;;
            from-platform-*) export FAKE_DOCKER_TARGETS="$TESTS_DIR/fixtures/targets/platform-hides-base.json" ;;
        esac
        export FAKE_DOCKER_CHECK="$TESTS_DIR/fixtures/check/second-image"
        base_run "$REPO_ROOT" "$CASE_DIR/Dockerfile"
    fi
    expect_rejected || rc=1
    expect_rule_error "$3" || rc=1
    return "$rc"
}

expect_check_asked() {
    grep -Fxq -- "$CHECK_CALL" "$FAKE_DOCKER_LOG" ||
        { echo "docker was not asked '$CHECK_CALL'; docker calls: $(tr '\n' ';' <"$FAKE_DOCKER_LOG")"; return 1; }
}

# Runs the full check on the repository Dockerfile with BuildKit giving a recorded check answer: images_run <case> <answer>.
images_run() {
    base_setup "images-$1"
    cp "$REPO_ROOT/Dockerfile" "$CASE_DIR/Dockerfile"
    export FAKE_DOCKER_CHECK="$TESTS_DIR/fixtures/check/$2"
    base_run "$REPO_ROOT" "$CASE_DIR/Dockerfile"
}

# Expects the buildkit-images rule to reject the repository Dockerfile for a recorded check answer: images_rejected <case> <answer>.
images_rejected() {
    local rc=0
    images_run "$@"
    expect_rejected || rc=1
    expect_rule_error buildkit-images || rc=1
    expect_check_asked || rc=1
    return "$rc"
}

check_base_images_asked_about_checked_file() {
    local rc=0
    base_setup images-asked
    { cat "$REPO_ROOT/Dockerfile"; printf '# checked copy\n'; } >"$CASE_DIR/Dockerfile"
    base_run "$REPO_ROOT" "$CASE_DIR/Dockerfile"
    expect_pass || rc=1
    expect_check_asked || rc=1
    cmp -s "$CASE_DIR/Dockerfile" "$FAKE_DOCKER_CHECK_STDIN" || { echo "BuildKit --check did not get the checked Dockerfile on stdin"; rc=1; }
    return "$rc"
}

check_base_images_after_buildkit_stages() {
    local targets check
    base_setup images-order
    base_run "$REPO_ROOT" "$REPO_ROOT/Dockerfile"
    targets="$(grep -nFx -- "$TARGETS_CALL" "$FAKE_DOCKER_LOG" | head -1 | cut -d: -f1)"
    check="$(grep -nFx -- "$CHECK_CALL" "$FAKE_DOCKER_LOG" | head -1 | cut -d: -f1)"
    [ -n "$targets" ] && [ -n "$check" ] && [ "$targets" -lt "$check" ] ||
        { echo "expected '$TARGETS_CALL' before '$CHECK_CALL'; docker calls: $(tr '\n' ';' <"$FAKE_DOCKER_LOG")"; return 1; }
}

check_base_images_uncached_answer_passes() {
    local rc=0
    images_run uncached uncached
    expect_pass || rc=1
    expect_check_asked || rc=1
    return "$rc"
}

check_base_images_second_image_rejected() {
    images_rejected second-image second-image
}

check_base_images_second_image_rejected_without_offline_rules() {
    local rc=0 BASE_SCRIPT
    base_setup images-rules-off
    scripts_with_rules_off plain-flags no-onbuild
    repository_with_line "$ESCAPED_FROM_LINE" || return 1
    export FAKE_DOCKER_CHECK="$TESTS_DIR/fixtures/check/second-image"
    base_run "$CASE_DIR" "$CASE_DIR/Dockerfile"
    expect_rejected || rc=1
    expect_rule_error buildkit-images || rc=1
    expect_check_asked || rc=1
    return "$rc"
}

check_base_images_tag_without_digest_rejected() {
    images_rejected tag-only pin-and-tag-only
}

check_base_images_no_image_rejected() {
    images_rejected no-image no-image
}

check_base_images_docker_failure_rejected() {
    images_rejected docker-fails lint-warnings
}

check_base_images_unreadable_answer_rejected() {
    images_rejected unreadable unreadable
}

# Makes a case directory whose PATH holds the fake gh and a docker that logs each call and runs the real docker.
real_docker_setup() {
    base_setup "$1"
    FAKE_DOCKER_REAL="$(command -v docker)" || { echo "docker not found"; return 1; }
    export FAKE_DOCKER_REAL
}

check_base_buildkit_real_repository_passes_twice() {
    local run calls rc=0
    real_docker_setup real-repository || return 1
    for run in 1 2; do
        base_run "$REPO_ROOT" "$REPO_ROOT/Dockerfile"
        expect_pass || { echo "on run $run"; rc=1; }
    done
    calls="$(grep -cFx -- "$CHECK_CALL" "$FAKE_DOCKER_LOG")"
    [ "$calls" = 2 ] || { echo "docker was asked '$CHECK_CALL' $calls times in 2 runs"; rc=1; }
    return "$rc"
}

check_base_buildkit_real_escaped_flag_rejected() {
    local rc=0 BASE_SCRIPT
    real_docker_setup real-escaped-flag || return 1
    scripts_with_rules_off plain-flags no-onbuild
    repository_with_line "$ESCAPED_FROM_LINE" || return 1
    base_run "$CASE_DIR" "$CASE_DIR/Dockerfile"
    expect_rejected || rc=1
    expect_rule_error buildkit-images || rc=1
    return "$rc"
}

check_base_buildkit_real_form_feed_rejected() {
    local rc=0 BASE_SCRIPT
    real_docker_setup real-form-feed || return 1
    scripts_with_rules_off plain-text-only
    review_input form-feed-appended "$CASE_DIR/Dockerfile" || return 1
    base_run "$CASE_DIR" "$CASE_DIR/Dockerfile"
    expect_rejected || rc=1
    expect_rule_error buildkit-stages || rc=1
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
    local fixture review rule hidden
    BASE_WORK="$(mktemp -d)"
    BASE_WORK="$(cd "$BASE_WORK" && pwd -P)"
    run_check "base/offline-repository-dockerfile-passes" check_base_offline_repository_passes
    run_check "base/offline-calls-neither-gh-nor-docker" check_base_offline_calls_no_network_tool
    run_check "base/default-file-repository-passes" check_base_default_file_repository_passes
    run_check "base/default-file-is-read-from-working-directory" check_base_default_file_is_read_from_working_directory
    for fixture in unpinned-from.Dockerfile two-pins.Dockerfile syntax-line.Dockerfile copy-from-image.Dockerfile; do
        run_check "base/offline-rejects-$fixture" check_base_offline_fixture "$fixture"
    done
    run_check "base/offline-rejects-escape-directive-bypass" check_base_offline_rule escape-directive.Dockerfile no-parser-directive
    run_check "base/offline-rejects-heredoc-bypass" check_base_offline_rule heredoc-hidden-stage.Dockerfile no-heredoc
    while read -r review rule; do
        [ -n "$review" ] || continue
        run_check "base/offline-rejects-review-input-$review" check_base_review_input "$review" "$rule"
    done <<<"$REVIEW_INPUTS"
    run_check "base/form-feed-rejected-before-gh-and-docker" check_base_form_feed_rejected_before_network
    run_check "base/plain-text-failure-reports-only-that-rule" check_base_plain_text_failure_reports_only_that_rule
    run_check "base/offline-rejects-changed-base-digest-label" check_base_offline_changed_base_digest_label_rejected
    run_check "base/offline-rejects-missing-base-labels" check_base_offline_missing_base_labels_rejected
    run_check "base/rejects-changed-base-name-label" check_base_changed_base_name_label_rejected
    run_check "base/invalid-tag-in-pin-rejected" check_base_invalid_tag_in_pin_rejected
    run_check "base/runs-from-scripts-alone" check_base_runs_from_scripts_alone
    run_check "base/scripts-do-not-reference-tests" check_base_scripts_do_not_reference_tests
    run_check "base/one-line-and-tag-pattern-defined-once" check_base_one_line_and_tag_pattern_defined_once
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
    run_check "base/usage-says-no-attestation-skips-buildkit" check_base_usage_says_no_attestation_skips_buildkit
    run_check "base/usage-names-buildkit-check" check_base_usage_names_buildkit_check
    run_check "base/buildkit-asked-about-checked-file" check_base_buildkit_asked_about_checked_file
    run_check "base/buildkit-extra-stage-rejected" check_base_buildkit_extra_stage_rejected
    run_check "base/buildkit-missing-stage-rejected" check_base_buildkit_missing_stage_rejected
    run_check "base/buildkit-renamed-stage-rejected" check_base_buildkit_renamed_stage_rejected
    run_check "base/buildkit-named-final-stage-rejected" check_base_buildkit_named_final_stage_rejected
    run_check "base/buildkit-reordered-stages-rejected" check_base_buildkit_reordered_stages_rejected
    run_check "base/buildkit-other-base-rejected" check_base_buildkit_other_base_rejected
    run_check "base/buildkit-docker-failure-rejected" check_base_buildkit_failure_rejected
    run_check "base/buildkit-text-answer-rejected" check_base_buildkit_text_answer_rejected
    run_check "base/buildkit-empty-answer-rejected" check_base_buildkit_empty_answer_rejected
    run_check "base/buildkit-targets-without-base-compared-by-name" check_base_buildkit_targets_without_base_compared_by_name
    run_check "base/buildkit-stage-on-earlier-stage-passes" check_base_buildkit_stage_on_earlier_stage_passes
    run_check "base/buildkit-two-documents-rejected" check_base_buildkit_two_documents_rejected
    while read -r hidden rule; do
        [ -n "$hidden" ] || continue
        run_check "base/offline-rejects-hidden-image-$hidden" check_base_hidden_image_rejected offline "$hidden" "$rule"
        run_check "base/full-rejects-hidden-image-$hidden" check_base_hidden_image_rejected full "$hidden" "$rule"
    done <<<"$HIDDEN_IMAGE_INPUTS"
    run_check "base/buildkit-images-asked-about-checked-file" check_base_images_asked_about_checked_file
    run_check "base/buildkit-images-after-buildkit-stages" check_base_images_after_buildkit_stages
    run_check "base/buildkit-images-uncached-answer-passes" check_base_images_uncached_answer_passes
    run_check "base/buildkit-images-second-image-rejected" check_base_images_second_image_rejected
    run_check "base/buildkit-images-second-image-rejected-without-offline-rules" check_base_images_second_image_rejected_without_offline_rules
    run_check "base/buildkit-images-tag-without-digest-rejected" check_base_images_tag_without_digest_rejected
    run_check "base/buildkit-images-no-image-rejected" check_base_images_no_image_rejected
    run_check "base/buildkit-images-docker-failure-rejected" check_base_images_docker_failure_rejected
    run_check "base/buildkit-images-unreadable-answer-rejected" check_base_images_unreadable_answer_rejected
    run_check "workflow/docker-build-checks-base-after-checkout" check_workflow_docker_build_checks_base_after_checkout
    run_check "workflow/docker-build-checks-base-before-qemu-login-build" check_workflow_docker_build_checks_base_before_qemu_login_build
    run_check "workflow/docker-build-checks-attestation" check_workflow_docker_build_checks_attestation
    run_check "workflow/base-check-triggers" check_workflow_base_check_triggers
    run_check "workflow/base-check-job" check_workflow_base_check_job
    run_check "workflow/base-check-read-only" check_workflow_base_check_read_only
    run_check "workflow/base-check-no-login" check_workflow_base_check_no_login
    rm -rf "$BASE_WORK"
}

# Runs the base check against the real docker and a fake gh: the repository Dockerfile, the form-feed stage and the escaped flag.
run_base_buildkit_checks() {
    BASE_WORK="$(mktemp -d)"
    BASE_WORK="$(cd "$BASE_WORK" && pwd -P)"
    run_check "base/buildkit-real-repository-dockerfile-passes-twice" check_base_buildkit_real_repository_passes_twice
    run_check "base/buildkit-real-rejects-form-feed-stage-without-plain-text-rule" check_base_buildkit_real_form_feed_rejected
    run_check "base/buildkit-real-rejects-escaped-flag-without-offline-rules" check_base_buildkit_real_escaped_flag_rejected
    rm -rf "$BASE_WORK"
}
