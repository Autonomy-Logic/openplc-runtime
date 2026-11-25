# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Gaps in the security envelope that a review found: concurrent sign-ins, reused account ids,
purged sessions under open debug sockets, sockets after a renewal, and password guessing through
the current-password check."""

import threading
import time

import pytest
from conftest import auth, create_user, login, token_for
from flask_jwt_extended import decode_token
from sqlalchemy import text as sa_text

from webserver import debug_websocket as dws
from webserver import sessions
from webserver.restapi import User, apply_user_schema_migrations, db

NS = "/api/debug"


class _Runtime:
    """A runtime socket that answers every debug command."""

    def is_connected(self):
        return True

    def send_and_receive(self, _command, timeout=None):
        return "DEBUG:41 00 01"


@pytest.fixture()
def sio(app):
    dws._session_tokens.clear()
    yield dws.init_debug_websocket(app, _Runtime())
    dws._session_tokens.clear()


def _command(ws):
    ws.get_received(NS)
    ws.emit("debug_command", {"command": "41 00 00"}, namespace=NS)
    return ws.get_received(NS)[-1]["args"][0]


def _later(monkeypatch, seconds):
    real = time.time
    monkeypatch.setattr(sessions.time, "time", lambda: real() + seconds)


def test_concurrent_wrong_passwords_are_held_one_at_a_time(app, admin_token, monkeypatch):
    check = User.check_password

    def slow(self, password):
        time.sleep(0.3)
        return check(self, password)

    monkeypatch.setattr(User, "check_password", slow)
    codes = []
    lock = threading.Lock()

    def attempt(i):
        response = app.test_client().post(
            "/api/login", json={"username": "admin", "password": f"bad{i}"}
        )
        with lock:
            codes.append(response.status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(codes) == [401] + [429] * 5


def test_account_ids_are_never_reused(client, admin_token):
    create_user(client, "eng", "p", token=admin_token, role="engineer")
    old = User.query.filter_by(username="eng").one().id
    stale = token_for(client, "eng", "p")
    assert client.delete(f"/api/delete-user/{old}", headers=auth(admin_token)).status_code == 200
    create_user(client, "other", "p", token=admin_token, role="admin")
    assert User.query.filter_by(username="other").one().id > old
    assert client.get("/api/whoami", headers=auth(stale)).status_code == 401


def test_the_migration_rebuilds_a_users_table_that_reuses_ids(app):
    db.session.execute(sa_text("DROP TABLE users"))
    db.session.execute(
        sa_text(
            "CREATE TABLE users (id INTEGER NOT NULL, username TEXT NOT NULL,"
            " password_hash TEXT NOT NULL, role VARCHAR(20), PRIMARY KEY (id), UNIQUE (username))"
        )
    )
    db.session.execute(
        sa_text(
            "INSERT INTO users (id, username, password_hash, role)"
            " VALUES (1, 'a', 'x', 'admin'), (2, 'b', 'y', 'user')"
        )
    )
    db.session.commit()
    apply_user_schema_migrations()
    sql = db.session.execute(
        sa_text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'")
    ).scalar()
    assert "AUTOINCREMENT" in sql.upper()
    rows = db.session.execute(sa_text("SELECT id, username, role FROM users ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [(1, "a", "admin"), (2, "b", "engineer")]
    db.session.execute(sa_text("DELETE FROM users WHERE id = 2"))
    db.session.execute(
        sa_text("INSERT INTO users (username, password_hash, role) VALUES ('c', 'z', 'viewer')")
    )
    db.session.commit()
    assert db.session.execute(sa_text("SELECT id FROM users WHERE username = 'c'")).scalar() == 3


def test_a_purge_keeps_the_session_of_an_open_debug_socket(sio, app, admin_token, monkeypatch):
    ws = sio.test_client(app, namespace=NS, auth={"token": admin_token})
    assert _command(ws)["success"] is True
    _later(monkeypatch, 16 * 60)
    sessions.purge()
    assert _command(ws)["success"] is True


def test_a_purge_drops_ended_and_day_idle_sessions(app, admin_token):
    sid = decode_token(admin_token)["sid"]
    sessions.end_session(sid, "signed out")
    now = time.time() + sessions.TOKEN_LIFETIME.total_seconds() + 1
    sessions.purge(now)
    assert db.session.get(sessions.AuthSession, sid) is None
    idle = sessions.start_session(1, sessions.now_ms())["sid"]
    sessions.purge(now + sessions.IDLE_SESSION_PURGE_S)
    assert db.session.get(sessions.AuthSession, idle) is None


def test_an_open_debug_socket_follows_a_renewal(sio, app, client, admin_token, monkeypatch):
    ws = sio.test_client(app, namespace=NS, auth={"token": admin_token})
    renewed = client.post("/api/renew", headers=auth(admin_token)).get_json()["access_token"]
    _later(monkeypatch, sessions.ROTATION_GRACE_S + 1)
    assert _command(ws)["success"] is True
    assert client.get("/api/whoami", headers=auth(renewed)).status_code == 200


def test_wrong_current_passwords_are_throttled(client, admin_token):
    create_user(client, "op", "op-pass", token=admin_token, role="operator")
    token = token_for(client, "op", "op-pass")
    me = User.query.filter_by(username="op").one().id
    body = {"password": "new", "current_password": "guess"}
    assert client.put(f"/api/update-user/{me}", json=body, headers=auth(token)).status_code == 403
    held = client.put(f"/api/update-user/{me}", json=body, headers=auth(token))
    assert held.status_code == 429
    assert 1 <= int(held.headers["Retry-After"]) <= 5


def test_a_token_renewed_twice_gives_one_successor(app, admin_token):
    payload = decode_token(admin_token)
    assert sessions.rotate(payload) is not None
    assert sessions.rotate(payload) is None


def test_a_sign_in_body_that_is_not_an_object_is_a_bad_request(client, admin_token):
    assert client.post("/api/login", json=["admin", "admin-pass"]).status_code == 400


def test_a_sign_in_that_began_before_a_password_change_does_not_survive_it(app, admin_token):
    began = sessions.now_ms()
    admin = User.query.filter_by(username="admin").one()
    admin.sessions_valid_after_ms = sessions.end_account_sessions(admin.id, "password changed")
    db.session.commit()
    claims = sessions.start_session(admin.id, began)
    payload = {"jti": "late", **claims}
    assert sessions.check_token(payload, admin.sessions_valid_after_ms) is not None


def test_a_renewal_purges_expired_revocations(app, client, admin_token):
    db.session.add(
        sessions.RevokedToken(
            jti="old", sid="gone", revoked_at=1.0, expires_at=2.0, reason="rotated"
        )
    )
    db.session.commit()
    assert client.post("/api/renew", headers=auth(admin_token)).status_code == 200
    assert db.session.get(sessions.RevokedToken, "old") is None


def test_sign_in_still_works_after_these_changes(client, admin_token):
    assert login(client, "admin", "admin-pass").status_code == 200
