# shellcheck shell=bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

SECURITY_PROBE="$TESTS_DIR/security_probe.py"
SECURITY_PYTHON=/workdir/venvs/runtime/bin/python
SECURITY_CHECKS="
webserver-python-3.13
plugin-host-python-3.13
plugin-modbus_slave-starts-on-python-3.13
plugin-modbus_master-starts-on-python-3.13
plugin-opcua-starts-on-python-3.13
tls-1.0-refused
tls-1.1-refused
tls-1.2-accepted
tls-1.3-accepted
upload-without-token-refused
upload-token-of-other-key-refused
upload-expired-token-refused
upload-dotdot-entry-refused
upload-absolute-entry-refused
upload-expanding-archive-refused
opcua-offers-only-configured-profile
"
SECURITY_CID=""
SECURITY_IMAGE=""

# Prints the import name of the main dependency of a Python plugin.
plugin_dependency() {
    case "$1" in
        modbus_slave | modbus_master) echo pymodbus ;;
        opcua) echo asyncua ;;
    esac
}

# Runs the probe in a new container of the image: security_probe <network> <python> <probe> [args].
# Prints the probe's verdict line; its exit code is the probe's.
security_probe() {
    local network="$1" python="$2" out rc=0
    shift 2
    out="$(docker run --rm -i --no-healthcheck --network "$network" -w /workdir \
        -e IMAGE_TEST_JWT_KEY="${SECURITY_JWT_KEY:-}" "$SECURITY_IMAGE" "$python" -I - "$@" <"$SECURITY_PROBE" 2>&1)" || rc=$?
    printf '%s\n' "$out" | tail -n 1
    return "$rc"
}

# Runs the probe beside the running runtime container, on its network.
runtime_probe() {
    security_probe "container:$SECURITY_CID" "$SECURITY_PYTHON" "$@"
}

check_security_ci_webserver_python() {
    local setup
    setup="$(yaml_path "$TESTS_WORKFLOW" jobs webserver steps | yaml_items | grep -E 'actions/setup-python@')"
    [ -n "$setup" ] || { echo "the Webserver (pytest) job has no actions/setup-python step"; return 1; }
    [[ "$setup" =~ python-version:[[:space:]]*[\"\']?3\.13(\.[0-9]+)?[\"\']?([[:space:]]|$) ]] ||
        { echo "the Webserver (pytest) job does not set up Python 3.13: $(clip "$setup")"; return 1; }
}

check_security_webserver_python() {
    # shellcheck disable=SC2016
    docker exec "$SECURITY_CID" bash -c '
        for p in /proc/[0-9]*; do
            case "$(tr "\0" " " <"$p/cmdline" 2>/dev/null)" in
                *"-m webserver.app"*) exe="$(readlink "$p/exe")"; break ;;
            esac
        done
        [ -n "${exe:-}" ] || { echo "no webserver.app process"; exit 1; }
        v="$("$exe" -c "import sys; print(\"%d.%d\" % sys.version_info[:2])")"
        [ "$v" = 3.13 ] || { echo "the webserver runs $exe, Python $v"; exit 1; }'
}

check_security_plugin_host_python() {
    local libs
    libs="$(docker exec "$SECURITY_CID" ldd /workdir/build/plc_main 2>&1 | grep -o 'libpython[0-9.]*\.so[0-9.]*' | head -n 1)"
    [[ "$libs" == libpython3.13.so* ]] || { echo "plc_main embeds ${libs:-no libpython}"; return 1; }
}

# Checks one Python plugin of plugins_default.conf: check_security_plugin <name>.
check_security_plugin() {
    local line path venv
    line="$(awk -F , -v n="$1" '$1 == n && $4 == 0 { print $2 "," $6; exit }' "$REPO_ROOT/plugins_default.conf")"
    [ -n "$line" ] || { echo "plugins_default.conf has no Python plugin $1"; return 1; }
    path="${line%%,*}"
    venv="${line#*,}"
    security_probe none "/workdir/${venv#./}/bin/python" plugin "$(plugin_dependency "$1")" "$path"
}

check_security_tls() {
    runtime_probe tls "$1" "$2"
}

check_security_auth() {
    runtime_probe auth "$1"
}

# Checks an archive is refused and no entry of it lands outside the extraction directory.
check_security_archive() {
    local verdict rc=0 escaped
    verdict="$(runtime_probe archive "$1")" || rc=1
    escaped="$(docker exec "$SECURITY_CID" find / -xdev -name "image-test-marker-$1*" ! -path '/workdir/core/generated/*' -print 2>/dev/null)"
    [ "$rc" = 0 ] || echo "$verdict"
    [ -z "$escaped" ] || { echo "written outside the extraction directory: $(printf '%s' "$escaped" | tr '\n' ' ')"; rc=1; }
    return "$rc"
}

check_security_opcua_profile() {
    security_probe none /workdir/venvs/opcua/bin/python opcua Basic256Sha256 SignAndEncrypt
}

# Records every built-image security check as failed with the same reason.
security_fail_all() {
    local id
    for id in $SECURITY_CHECKS; do
        report "security/$id" 1 "$1"
    done
}

# Runs the security checks against a container of an image: run_security_checks <image>.
run_security_checks() {
    local name
    SECURITY_IMAGE="$1"
    if [ -z "$SECURITY_IMAGE" ] || ! docker image inspect "$SECURITY_IMAGE" >/dev/null 2>&1; then
        security_fail_all "no built image to check"
        return
    fi
    SECURITY_CID="$(built_start_healthy "$SECURITY_IMAGE")" ||
        { security_fail_all "the runtime container did not start: $(printf '%s' "$SECURITY_CID" | tr '\n' ' ')"; return; }
    SECURITY_JWT_KEY="$(docker exec "$SECURITY_CID" sed -n 's/^JWT_SECRET_KEY=//p' /var/run/runtime/.env 2>/dev/null)"
    run_check security/webserver-python-3.13 check_security_webserver_python
    run_check security/plugin-host-python-3.13 check_security_plugin_host_python
    for name in modbus_slave modbus_master opcua; do
        run_check "security/plugin-$name-starts-on-python-3.13" check_security_plugin "$name"
    done
    run_check security/tls-1.0-refused check_security_tls 1.0 refused
    run_check security/tls-1.1-refused check_security_tls 1.1 refused
    run_check security/tls-1.2-accepted check_security_tls 1.2 accepted
    run_check security/tls-1.3-accepted check_security_tls 1.3 accepted
    run_check security/upload-without-token-refused check_security_auth none
    run_check security/upload-token-of-other-key-refused check_security_auth other-key
    run_check security/upload-expired-token-refused check_security_auth expired
    run_check security/upload-dotdot-entry-refused check_security_archive dotdot
    run_check security/upload-absolute-entry-refused check_security_archive absolute
    run_check security/upload-expanding-archive-refused check_security_archive expanding
    run_check security/opcua-offers-only-configured-profile check_security_opcua_profile
    docker rm -f "$SECURITY_CID" >/dev/null 2>&1
    SECURITY_CID=""
}

# Runs the security checks that need no Docker.
run_security_static_checks() {
    run_check security/ci-webserver-python-3.13 check_security_ci_webserver_python
}
