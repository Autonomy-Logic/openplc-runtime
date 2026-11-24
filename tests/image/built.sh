# shellcheck shell=bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

IMAGE_TEST_BUILDER="${IMAGE_TEST_BUILDER:-openplc-image-test}"
IMAGE_TEST_REPO="openplc-runtime-image-test"
BUILT_CHECKS="
image-builds-for-host
final-stage-on-pinned-base
layer-order
venvs-run-python-3.13
runtime-files-present
base-labels-equal-pin
no-secrets-or-sources
container-becomes-healthy
builder-gcc-14
builder-cmake-from-apt
venv-layer-reproducible
app-change-keeps-base-and-venv-layers
requirements-change-rebuilds-venv-layer
"

built_usage() {
    cat <<EOF
The built-image checks need docker with buildx, jq, gzip, tar and git, and a buildx builder with the
docker-container driver, named by IMAGE_TEST_BUILDER (default: openplc-image-test). Create one with:
  docker buildx create --name ${IMAGE_TEST_BUILDER} --driver docker-container
They build the image for the host architecture twice without cache and twice with cache.
SOURCE_DATE_EPOCH defaults to the time of the last commit. IMAGE_TEST_KEEP=1 keeps the work directory.
EOF
}

# Fails with a reason when a tool or the buildx builder the built-image checks need is missing.
built_prerequisites() {
    local tool
    for tool in docker jq gzip tar git; do
        command -v "$tool" >/dev/null 2>&1 || { echo "$tool not found"; return 1; }
    done
    docker buildx inspect "$IMAGE_TEST_BUILDER" >/dev/null 2>&1 || {
        echo "buildx builder $IMAGE_TEST_BUILDER not found; create it with:" \
            "docker buildx create --name $IMAGE_TEST_BUILDER --driver docker-container"
        return 1
    }
}

# Records every built-image check from the given one on as failed with the same reason.
built_fail_from() {
    local from="$1" reason="$2" id on=0
    for id in $BUILT_CHECKS; do
        [ "$id" = "$from" ] && on=1
        [ "$on" = 1 ] && report "built/$id" 1 "$reason"
    done
}

# Copies the working tree's tracked and untracked, not ignored, files into a new directory.
copy_context() {
    mkdir -p "$1"
    (cd "$REPO_ROOT" && git ls-files -z -co --exclude-standard |
        while IFS= read -r -d '' f; do
            if [ -e "$f" ] || [ -L "$f" ]; then printf '%s\0' "$f"; fi
        done | tar -c -f - --null -T -) | tar -x -f - -C "$1"
}

# Builds a context into an OCI layout directory with rewritten timestamps: build_image <name> <context> [flags].
build_image() {
    local name="$1" ctx="$2"
    shift 2
    SOURCE_DATE_EPOCH="$BUILT_SDE" docker buildx build --builder "$IMAGE_TEST_BUILDER" \
        --provenance=false --sbom=false --progress=plain \
        --output "type=oci,dest=$BUILT_DIR/$name,tar=false,name=$IMAGE_TEST_REPO:$name,rewrite-timestamp=true" \
        "$@" "$ctx" >"$BUILT_DIR/$name.log" 2>&1
}

# Prints the last lines of a build log on one line.
build_log_tail() {
    tail -5 "$BUILT_DIR/$1.log" | tr '\n' ' '
}

oci_blob() {
    cat "$1/blobs/sha256/${2#sha256:}"
}

oci_manifest() {
    oci_blob "$1" "$(jq -r '.manifests[0].digest' "$1/index.json")"
}

oci_config() {
    oci_blob "$1" "$(oci_manifest "$1" | jq -r .config.digest)"
}

oci_layers() {
    oci_manifest "$1" | jq -r '.layers[].digest'
}

# Writes <digest> TAB <role> per layer to <dir>.roles: base, dirs (no files), venvs, build, app, or mixed.
classify_layers() {
    local dir="$1" i=0 digest role
    : >"$dir.roles"
    while IFS= read -r digest; do
        if [ "$i" -lt "$BASE_LAYERS" ]; then
            role=base
        else
            role="$(oci_blob "$dir" "$digest" | gzip -dc | tar -tf - | awk '
                { sub(/^\.\//, "") }
                /\/$/ { next }
                { n++; if ($0 ~ /^workdir\/venvs\//) v++; else if ($0 ~ /^workdir\/build\//) b++ }
                END {
                    if (!n) print "dirs"
                    else if (v == n) print "venvs"
                    else if (b == n) print "build"
                    else if (v || b) print "mixed"
                    else print "app"
                }')"
        fi
        printf '%s\t%s\n' "$digest" "$role" >>"$dir.roles"
        i=$((i + 1))
    done < <(oci_layers "$dir")
}

venv_digest() {
    awk -F '\t' '$2 == "venvs" { print $1; exit }' "$1.roles"
}

base_digests() {
    awk -F '\t' '$2 == "base" { print $1 }' "$1.roles" | tr '\n' ' '
}

check_built_final_on_base() {
    local got
    got="$(oci_config "$BUILT_DIR/a" | jq -c ".rootfs.diff_ids[0:$BASE_LAYERS]")"
    [ "$got" = "$BASE_DIFF_IDS" ] || { echo "the image does not start with the layers of $BASE_PIN"; return 1; }
}

check_built_layer_order() {
    local seq
    seq="$(awk -F '\t' '$2 != "base" && $2 != "dirs" { printf "%s ", $2 }' "$BUILT_DIR/a.roles")"
    [[ "$seq" =~ ^venvs\ build\ (app\ )+$ ]] ||
        { echo "layers above the base are '${seq% }', expected venvs, build, then application layers"; return 1; }
}

check_built_venvs_python() {
    # shellcheck disable=SC2016,SC2046
    docker run --rm --network none "$BUILT_IMAGE" bash -c '
        for v in "$@"; do
            /workdir/venvs/$v/bin/python -c "import sys; assert sys.version_info[:2] == (3, 13), sys.version" ||
                { echo "venv $v does not run Python 3.13"; exit 1; }
        done' bash $(venv_names)
}

check_built_runtime_files() {
    local missing
    # shellcheck disable=SC2016,SC2046
    missing="$(docker run --rm --network none "$BUILT_IMAGE" bash -c '
        cd /workdir || { echo "no /workdir"; exit 0; }
        for f in build/plc_main build/etherdog; do test -x "$f" || echo "missing $f"; done
        for f in .installed "$@"; do test -f "$f" || echo "missing $f"; done' bash \
        $(awk -F , '$4 == 1 { print $2 }' "$REPO_ROOT/plugins_default.conf") 2>&1)"
    [ -z "$missing" ] || { echo "$missing"; return 1; }
}

check_built_base_labels() {
    local got
    got="$(oci_config "$BUILT_DIR/a" | jq -r '.config.Labels // {} |
        (.["org.opencontainers.image.base.name"] // "") + "@" + (.["org.opencontainers.image.base.digest"] // "")')"
    [ "$got" = "$BASE_PIN" ] || { echo "base labels are $got"; return 1; }
}

# Fails naming each .pem, .env, .db, .git or third_party under /workdir, except .pem files in installed packages.
check_built_no_secrets() {
    local found
    found="$(docker run --rm --network none "$BUILT_IMAGE" find /workdir \
        \( -name '*.pem' ! -path '*/site-packages/*' -o -name .env -o -name '*.db' -o -name .git -o -name third_party \) \
        -print 2>&1)"
    [ -z "$found" ] || { echo "$found"; return 1; }
}

# Starts a healthy container of an image: built_start_healthy <image>; prints its id, or the reason and the last logs.
built_start_healthy() {
    local cid status reason deadline
    cid="$(docker run -d --cap-add SYS_NICE --cap-add SYS_RESOURCE "$1" 2>&1)" ||
        { echo "$cid"; return 1; }
    deadline=$((SECONDS + 300))
    while :; do
        status="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid")"
        case "$status" in
            "running healthy") printf '%s\n' "$cid"; return 0 ;;
            "running starting") ;;
            "running ") reason="the image has no HEALTHCHECK"; break ;;
            *) reason="container is $status"; break ;;
        esac
        [ "$SECONDS" -lt "$deadline" ] || { reason="not healthy after 300s"; break; }
        sleep 5
    done
    echo "$reason"
    docker logs --tail 5 "$cid" 2>&1
    docker rm -f "$cid" >/dev/null 2>&1
    return 1
}

check_built_healthy() {
    local cid
    cid="$(built_start_healthy "$BUILT_IMAGE")" || { echo "$cid"; return 1; }
    docker rm -f "$cid" >/dev/null 2>&1
}

check_built_builder_gcc() {
    local version
    version="$(docker run --rm --network none "$IMAGE_TEST_REPO:builder" gcc -dumpversion 2>&1)"
    [ "${version%%.*}" = 14 ] || { echo "builder stage gcc is $version"; return 1; }
}

check_built_builder_cmake() {
    # shellcheck disable=SC2016
    docker run --rm --network none "$IMAGE_TEST_REPO:builder" bash -c '
        p="$(command -v cmake)" || { echo "no cmake in the builder stage"; exit 1; }
        test ! -e /usr/local/bin/cmake || { echo "/usr/local/bin/cmake is present"; exit 1; }
        dpkg -S "$(readlink -f "$p")" >/dev/null 2>&1 || { echo "$p belongs to no Debian package"; exit 1; }'
}

check_built_reproducible() {
    local first second
    first="$(venv_digest "$BUILT_DIR/a")"
    second="$(venv_digest "$BUILT_DIR/b")"
    [ -n "$first" ] && [ -n "$second" ] || { echo "no venvs layer to compare"; return 1; }
    [ "$first" = "$second" ] || { echo "venvs layer $first then $second"; return 1; }
}

check_built_app_change() {
    local rc=0
    [ "$(base_digests "$BUILT_DIR/b")" = "$(base_digests "$BUILT_DIR/app")" ] ||
        { echo "base layers changed"; rc=1; }
    [ -n "$(venv_digest "$BUILT_DIR/b")" ] && [ "$(venv_digest "$BUILT_DIR/b")" = "$(venv_digest "$BUILT_DIR/app")" ] ||
        { echo "venvs layer changed or missing"; rc=1; }
    [ "$(oci_layers "$BUILT_DIR/b")" != "$(oci_layers "$BUILT_DIR/app")" ] ||
        { echo "the application change did not reach the image"; rc=1; }
    return "$rc"
}

check_built_requirements_change() {
    local rc=0 before after
    [ "$(base_digests "$BUILT_DIR/b")" = "$(base_digests "$BUILT_DIR/req")" ] ||
        { echo "base layers changed"; rc=1; }
    before="$(venv_digest "$BUILT_DIR/b")"
    after="$(venv_digest "$BUILT_DIR/req")"
    [ -n "$before" ] && [ -n "$after" ] || { echo "no venvs layer to compare"; return 1; }
    [ "$before" != "$after" ] || { echo "venvs layer unchanged: $after"; rc=1; }
    return "$rc"
}

# Loads the base image facts the checks compare against: its layer count and diff IDs.
load_base_facts() {
    docker image inspect "$BASE_PIN" >/dev/null 2>&1 || docker pull -q "$BASE_PIN" >/dev/null 2>&1 ||
        { echo "cannot pull $BASE_PIN"; return 1; }
    BASE_LAYERS="$(docker image inspect --format '{{len .RootFS.Layers}}' "$BASE_PIN")"
    BASE_DIFF_IDS="$(docker image inspect --format '{{json .RootFS.Layers}}' "$BASE_PIN" | jq -c .)"
}

# Builds one variant and checks it built: built_variant <name> <check id> [flags].
built_variant() {
    local name="$1" id="$2"
    shift 2
    if build_image "$name" "$BUILT_DIR/ctx-$name" "$@"; then
        classify_layers "$BUILT_DIR/$name"
        return 0
    fi
    report "built/$id" 1 "build $name failed: $(build_log_tail "$name")"
    return 1
}

built_cleanup() {
    [ -n "${BUILT_DIR:-}" ] || return 0
    docker image rm -f "$BUILT_IMAGE" "$IMAGE_TEST_REPO:builder" >/dev/null 2>&1
    if [ "${IMAGE_TEST_KEEP:-0}" = 1 ]; then
        printf '# work directory kept: %s\n' "$BUILT_DIR"
    else
        rm -rf "$BUILT_DIR"
    fi
}

# Builds the image and runs every built-image check, one line per check; built_cleanup removes the image.
run_built_checks() {
    local reason name
    reason="$(built_prerequisites)" || { built_usage >&2; built_fail_from image-builds-for-host "$reason"; return; }
    reason="$(load_base_facts 2>&1)" || { built_fail_from image-builds-for-host "$reason"; return; }
    load_base_facts
    BUILT_DIR="$(mktemp -d)"
    BUILT_DIR="$(cd "$BUILT_DIR" && pwd -P)"
    BUILT_SDE="${SOURCE_DATE_EPOCH:-$(git -C "$REPO_ROOT" log -1 --format=%ct)}"
    BUILT_IMAGE="$IMAGE_TEST_REPO:a"
    for name in a b app req; do
        copy_context "$BUILT_DIR/ctx-$name"
    done
    printf '\n# image test change\n' >>"$BUILT_DIR/ctx-app/webserver/app.py"
    printf '\nsix==1.17.0\n' >>"$BUILT_DIR/ctx-req/requirements.txt"

    if ! build_image a "$BUILT_DIR/ctx-a" --no-cache; then
        built_fail_from image-builds-for-host "build failed: $(build_log_tail a)"
        return
    fi
    report built/image-builds-for-host 0
    classify_layers "$BUILT_DIR/a"
    tar -c -f - -C "$BUILT_DIR/a" . | docker load >/dev/null
    run_check built/final-stage-on-pinned-base check_built_final_on_base
    run_check built/layer-order check_built_layer_order
    run_check built/venvs-run-python-3.13 check_built_venvs_python
    run_check built/runtime-files-present check_built_runtime_files
    run_check built/base-labels-equal-pin check_built_base_labels
    run_check built/no-secrets-or-sources check_built_no_secrets
    run_check built/container-becomes-healthy check_built_healthy

    if SOURCE_DATE_EPOCH="$BUILT_SDE" docker buildx build --builder "$IMAGE_TEST_BUILDER" \
        --provenance=false --sbom=false --progress=plain --target builder \
        --output "type=docker,name=$IMAGE_TEST_REPO:builder" "$BUILT_DIR/ctx-a" >"$BUILT_DIR/builder.log" 2>&1; then
        run_check built/builder-gcc-14 check_built_builder_gcc
        run_check built/builder-cmake-from-apt check_built_builder_cmake
    else
        report built/builder-gcc-14 1 "builder stage failed: $(build_log_tail builder)"
        report built/builder-cmake-from-apt 1 "builder stage failed: $(build_log_tail builder)"
    fi

    if built_variant b venv-layer-reproducible --no-cache; then
        run_check built/venv-layer-reproducible check_built_reproducible
        if built_variant app app-change-keeps-base-and-venv-layers; then
            run_check built/app-change-keeps-base-and-venv-layers check_built_app_change
        fi
        if built_variant req requirements-change-rebuilds-venv-layer; then
            run_check built/requirements-change-rebuilds-venv-layer check_built_requirements_change
        fi
    else
        report built/app-change-keeps-base-and-venv-layers 1 "no second build to compare against"
        report built/requirements-change-rebuilds-venv-layer 1 "no second build to compare against"
    fi
}
