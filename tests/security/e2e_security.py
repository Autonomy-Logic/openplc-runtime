# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""End-to-end check of the runtime's security envelope against a running container.

Starts the runtime image with its data directory on a Docker volume, drives the REST API over
HTTPS, and restarts the container where a check must hold across a restart: roles per endpoint,
the refused role name ``user``, token rotation and reuse, sign-out of one session, account-wide
ends (role change, deletion) and sign-in throttling.

Usage: python3 tests/security/e2e_security.py [image] [--upgrade-from OLD_IMAGE]
(default image: openplc-runtime:p8-e2e). With ``--upgrade-from``, an account with the old role
``user`` is first created on OLD_IMAGE, and the run checks it became ``engineer`` after the upgrade.
"""

import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

ARGS = sys.argv[1:]
UPGRADE_FROM = ARGS[ARGS.index("--upgrade-from") + 1] if "--upgrade-from" in ARGS else None
POSITIONAL = [
    a
    for i, a in enumerate(ARGS)
    if a != "--upgrade-from" and (i == 0 or ARGS[i - 1] != "--upgrade-from")
]
IMAGE = POSITIONAL[0] if POSITIONAL else "openplc-runtime:p8-e2e"
CONTAINER = "openplc-p8-e2e"
VOLUME = "openplc-p8-e2e-data"
PORT = 18443
BASE = f"https://127.0.0.1:{PORT}/api"
TLS = ssl.create_default_context()
TLS.check_hostname = False
TLS.verify_mode = ssl.CERT_NONE


@dataclass
class Reply:
    status: int
    body: object
    headers: dict[str, str]


def call(method: str, path: str, token: str | None = None, body: object = None) -> Reply:
    """One HTTPS request to the runtime API."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, context=TLS, timeout=30) as response:
            raw = response.read()
            return Reply(response.status, _json(raw), dict(response.headers))
    except urllib.error.HTTPError as error:
        return Reply(error.code, _json(error.read()), dict(error.headers))


def _json(raw: bytes) -> object:
    try:
        return json.loads(raw or b"null")
    except json.JSONDecodeError:
        return raw.decode(errors="replace")


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


def wait_ready(timeout_s: float = 120) -> None:
    """Waits until the API answers /api/version."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if call("GET", "/version").status == 200:
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1)
    raise RuntimeError("the runtime did not answer /api/version")


def start(image: str = IMAGE) -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, check=False)
    docker(
        "run",
        "-d",
        "--name",
        CONTAINER,
        "--cap-add=sys_nice",
        "-p",
        f"{PORT}:8443",
        "-v",
        f"{VOLUME}:/var/run/runtime",
        image,
    )
    wait_ready()


def restart() -> None:
    docker("restart", CONTAINER)
    wait_ready()


def cleanup() -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, check=False)
    subprocess.run(["docker", "volume", "rm", "-f", VOLUME], capture_output=True, check=False)


def login(username: str, password: str) -> str:
    reply = call("POST", "/login", body={"username": username, "password": password})
    assert reply.status == 200, (username, reply)
    assert isinstance(reply.body, dict)
    return str(reply.body["access_token"])


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition else f'  {detail}'}")
    if not condition:
        raise SystemExit(1)


def user_id(token: str, username: str) -> int:
    users = call("GET", "/get-users-info", token).body
    assert isinstance(users, list)
    return next(int(u["id"]) for u in users if u["username"] == username)


PLC_ONLY = [
    ("GET", "/start-plc"),
    ("GET", "/stop-plc"),
    ("GET", "/runtime-logs"),
    ("POST", "/upload-file"),
    ("POST", "/plugin-command"),
    ("GET", "/discovery/interfaces"),
    ("GET", "/get-users-info"),
]


def upgrade_from(old_image: str) -> None:
    """Creates an account with the old role ``user`` on ``old_image``, then upgrades to ``IMAGE``."""
    start(old_image)
    assert (
        call("POST", "/create-user", body={"username": "admin", "password": "admin-pass"}).status
        == 201
    )
    admin = login("admin", "admin-pass")
    reply = call(
        "POST", "/create-user", admin, {"username": "tech", "password": "tech-pass", "role": "user"}
    )
    check("the old runtime creates a user account", reply.status == 201, reply)
    start()
    admin = login("admin", "admin-pass")
    users = call("GET", "/get-users-info", admin).body
    roles = {u["username"]: u["role"] for u in users} if isinstance(users, list) else users
    check(
        "TC08 after the upgrade the user account is engineer",
        roles == {"admin": "admin", "tech": "engineer"},
        roles,
    )
    check(
        "TC08 the migrated engineer reaches PLC operation",
        call("GET", "/runtime-logs", login("tech", "tech-pass")).status == 200,
    )
    call("DELETE", f"/delete-user/{user_id(admin, 'tech')}", admin)


def main() -> None:
    cleanup()
    if UPGRADE_FROM:
        upgrade_from(UPGRADE_FROM)
    else:
        start()
        assert (
            call(
                "POST", "/create-user", body={"username": "admin", "password": "admin-pass"}
            ).status
            == 201
        )
    admin = login("admin", "admin-pass")
    for role in ("engineer", "operator", "viewer"):
        reply = call(
            "POST",
            "/create-user",
            admin,
            {"username": role, "password": f"{role}-pass", "role": role},
        )
        check(f"create {role}", reply.status == 201, reply)
    tokens = {role: login(role, f"{role}-pass") for role in ("engineer", "operator", "viewer")}

    for role in ("operator", "viewer"):
        for method, path in (("GET", "/whoami"), ("GET", "/status"), ("GET", "/ping")):
            reply = call(method, path, tokens[role])
            check(f"TC07 {role} reaches {path}", reply.status == 200, reply.status)
        for method, path in PLC_ONLY:
            reply = call(method, path, tokens[role], {} if method == "POST" else None)
            check(f"TC07 {role} refused on {method} {path}", reply.status == 403, reply.status)
    for method, path in PLC_ONLY:
        reply = call(method, path, tokens["engineer"], {} if method == "POST" else None)
        check(f"engineer not refused on {method} {path}", reply.status != 403, reply.status)

    reply = call(
        "POST", "/create-user", admin, {"username": "old", "password": "x", "role": "user"}
    )
    check("TC08 the role user is refused", reply.status == 400, reply)

    renewed = call("POST", "/renew", tokens["engineer"])
    check("TC09 renewal returns a token", renewed.status == 200, renewed)
    assert isinstance(renewed.body, dict)
    new = str(renewed.body["access_token"])
    check("TC09 the renewed token works", call("GET", "/whoami", new).status == 200)
    check(
        "TC09 the predecessor is refused", call("GET", "/whoami", tokens["engineer"]).status == 401
    )
    restart()
    check("TC09 the renewed token works after a restart", call("GET", "/whoami", new).status == 200)
    time.sleep(11)
    check(
        "TC09 reusing the predecessor is refused",
        call("GET", "/whoami", tokens["engineer"]).status == 401,
    )
    check("TC09 ... and ends the session", call("GET", "/whoami", new).status == 401)

    second_admin = login("admin", "admin-pass")
    check("TC10 sign out", call("POST", "/logout", admin).status == 200)
    restart()
    check(
        "TC10 the signed-out session stays out after a restart",
        call("GET", "/whoami", admin).status == 401,
    )
    check("TC10 the other session is still in", call("GET", "/whoami", second_admin).status == 200)

    operator_two = login("operator", "operator-pass")
    reply = call(
        "PUT", f"/update-user/{user_id(second_admin, 'operator')}", second_admin, {"role": "viewer"}
    )
    check("TC24 role change", reply.status == 200, reply)
    restart()
    for token in (tokens["operator"], operator_two):
        check(
            "TC24 every session of the account ended", call("GET", "/whoami", token).status == 401
        )
    fresh = call("GET", "/whoami", login("operator", "operator-pass")).body
    check(
        "TC24 signing in again gives viewer",
        isinstance(fresh, dict) and fresh.get("role") == "viewer",
        fresh,
    )

    viewer_two = login("viewer", "viewer-pass")
    reply = call("DELETE", f"/delete-user/{user_id(second_admin, 'viewer')}", second_admin)
    check("TC10 delete the account", reply.status == 200, reply)
    restart()
    for token in (tokens["viewer"], viewer_two):
        check(
            "TC10 a deleted account's sessions are refused after a restart",
            call("GET", "/whoami", token).status == 401,
        )

    check(
        "FR53 a wrong password",
        call("POST", "/login", body={"username": "engineer", "password": "no"}).status == 401,
    )
    held = call("POST", "/login", body={"username": "engineer", "password": "engineer-pass"})
    check("FR53 the next attempt is throttled", held.status == 429, held)
    check(
        "FR53 with Retry-After", 1 <= int(held.headers.get("Retry-After", "0")) <= 5, held.headers
    )
    time.sleep(int(held.headers["Retry-After"]) + 1)
    check(
        "FR53 accepted after the wait",
        call("POST", "/login", body={"username": "engineer", "password": "engineer-pass"}).status
        == 200,
    )
    reply = call(
        "POST",
        "/create-user",
        second_admin,
        {"username": "panel", "password": "p1", "role": "viewer"},
    )
    check("create a viewer", reply.status == 201, reply)
    panel = login("panel", "p1")
    reply = call(
        "PUT",
        f"/update-user/{user_id(second_admin, 'panel')}",
        panel,
        {"password": "p2", "current_password": "p1"},
    )
    check("a viewer changes its own password", reply.status == 200, reply)
    check("... which ends its sessions", call("GET", "/whoami", panel).status == 401)
    reply = call(
        "PUT",
        f"/update-user/{user_id(second_admin, 'panel')}",
        login("panel", "p2"),
        {"username": "x"},
    )
    check("a viewer may not rename itself", reply.status == 403, reply)
    print("ALL PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(["docker", "logs", "--tail", "40", CONTAINER], check=False)
        cleanup()
