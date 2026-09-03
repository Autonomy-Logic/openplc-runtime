#!/usr/bin/env python3
"""End-to-end tests for the RTOP-283 bootloader, run inside the test host.

These drive the real thing: a real Docker daemon, a real registry, real image
pulls with real progress streaming, and the bootloader binary that ships. The
runtime under management is usually a stub (see stubruntime/main.go) because
the failure modes are the interesting part and a real runtime cannot be asked
to exit 1 during start-up on demand. One case swaps in the real runtime image
to prove it actually comes up and reports the right policy.

What is deliberately NOT covered here, and is what the SLM-RP4 round is for:
hardware. There is no /dev/spidev6.0 or /dev/gpiochip0 in a container on a
developer machine, so VPP plugin behaviour and genuine SCHED_FIFO latency have
to be validated on a device.

Standard library only -- the test host has python3 and nothing else.
"""

import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

REGISTRY = "localhost:5000"
STUB_REPO = f"{REGISTRY}/openplc-stub"
REAL_REPO = f"{REGISTRY}/openplc-runtime"

BOOTLOADER_IMAGE = "openplc-bootloader:test"
BOOTLOADER_NAME = "openplc-bootloader"
RUNTIME_NAME = "openplc-runtime"

STATE_DIR = "/var/lib/openplc-bootloader"
DATA_DIR = "/var/lib/openplc-runtime"
BOOTLOADER_URL = "https://127.0.0.1:8445"

# From the shared auth vector (bootloader/internal/runtimeauth/runtimeauth_test.go
# and tests/pytest/restapi/test_bootloader_auth_vector.py). Reusing it here
# means the data directory can be seeded with a genuine werkzeug hash without
# werkzeug being installed in the test host -- and it cross-checks the vector
# in a real integration setting rather than only in unit tests.
PEPPER = "a" * 64
JWT_SECRET = "b" * 64
USERNAME = "operator"
PASSWORD = "correct horse battery staple"
PASSWORD_HASH = (
    "pbkdf2:sha256:600000$WCXqtZujfdFXqzAB$"
    "4be2d44037a7d62f2483d1a189bd2dacb66871b323871f185a73a8e2d3230611"
)

_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


# --- plumbing -------------------------------------------------------------


class Failure(Exception):
    """A test assertion failed."""


def sh(*args: str, check: bool = True) -> str:
    """Run a command and return its stdout."""
    result = subprocess.run(
        args, capture_output=True, text=True, check=False, timeout=600
    )
    if check and result.returncode != 0:
        raise Failure(
            f"command failed: {' '.join(args)}\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def http(
    path: str, method: str = "GET", body: dict | None = None, token: str | None = None
) -> tuple[int, dict]:
    """Call the bootloader API, returning (status, decoded body)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BOOTLOADER_URL + path, data=data, method=method
    )
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, context=_TLS, timeout=30) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read().decode()
        try:
            return err.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return err.code, {"raw": raw}


def wait_for(description: str, predicate, timeout: float = 90.0, interval: float = 0.5):
    """Poll until predicate returns a truthy value, or fail with context."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as err:  # noqa: BLE001 - a transient failure is expected while polling
            last = err
        time.sleep(interval)
    raise Failure(f"timed out waiting for {description} (last observed: {last!r})")


def seed_data_dir() -> None:
    """Create the runtime data directory the bootloader authenticates against.

    A real .env and a real users row, so login exercises the actual PBKDF2 and
    SQLite paths rather than a mock.
    """
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, ".env"), "w", encoding="utf-8") as handle:
        handle.write("FLASK_ENV=development\n")
        handle.write(f"SQLALCHEMY_DATABASE_URI=sqlite:///{DATA_DIR}/restapi.db\n")
        handle.write(f"JWT_SECRET_KEY={JWT_SECRET}\n")
        handle.write(f"PEPPER={PEPPER}\n")

    db_path = os.path.join(DATA_DIR, "restapi.db")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE users ("
            "id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, "
            "password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'admin')"
        )
        connection.execute(
            "INSERT INTO users (id, username, password_hash, role) VALUES (?,?,?,?)",
            (1, USERNAME, PASSWORD_HASH, "admin"),
        )
        connection.commit()
    finally:
        connection.close()


def write_spec(repository: str, version: str, extra_env: list[str] | None = None) -> None:
    """Write the bootloader's runtime spec, as install.sh would."""
    os.makedirs(STATE_DIR, exist_ok=True)
    spec = {
        "repository": repository,
        "version": version,
        "dataDir": DATA_DIR,
        "bootloaderPort": 8445,
    }
    if extra_env:
        spec["extraEnv"] = extra_env
    with open(os.path.join(STATE_DIR, "runtime-spec.json"), "w", encoding="utf-8") as handle:
        json.dump(spec, handle, indent=2)


def start_bootloader(extra_args: list[str] | None = None) -> None:
    remove_container(BOOTLOADER_NAME)
    args = [
        "docker", "run", "-d", "--name", BOOTLOADER_NAME,
        "--restart", "always", "--network", "host",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{STATE_DIR}:{STATE_DIR}",
        "-v", f"{DATA_DIR}:{DATA_DIR}:ro",
        BOOTLOADER_IMAGE,
    ]
    args += extra_args or ["-log-level=debug"]
    sh(*args)

    # Wait for THIS bootloader to answer before returning.
    #
    # Without it a case starts polling while nothing is listening on 8445 yet,
    # and the failures read as "ConnectionRefused" or -- worse -- as progress
    # belonging to a different case, because a poll can land on a bootloader
    # that has not been replaced yet. Confirming a fresh, responsive process
    # removes both by construction.
    def responsive() -> bool:
        state = container_state(BOOTLOADER_NAME)
        if not state.get("State", {}).get("Running"):
            # A bootloader that exits is restarted by its policy, so a
            # crash-loop presents as "never becomes responsive". Surface its
            # own output rather than leaving a bare timeout.
            return False
        status, _ = http("/api/bootloader/capabilities")
        return status == 200

    try:
        wait_for("the bootloader to start answering", responsive, timeout=60, interval=0.25)
    except Failure as err:
        logs = sh("docker", "logs", "--tail", "30", BOOTLOADER_NAME, check=False)
        raise Failure(f"{err}\nbootloader logs:\n{logs}") from err


def reset(repository: str = STUB_REPO, version: str = "v1.0.0",
          extra_env: list[str] | None = None) -> None:
    """Return the host to a known state and start the bootloader."""
    # Bootloader first: it would otherwise notice the runtime disappearing and
    # helpfully recreate it, which is exactly its job and exactly wrong here.
    remove_container(BOOTLOADER_NAME)
    remove_container(RUNTIME_NAME)
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    seed_data_dir()
    write_spec(repository, version, extra_env)
    start_bootloader()


def login() -> str:
    status, body = wait_for(
        "bootloader login",
        lambda: (lambda r: r if r[0] == 200 else None)(
            http("/api/bootloader/login", "POST",
                 {"username": USERNAME, "password": PASSWORD})
        ),
        timeout=60,
    )
    if status != 200:
        raise Failure(f"login returned {status}: {body}")
    return body["access_token"]


def remove_container(name: str) -> None:
    """Remove a container and wait until it is genuinely gone.

    `docker rm -f` returns before removal completes. Starting a replacement
    under the same name in that window fails with "container is marked for
    removal", and -- more insidiously -- a bootloader that is still alive keeps
    supervising with the spec it loaded at startup, so a later case sees
    environment it never asked for. Both were real failures in this suite
    before this wait existed.
    """
    sh("docker", "rm", "-f", name, check=False)
    deadline = time.time() + 60
    while time.time() < deadline:
        if not container_exists(name):
            return
        time.sleep(0.2)
    raise Failure(f"container {name} was still present 60s after removal")


def container_exists(name: str) -> bool:
    """Whether a container of this exact name exists.

    `docker ps -aq -f name=^x$` rather than `docker inspect`: inspect prints
    "[]" on stdout for a missing container and only signals absence through
    its exit code, so a naive empty-stdout check never sees the container go
    away. This filter prints an id or nothing, with no ambiguity.
    """
    return sh("docker", "ps", "-aq", "-f", f"name=^{name}$", check=False) != ""


def container_state(name: str) -> dict:
    raw = sh("docker", "inspect", name, check=False)
    if not raw:
        return {}
    parsed = json.loads(raw)
    # A missing container inspects to an empty list, not to nothing.
    return parsed[0] if parsed else {}


def bootloader_state() -> str:
    _, body = http("/api/bootloader/capabilities")
    return body.get("state", "")


def wait_healthy(timeout: float = 120.0) -> None:
    wait_for("the bootloader to report healthy",
             lambda: bootloader_state() == "healthy", timeout=timeout)


def runtime_version_served() -> dict:
    """Read the managed runtime's own /api/capabilities."""
    request = urllib.request.Request("https://127.0.0.1:8443/api/capabilities")
    with urllib.request.urlopen(request, context=_TLS, timeout=15) as response:
        return json.loads(response.read().decode())


def image_present(reference: str) -> bool:
    """Whether this exact image reference resolves locally.

    `docker images -q <ref>` prints an id or nothing. `docker image inspect`
    would not do: like its container counterpart it prints "[]" on stdout for
    a missing image and signals absence only through its exit code, so an
    empty-stdout check returns True for everything -- which quietly made this
    assertion, and two others that rely on it, pass no matter what happened.

    Note the stub versions in this harness are all tags of one image, so
    "retired" here means the TAG is gone. That is exactly what the updater
    removes, and Docker untags a shared image happily even while a container
    is running from another of its tags.
    """
    return sh("docker", "images", "-q", reference, check=False) != ""


def run_update(token: str, version: str) -> tuple[int, dict]:
    return http("/api/bootloader/update", "POST", {"version": version}, token)


def wait_update(token: str, expected: str, timeout: float = 180.0) -> dict:
    def check():
        _, body = http("/api/bootloader/update", token=token)
        if body.get("state") in ("success", "failed"):
            return body
        return None

    result = wait_for(f"the update to finish ({expected})", check, timeout=timeout)
    if result["state"] != expected:
        raise Failure(f"update ended in {result['state']}, wanted {expected}: {result}")
    return result


# --- cases ----------------------------------------------------------------

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def test_bootstrap_creates_and_supervises_the_runtime():
    """A device with no runtime container gets one, and it comes up healthy."""
    reset()
    wait_healthy()

    runtime = container_state(RUNTIME_NAME)
    if not runtime:
        raise Failure("the bootloader did not create a runtime container")

    host = runtime["HostConfig"]
    if not host["Privileged"]:
        raise Failure("the runtime must be privileged for hardware parity")
    if host["NetworkMode"] != "host":
        raise Failure(f"want host networking, got {host['NetworkMode']}")
    if host["RestartPolicy"]["Name"] != "no":
        raise Failure("the bootloader owns restarts; docker must not")
    if "/dev:/dev" not in host["Binds"]:
        raise Failure(f"/dev must be bound, got {host['Binds']}")

    # The trap that is not a privilege: any CPU limit enables the cgroup CPU
    # controller and SCHED_FIFO then fails silently.
    for field in ("NanoCpus", "CpuQuota", "CpuPeriod", "Memory", "CpuShares"):
        if host.get(field):
            raise Failure(f"{field} must be unset, got {host[field]}")

    limits = {u["Name"]: u["Soft"] for u in host.get("Ulimits") or []}
    if limits.get("rtprio") != 99 or limits.get("memlock") != -1:
        raise Failure(f"real-time ulimits are wrong: {limits}")


@case
def test_the_runtime_is_told_to_use_the_mounted_data_directory():
    """The bind alone is not enough: the runtime resolves its data dir by
    detection, so without the env override it writes a fresh database inside
    the container and every swap loses users, the project and licenses."""
    reset()
    wait_healthy()
    served = runtime_version_served()
    if served.get("dataDir") != DATA_DIR:
        raise Failure(
            f"the runtime was not pointed at {DATA_DIR}, it reports {served.get('dataDir')!r}"
        )


@case
def test_the_runtime_is_told_it_is_bootloader_managed():
    """updatePolicy 'self' is what makes the editor offer the update action,
    and only our bootloader sets it."""
    reset()
    wait_healthy()
    served = runtime_version_served()
    if served.get("updatePolicy") != "self":
        raise Failure(f"want updatePolicy self, got {served.get('updatePolicy')!r}")
    if str(served.get("bootloaderPort")) != "8445":
        raise Failure(f"want bootloaderPort 8445, got {served.get('bootloaderPort')!r}")


@case
def test_restarting_the_bootloader_adopts_the_running_runtime():
    """The bootloader restarts far more often than the runtime does. A
    reconcile that recreated or bounced a working runtime would turn a
    bootloader hiccup into a plant outage."""
    reset()
    wait_healthy()
    before_state = container_state(RUNTIME_NAME)
    before, started_at = before_state["Id"], before_state["State"]["StartedAt"]

    # Key off the BOOTLOADER's own start time. Trying to catch the restart
    # window by polling for "not running" is a race the restart usually wins,
    # and then the assertion runs against the old process having done nothing.
    bootloader_started = container_state(BOOTLOADER_NAME)["State"]["StartedAt"]
    sh("docker", "restart", BOOTLOADER_NAME)
    wait_for(
        "the bootloader process to be replaced",
        lambda: container_state(BOOTLOADER_NAME).get("State", {}).get("StartedAt")
        not in (None, bootloader_started),
        timeout=60,
        interval=0.2,
    )
    wait_healthy()

    after = container_state(RUNTIME_NAME)
    if after["Id"] != before:
        raise Failure("the runtime container was recreated instead of adopted")
    if after["State"]["StartedAt"] != started_at:
        raise Failure("the runtime was restarted instead of adopted")


@case
def test_an_upgrade_pulls_swaps_and_retires_the_old_image():
    reset(version="v1.0.0")
    wait_healthy()
    token = login()

    status, body = run_update(token, "v1.0.1")
    if status != 202:
        raise Failure(f"want 202 accepted, got {status}: {body}")

    wait_update(token, "success")
    wait_healthy()

    if container_state(RUNTIME_NAME)["Config"]["Image"] != f"{STUB_REPO}:v1.0.1":
        raise Failure("the runtime is not running the new image")
    if image_present(f"{STUB_REPO}:v1.0.0"):
        raise Failure("the previous image should have been retired")
    # The choice has to survive a reboot, or the device reverts on next boot.
    with open(os.path.join(STATE_DIR, "runtime-spec.json"), encoding="utf-8") as handle:
        if json.load(handle)["version"] != "v1.0.1":
            raise Failure("the new version was not recorded in the spec")


@case
def test_a_downgrade_is_the_same_operation():
    """No version floor: a user may deliberately pair an older runtime with an
    older editor."""
    reset(version="v1.0.1")
    wait_healthy()
    token = login()

    status, _ = run_update(token, "v0.9.0")
    if status != 202:
        raise Failure(f"a downgrade must be accepted, got {status}")
    wait_update(token, "success")
    wait_healthy()

    if container_state(RUNTIME_NAME)["Config"]["Image"] != f"{STUB_REPO}:v0.9.0":
        raise Failure("the downgrade did not take effect")


@case
def test_a_version_that_does_not_exist_fails_without_touching_the_runtime():
    """A failed pull must never interrupt a working PLC."""
    reset(version="v1.0.0")
    wait_healthy()
    token = login()
    running_before = container_state(RUNTIME_NAME)["Id"]

    status, _ = run_update(token, "v6.6.6")
    if status != 202:
        raise Failure(f"want 202, got {status}")
    result = wait_update(token, "failed")
    if "could not download" not in result["error"]:
        raise Failure(f"want a download failure, got {result['error']!r}")

    # The old image must still be there, and the spec unchanged.
    if not image_present(f"{STUB_REPO}:v1.0.0"):
        raise Failure("a failed pull must not remove the working image")
    with open(os.path.join(STATE_DIR, "runtime-spec.json"), encoding="utf-8") as handle:
        if json.load(handle)["version"] != "v1.0.0":
            raise Failure("a failed pull must not change the recorded version")

    # And the PLC must still be running. Nothing was touched, so a bad version
    # name must not take a working plant offline -- which is exactly what
    # happened on the SLM-RP4 before this was fixed, and what this assertion
    # previously waved through.
    running_after = container_state(RUNTIME_NAME)
    if running_after.get("Id") != running_before:
        raise Failure("a failed pull must not replace the runtime container")
    if not running_after.get("State", {}).get("Running"):
        raise Failure("a failed pull must not stop the running runtime")
    if bootloader_state() == "recovery":
        raise Failure("a failure before the swap must not enter recovery")


@case
def test_a_new_version_that_will_not_start_enters_recovery():
    """The case pull-first ordering exists for: the operator is handed a device
    in recovery that still has the previous image on disk."""
    # STUB_FAIL is injected through the spec's extraEnv, so the NEW container
    # inherits it and exits immediately during start-up.
    reset(version="v1.0.0")
    wait_healthy()
    token = login()

    write_spec(STUB_REPO, "v1.0.0", extra_env=["STUB_FAIL=exit"])
    sh("docker", "restart", BOOTLOADER_NAME)
    # The running container predates the env change, so it stays healthy; the
    # bootloader adopts it.
    wait_healthy()

    status, _ = run_update(token, "v1.0.2")
    if status != 202:
        raise Failure(f"want 202, got {status}")
    result = wait_update(token, "failed")
    if "did not start" not in result["error"]:
        raise Failure(f"want a start failure, got {result['error']!r}")

    wait_for("recovery mode", lambda: bootloader_state() == "recovery", timeout=60)
    _, caps = http("/api/bootloader/capabilities")
    if not caps.get("recovery"):
        raise Failure("capabilities must advertise recovery without a token")
    if not image_present(f"{STUB_REPO}:v1.0.0"):
        raise Failure("the previous image must survive a failed start")


@case
def test_recovery_can_install_a_working_version():
    """Recovery is only useful if something can be done from it."""
    reset(version="v1.0.0", extra_env=["STUB_FAIL=exit"])
    wait_for("recovery mode", lambda: bootloader_state() == "recovery", timeout=120)

    token = login()
    # Clear the failure injection, then install a version from recovery.
    write_spec(STUB_REPO, "v1.0.0")
    sh("docker", "restart", BOOTLOADER_NAME)
    wait_for("recovery mode after restart",
             lambda: bootloader_state() in ("recovery", "healthy"), timeout=120)

    token = login()
    status, _ = run_update(token, "v1.0.1")
    if status != 202:
        raise Failure(f"want 202 from recovery, got {status}")
    wait_update(token, "success")
    wait_healthy()


@case
def test_a_crash_looping_runtime_ends_in_recovery():
    """Three unexpected exits in the window. The stub serves healthy first and
    then dies, which is the shape that matters: a healthy start must not clear
    the crash window, or the threshold is never reached."""
    reset(version="v1.0.0",
          extra_env=["STUB_FAIL=crash-loop", "STUB_CRASH_AFTER=15"])
    wait_for("recovery after repeated crashes",
             lambda: bootloader_state() == "recovery", timeout=180)

    token = login()
    _, status_body = http("/api/bootloader/status", token=token)
    if status_body.get("crashCount", 0) < 3:
        raise Failure(f"want at least 3 crashes recorded, got {status_body}")
    if "exited" not in (status_body.get("reason") or ""):
        raise Failure(f"the reason must explain the crash loop: {status_body.get('reason')!r}")


@case
def test_a_second_concurrent_update_is_refused():
    reset(version="v1.0.0")
    wait_healthy()
    token = login()

    # The real runtime image is ~1 GB, so this pull takes long enough to make
    # the race observable without any artificial delay.
    status, _ = http("/api/bootloader/update", "POST", {"version": "v4.2.1"}, token)
    if status != 202:
        raise Failure(f"want 202 for the first update, got {status}")
    try:
        second, body = run_update(token, "v1.0.1")
        if second != 409:
            raise Failure(f"want 409 for a concurrent update, got {second}: {body}")
        if not body.get("progress"):
            raise Failure("the in-flight progress must be attached to the 409")
    finally:
        # This update points at a repository the stub spec does not use, so it
        # will fail; let it settle rather than leaving a pull running.
        try:
            wait_for("the first update to settle",
                     lambda: http("/api/bootloader/update", token=token)[1].get("state")
                     in ("success", "failed"), timeout=240)
        except Failure:
            pass


@case
def test_an_invalid_version_is_refused_with_a_reason():
    reset(version="v1.0.0")
    wait_healthy()
    token = login()
    for version in ["evil.example.com/x:v1", "v1/../../etc", "v1.0.0@sha256:abc", ""]:
        status, body = run_update(token, version)
        if status != 400:
            raise Failure(f"version {version!r} should be refused, got {status}")
        if not body.get("error"):
            raise Failure(f"version {version!r} was refused without a reason")


@case
def test_the_api_requires_authentication():
    reset(version="v1.0.0")
    wait_healthy()
    for path, method in [
        ("/api/bootloader/status", "GET"),
        ("/api/bootloader/logs", "GET"),
        ("/api/bootloader/update", "GET"),
        ("/api/bootloader/update", "POST"),
        ("/api/bootloader/restart", "POST"),
    ]:
        body = {"version": "v1.0.1"} if method == "POST" else None
        status, _ = http(path, method, body)
        if status != 401:
            raise Failure(f"{method} {path} must require a token, got {status}")

    # Capabilities stays open, so a client can identify the device first.
    status, _ = http("/api/bootloader/capabilities")
    if status != 200:
        raise Failure(f"capabilities must stay unauthenticated, got {status}")


@case
def test_a_bad_password_is_refused():
    reset(version="v1.0.0")
    wait_healthy()
    status, body = http("/api/bootloader/login", "POST",
                        {"username": USERNAME, "password": "wrong"})
    if status != 401:
        raise Failure(f"want 401, got {status}: {body}")
    status, _ = http("/api/bootloader/login", "POST",
                     {"username": "nobody", "password": PASSWORD})
    if status != 401:
        raise Failure(f"an unknown user must also get 401, got {status}")


@case
def test_logs_are_readable_without_shell_access():
    """The whole point: seeing why a runtime will not start, from the editor."""
    reset(version="v1.0.0")
    wait_healthy()
    token = login()
    _, body = http("/api/bootloader/logs?tail=50", token=token)
    if not body.get("available"):
        raise Failure(f"logs should be available: {body}")
    if "listening" not in body.get("logs", ""):
        raise Failure(f"the runtime's own output should come through: {body.get('logs')!r}")


@case
def test_restart_brings_the_runtime_back():
    reset(version="v1.0.0")
    wait_healthy()
    token = login()
    before = container_state(RUNTIME_NAME)["State"]["StartedAt"]

    status, body = http("/api/bootloader/restart", "POST", {}, token)
    if status != 200:
        raise Failure(f"want 200, got {status}: {body}")
    wait_healthy()
    after = container_state(RUNTIME_NAME)["State"]["StartedAt"]
    if after == before:
        raise Failure("restart did not actually restart the runtime")


@case
def test_the_bootloader_replaces_itself_without_disturbing_the_runtime():
    """A bootloader update must not interrupt a running PLC.

    Losing the ability to manage a device is a bad afternoon; stopping its
    plant is a different category of problem. This is the whole reason the
    swap is done by a one-shot helper from outside rather than by the
    bootloader trying to remove itself.
    """
    reset(version="v1.0.0")
    wait_healthy()
    token = login()

    runtime_before = container_state(RUNTIME_NAME)["Id"]
    started_before = container_state(RUNTIME_NAME)["State"]["StartedAt"]
    bootloader_before = container_state(BOOTLOADER_NAME)["Id"]

    # The bootloader pulls its replacement from its own repository, so point
    # that at the local registry and publish a tag there to pull.
    sh("docker", "tag", BOOTLOADER_IMAGE, f"{REGISTRY}/openplc-bootloader:v2")
    sh("docker", "push", "-q", f"{REGISTRY}/openplc-bootloader:v2")
    # Restart the bootloader with the repository override so its self-update
    # resolves inside the harness rather than reaching for ghcr.io.
    remove_container(BOOTLOADER_NAME)
    sh("docker", "run", "-d", "--name", BOOTLOADER_NAME,
       "--restart", "always", "--network", "host",
       "-e", f"OPENPLC_BOOTLOADER_REPOSITORY={REGISTRY}/openplc-bootloader",
       "-v", "/var/run/docker.sock:/var/run/docker.sock",
       "-v", f"{STATE_DIR}:{STATE_DIR}",
       "-v", f"{DATA_DIR}:{DATA_DIR}:ro",
       BOOTLOADER_IMAGE, "-log-level=debug")
    wait_healthy()
    token = login()
    bootloader_before = container_state(BOOTLOADER_NAME)["Id"]

    status, body = http("/api/bootloader/self-update", "POST", {"version": "v2"}, token)
    if status != 202:
        raise Failure(f"want 202, got {status}: {body}")

    # The bootloader is replaced, so it goes away and comes back under the
    # same name with a new container id.
    wait_for(
        "the bootloader to be replaced",
        lambda: container_state(BOOTLOADER_NAME).get("Id") not in (None, "", bootloader_before),
        timeout=120,
        interval=0.5,
    )
    wait_healthy(timeout=120)

    runtime_after = container_state(RUNTIME_NAME)
    if runtime_after["Id"] != runtime_before:
        raise Failure("the runtime container was replaced by a bootloader update")
    if runtime_after["State"]["StartedAt"] != started_before:
        raise Failure("the runtime was restarted by a bootloader update")
    if not runtime_after["State"]["Running"]:
        raise Failure("the runtime must keep running throughout a bootloader update")

    # The one-shot helper must not be left with a restart policy, or it would
    # re-run the swap on every daemon start.
    helper = container_state(f"{BOOTLOADER_NAME}-selfupdate")
    if helper and helper.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") != "no":
        raise Failure(f"the helper must never restart: {helper.get('HostConfig')}")


@case
def test_the_real_runtime_image_comes_up_under_the_bootloader():
    """Everything above uses the stub. This proves the real thing works: the
    actual OpenPLC runtime, started by the bootloader, reaching healthy and
    reporting the policy the editor keys off."""
    reset(repository=REAL_REPO, version="v4.2.1")
    # Generous: the real runtime loads plugin venvs on a cold start, and the
    # image's own start-period is 90s.
    wait_healthy(timeout=300)

    served = runtime_version_served()
    if served.get("updatePolicy") != "self":
        raise Failure(f"want updatePolicy self, got {served}")
    if not served.get("runtimeVersion"):
        raise Failure(f"the real runtime must report a version, got {served}")

    # The data-directory bug, checked against the real runtime rather than a
    # field the stub invents. config.py resolves the persistent directory by
    # container detection, so without the env override the runtime writes a
    # fresh .env inside the container and ignores the mounted one -- losing
    # users, the stored project, retain data and licences on every swap.
    env = container_state(RUNTIME_NAME)["Config"]["Env"]
    if f"OPENPLC_PERSISTENT_DATA_DIR={DATA_DIR}" not in env:
        raise Failure(f"the runtime was not pointed at the mounted data dir: {env}")

    inside = sh("docker", "exec", RUNTIME_NAME, "ls", "-a", "/var/run/runtime", check=False)
    if ".env" in inside.split():
        raise Failure(
            "the real runtime wrote its .env inside the container instead of "
            f"the mount, so a version swap would discard it: {inside!r}"
        )
    if not os.path.exists(os.path.join(DATA_DIR, ".env")):
        raise Failure("the real runtime did not write .env into the mounted data dir")


# --- runner ---------------------------------------------------------------


def main() -> int:
    # Leftovers from an interrupted run keep holding port 8445, and a poll that
    # lands on one reports another run's progress entirely. Clear them first.
    for name in (BOOTLOADER_NAME, RUNTIME_NAME):
        remove_container(name)

    only = sys.argv[1] if len(sys.argv) > 1 else None
    selected = [c for c in CASES if not only or only in c.__name__]
    if not selected:
        print(f"no cases match {only!r}")
        return 2

    passed, failed = 0, []
    for fn in selected:
        name = fn.__name__
        print(f"\n\033[0;34m=== {name}\033[0m", flush=True)
        started = time.time()
        try:
            fn()
        except Exception as err:  # noqa: BLE001 - report every failure, keep going
            failed.append((name, err))
            print(f"\033[0;31mFAIL\033[0m ({time.time() - started:.1f}s): {err}", flush=True)
        else:
            passed += 1
            print(f"\033[0;32mPASS\033[0m ({time.time() - started:.1f}s)", flush=True)

    print(f"\n{'=' * 60}\n{passed} passed, {len(failed)} failed")
    for name, err in failed:
        print(f"  FAILED {name}: {str(err).splitlines()[0]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
