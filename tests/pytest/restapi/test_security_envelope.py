# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The runtime's security envelope: four roles on every endpoint, sessions that
rotate and end durably, and throttled sign-ins."""

import logging

import pytest
from conftest import auth, create_user, login, token_for
from flask_jwt_extended import decode_token
from sqlalchemy import text as sa_text

from webserver import access, restapi, sessions
from webserver.restapi import User, apply_user_schema_migrations, db

ROLES = ("admin", "engineer", "operator", "viewer")


@pytest.fixture()
def tokens(client, admin_token):
    """A token per role; the admin is the bootstrap account."""
    out = {"admin": admin_token}
    for role in ROLES[1:]:
        assert (
            create_user(client, role, f"{role}-pass", token=admin_token, role=role).status_code
            == 201
        )
        out[role] = token_for(client, role, f"{role}-pass")
    return out


@pytest.fixture()
def handlers(monkeypatch):
    """Command handlers that answer without a PLC, so an allowed command returns 200."""
    monkeypatch.setattr(restapi, "_handler_callback_get", lambda command, data: {"ok": command})
    monkeypatch.setattr(restapi, "_handler_callback_post", lambda command, data: {"ok": command})


def _user_id(username):
    return User.query.filter_by(username=username).one().id


# (method, path or callable of the account ids, json body, roles allowed)
ENGINEERING = {"admin", "engineer"}
EVERYONE = set(ROLES)
ENDPOINTS = [
    ("GET", "/api/whoami", None, EVERYONE),
    ("GET", "/api/status", None, EVERYONE),
    ("GET", "/api/ping", None, EVERYONE),
    ("GET", "/api/start-plc", None, ENGINEERING),
    ("GET", "/api/stop-plc", None, ENGINEERING),
    ("GET", "/api/runtime-logs", None, ENGINEERING),
    ("GET", "/api/compilation-status", None, ENGINEERING),
    ("GET", "/api/serial-ports", None, ENGINEERING),
    ("GET", "/api/switch", None, ENGINEERING),
    ("POST", "/api/upload-file", {}, ENGINEERING),
    ("POST", "/api/plugin-command", {}, ENGINEERING),
    ("GET", "/api/get-users-info", None, ENGINEERING),
    ("GET", lambda ids: f"/api/get-user-info/{ids['admin']}", None, ENGINEERING),
    ("PUT", lambda ids: f"/api/update-user/{ids['self']}", {"username": "renamed"}, ENGINEERING),
    ("PUT", lambda ids: f"/api/update-user/{ids['admin']}", {"password": "x"}, {"admin"}),
    ("POST", "/api/create-user", {"username": "x", "password": "y"}, {"admin"}),
    ("GET", "/api/project-snapshot", None, {"admin"}),
    ("POST", "/api/discovery/ethercat/validate", {}, ENGINEERING),
]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("method,path,body,allowed", ENDPOINTS)
def test_each_role_reaches_only_its_endpoints(
    client, tokens, handlers, role, method, path, body, allowed
):
    ids = {"admin": _user_id("admin"), "self": _user_id("admin" if role == "admin" else role)}
    url = path(ids) if callable(path) else path
    resp = client.open(url, method=method, json=body, headers=auth(tokens[role]))
    if role in allowed:
        assert resp.status_code != 403, (role, url, resp.get_json())
    else:
        assert resp.status_code == 403, (role, url, resp.status_code)


@pytest.mark.parametrize("role", ROLES)
def test_every_role_changes_its_own_password_which_ends_its_sessions(client, tokens, role):
    password = "admin-pass" if role == "admin" else f"{role}-pass"
    other = token_for(client, role, password)
    resp = client.put(
        f"/api/update-user/{_user_id(role)}",
        json={"password": "changed", "current_password": password},
        headers=auth(tokens[role]),
    )
    assert resp.status_code == 200, resp.get_json()
    assert client.get("/api/whoami", headers=auth(other)).status_code == 401
    assert login(client, role, "changed").status_code == 200


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_hmi_roles_may_not_delete_accounts(client, tokens, role):
    resp = client.delete(f"/api/delete-user/{_user_id('engineer')}", headers=auth(tokens[role]))
    assert resp.status_code == 403


def test_every_discovery_route_is_for_engineering_roles(app):
    keys = {
        getattr(app.view_functions[rule.endpoint], "access_key", None)
        for rule in app.url_map.iter_rules()
        if rule.endpoint.startswith("discovery.")
    }
    assert keys == {"discovery"}
    assert access.POLICY["discovery"] == {"admin", "engineer"}


def test_every_route_is_covered_by_the_policy(app):
    covered = (
        access.PUBLIC_ENDPOINTS | access.SELF_AUTHENTICATING_ENDPOINTS | access.COMMAND_ENDPOINTS
    )
    missing = [
        rule.endpoint
        for rule in app.url_map.iter_rules()
        if rule.endpoint not in covered
        and getattr(app.view_functions[rule.endpoint], "access_key", None) not in access.POLICY
    ]
    assert missing == []


def test_stored_user_accounts_become_engineers_and_the_role_user_is_refused(client, admin_token):
    db.session.execute(
        sa_text("INSERT INTO users (username, password_hash, role) VALUES ('old', 'x', 'user')")
    )
    db.session.commit()
    apply_user_schema_migrations()
    db.session.expire_all()
    assert User.query.filter_by(username="old").one().role == "engineer"
    resp = create_user(client, "new", "pw", token=admin_token, role="user")
    assert resp.status_code == 400
    assert "'user' is now 'engineer'" in resp.get_json()["msg"]


def test_renewal_rotates_and_a_reused_token_ends_the_session(client, admin_token, monkeypatch):
    first = client.post("/api/renew", headers=auth(admin_token))
    assert first.status_code == 200
    renewed = first.get_json()["access_token"]
    assert client.get("/api/whoami", headers=auth(renewed)).status_code == 200
    assert client.get("/api/whoami", headers=auth(admin_token)).status_code == 401
    assert client.get("/api/whoami", headers=auth(renewed)).status_code == 200

    monkeypatch.setattr(sessions, "ROTATION_GRACE_S", -1.0)
    assert client.get("/api/whoami", headers=auth(admin_token)).status_code == 401
    assert client.get("/api/whoami", headers=auth(renewed)).status_code == 401


def test_renewal_keeps_the_session_and_role(client, tokens):
    renewed = client.post("/api/renew", headers=auth(tokens["operator"])).get_json()["access_token"]
    assert decode_token(renewed)["sid"] == decode_token(tokens["operator"])["sid"]
    assert client.get("/api/start-plc", headers=auth(renewed)).status_code == 403


def _restart():
    """Drops every in-process handle on the database, as a runtime restart does."""
    db.session.remove()
    db.engine.dispose()


def test_signing_out_ends_only_that_session_and_survives_a_restart(client, admin_token):
    other = token_for(client, "admin", "admin-pass")
    assert client.post("/api/logout", headers=auth(admin_token)).status_code == 200
    _restart()
    assert client.get("/api/whoami", headers=auth(admin_token)).status_code == 401
    assert client.get("/api/whoami", headers=auth(other)).status_code == 200


@pytest.mark.parametrize("change", ["password", "role", "delete"])
def test_an_account_change_ends_every_session_of_the_account(client, tokens, change):
    second = token_for(client, "operator", "operator-pass")
    target = _user_id("operator")
    if change == "password":
        resp = client.put(
            f"/api/update-user/{target}", json={"password": "new"}, headers=auth(tokens["admin"])
        )
    elif change == "role":
        resp = client.put(
            f"/api/update-user/{target}", json={"role": "viewer"}, headers=auth(tokens["admin"])
        )
    else:
        resp = client.delete(f"/api/delete-user/{target}", headers=auth(tokens["admin"]))
    assert resp.status_code == 200
    _restart()
    for token in (tokens["operator"], second):
        assert client.get("/api/whoami", headers=auth(token)).status_code == 401
    assert client.get("/api/whoami", headers=auth(tokens["engineer"])).status_code == 200
    if change == "role":
        fresh = token_for(client, "operator", "operator-pass")
        assert client.get("/api/whoami", headers=auth(fresh)).get_json()["role"] == "viewer"


def test_failed_sign_ins_are_throttled_with_a_growing_delay(client, admin_token):
    assert login(client, "admin", "wrong").status_code == 401
    held = login(client, "admin", "admin-pass")
    assert held.status_code == 429
    assert 1 <= int(held.headers["Retry-After"]) <= 5


def test_throttle_delay_doubles_from_five_seconds_up_to_five_minutes():
    assert [sessions.throttle_delay(n) for n in range(1, 9)] == [5, 10, 20, 40, 80, 160, 300, 300]


def test_throttle_is_keyed_by_client_address_only(app):
    key = sessions.throttle_key("10.0.0.1")
    assert key == "addr:10.0.0.1"
    sessions.record_failure(key, now=1000.0)
    assert sessions.throttle_wait(key, now=1001.0) == 4
    assert sessions.throttle_wait(sessions.throttle_key("10.0.0.2"), now=1001.0) == 0
    assert sessions.throttle_wait(sessions.throttle_key(None), now=1001.0) == 0
    sessions.record_failure(key, now=1006.0)
    assert sessions.throttle_wait(key, now=1006.0) == 10
    sessions.record_failure(key, now=1006.0 + sessions.THROTTLE_FORGET_S + 1)
    assert (
        sessions.throttle_wait(key, now=1006.0 + sessions.THROTTLE_FORGET_S + 1)
        == sessions.THROTTLE_FIRST_DELAY_S
    )


def test_unknown_addresses_share_one_counter(app):
    assert sessions.throttle_key(None) == sessions.throttle_key("") == "addr:unknown"
    sessions.record_failure(sessions.throttle_key(None), now=1000.0)
    assert sessions.throttle_wait(sessions.throttle_key(""), now=1001.0) == 4
    assert sessions.throttle_wait(sessions.throttle_key("10.0.0.1"), now=1001.0) == 0


def _sign_in_from(client, username, password, address):
    return client.post(
        "/api/login",
        json={"username": username, "password": password},
        environ_base={"REMOTE_ADDR": address},
    )


def test_failures_for_many_usernames_from_one_address_are_throttled_together(
    client, admin_token, monkeypatch
):
    clock = [1_000_000.0]
    monkeypatch.setattr(sessions.time, "time", lambda: clock[0])
    for n, delay in enumerate([5, 10, 20, 40]):
        assert _sign_in_from(client, f"user{n}", "wrong", "10.6.6.6").status_code == 401
        held = _sign_in_from(client, f"next{n}", "wrong", "10.6.6.6")
        assert held.status_code == 429
        assert int(held.headers["Retry-After"]) == delay
        clock[0] += delay


def test_the_same_username_from_two_addresses_is_throttled_independently(
    client, admin_token, monkeypatch
):
    monkeypatch.setattr(sessions.time, "time", lambda: 1_000_000.0)
    assert _sign_in_from(client, "admin", "wrong", "10.0.0.1").status_code == 401
    assert _sign_in_from(client, "admin", "admin-pass", "10.0.0.1").status_code == 429
    assert _sign_in_from(client, "admin", "admin-pass", "10.0.0.2").status_code == 200
    assert _sign_in_from(client, "admin", "wrong", "10.0.0.2").status_code == 401
    for address in ("10.0.0.1", "10.0.0.2"):
        assert sessions.throttle_wait(sessions.throttle_key(address)) == 5


def test_a_successful_sign_in_keeps_the_address_counter(client, admin_token, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(sessions.time, "time", lambda: clock[0])
    assert _sign_in_from(client, "admin", "wrong", "10.0.0.1").status_code == 401
    clock[0] += 5
    assert _sign_in_from(client, "admin", "admin-pass", "10.0.0.1").status_code == 200
    assert _sign_in_from(client, "admin", "wrong", "10.0.0.1").status_code == 401
    assert sessions.throttle_wait(sessions.throttle_key("10.0.0.1")) == 10


def test_failures_from_one_address_never_delay_a_sign_in_from_another(
    client, admin_token, monkeypatch
):
    clock = [1_000_000.0]
    monkeypatch.setattr(sessions.time, "time", lambda: clock[0])
    attacker = {"REMOTE_ADDR": "10.6.6.6"}
    operator = {"REMOTE_ADDR": "10.0.0.5"}

    def sign_in(password, environ):
        return client.post(
            "/api/login",
            json={"username": "admin", "password": password},
            environ_base=environ,
        )

    end = clock[0] + 2 * 3600
    failures = 0
    while clock[0] < end:
        response = sign_in("wrong", attacker)
        assert response.status_code == 401
        failures += 1
        early = sign_in("admin-pass", attacker)
        assert early.status_code == 429
        assert sign_in("admin-pass", operator).status_code == 200
        clock[0] += int(early.headers["Retry-After"])
    assert failures >= 20
    assert int(early.headers["Retry-After"]) == sessions.THROTTLE_MAX_DELAY_S


def test_role_changes_session_ends_and_throttled_sign_ins_are_logged(client, tokens, caplog):
    caplog.set_level(logging.WARNING)
    client.put(
        f"/api/update-user/{_user_id('operator')}",
        json={"role": "viewer"},
        headers=auth(tokens["admin"]),
    )
    login(client, "engineer", "wrong")
    login(client, "engineer", "engineer-pass")
    client.get("/api/start-plc", headers=auth(tokens["viewer"]))
    text = caplog.text
    assert "Role of account operator changed from operator to viewer by admin" in text
    assert "Every session of account" in text
    assert "Throttled a sign-in for engineer" in text
    assert "Refused plc-command" in text or "Refused GET start-plc" in text
