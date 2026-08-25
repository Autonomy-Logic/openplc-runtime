"""Behavioural tests for the persistent-storage (RETAIN) settings endpoints.

These settings decide whether a device keeps its retained variables at all, so
the things worth pinning down are: that it is OFF until somebody turns it on,
that only an admin can turn it on, and that a setting the runtime could not
honour is refused at the API rather than discovered as a store that silently
never writes.
"""

import os

import pytest
from conftest import create_user, token_for

from webserver import retain_config


@pytest.fixture(autouse=True)
def isolated_conf(tmp_path, monkeypatch):
    """Point retain.conf at a temp file — never the developer's own runtime."""
    conf = tmp_path / "retain.conf"
    monkeypatch.setattr(retain_config, "RETAIN_CONF_PATH", conf)
    from webserver import restapi

    monkeypatch.setitem(restapi.app_restapi.config, "RUNTIME_MANAGER", None)
    return conf


@pytest.fixture()
def admin_headers(client):
    create_user(client, "admin", "admin-pass")
    return {"Authorization": f"Bearer {token_for(client, 'admin', 'admin-pass')}"}


@pytest.fixture()
def user_headers(client, admin_headers):
    create_user(client, "bob", "bob-pass", token=admin_headers["Authorization"].split()[1], role="user")
    return {"Authorization": f"Bearer {token_for(client, 'bob', 'bob-pass')}"}


# --- defaults -------------------------------------------------------------


def test_retention_is_off_until_somebody_turns_it_on(client, admin_headers):
    # The whole point of the default: a device does not start writing to its
    # data partition on a cadence nobody chose.
    body = client.get("/api/retain-config", headers=admin_headers).get_json()
    assert body["enabled"] is False
    assert body["path"] == retain_config.DEFAULT_RETAIN_PATH
    assert body["flushSeconds"] == retain_config.DEFAULT_FLUSH_SECONDS


def test_get_reports_the_bounds_the_ui_should_enforce(client, admin_headers):
    body = client.get("/api/retain-config", headers=admin_headers).get_json()
    assert body["minFlushSeconds"] == retain_config.MIN_FLUSH_SECONDS
    assert body["maxFlushSeconds"] == retain_config.MAX_FLUSH_SECONDS
    assert body["defaultPath"] == retain_config.DEFAULT_RETAIN_PATH


def test_get_requires_authentication(client):
    assert client.get("/api/retain-config").status_code == 401


# --- who may change it ----------------------------------------------------


def test_a_plain_user_may_read_but_not_change(client, user_headers):
    assert client.get("/api/retain-config", headers=user_headers).status_code == 200
    resp = client.put("/api/retain-config", json={"enabled": True}, headers=user_headers)
    assert resp.status_code == 403


def test_put_requires_authentication(client):
    assert client.put("/api/retain-config", json={"enabled": True}).status_code == 401


# --- saving ---------------------------------------------------------------


def test_admin_enables_it_and_it_persists(client, admin_headers, tmp_path):
    target = str(tmp_path / "retain.bin")
    resp = client.put(
        "/api/retain-config",
        json={"enabled": True, "path": target, "flushSeconds": 30},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    body = client.get("/api/retain-config", headers=admin_headers).get_json()
    assert body["enabled"] is True
    assert body["path"] == target
    assert body["flushSeconds"] == 30


def test_the_file_is_written_in_the_form_the_core_parses(client, admin_headers, tmp_path, isolated_conf):
    target = str(tmp_path / "retain.bin")
    client.put(
        "/api/retain-config",
        json={"enabled": True, "path": target, "flushSeconds": 7},
        headers=admin_headers,
    )
    text = isolated_conf.read_text()
    # Flat key=value, because the core parses this in C++ at startup.
    assert "enabled=1" in text
    assert f"path={target}" in text
    assert "flush_seconds=7" in text


def test_a_partial_update_leaves_the_other_settings_alone(client, admin_headers, tmp_path):
    target = str(tmp_path / "retain.bin")
    client.put(
        "/api/retain-config",
        json={"enabled": True, "path": target, "flushSeconds": 30},
        headers=admin_headers,
    )
    client.put("/api/retain-config", json={"enabled": False}, headers=admin_headers)
    body = client.get("/api/retain-config", headers=admin_headers).get_json()
    assert body["enabled"] is False
    assert body["path"] == target
    assert body["flushSeconds"] == 30


# --- settings the runtime could not honour --------------------------------


def test_a_relative_path_is_refused(client, admin_headers):
    resp = client.put(
        "/api/retain-config", json={"enabled": True, "path": "retain.bin"}, headers=admin_headers
    )
    assert resp.status_code == 400
    assert "absolute" in resp.get_json()["msg"]


def test_a_missing_directory_is_refused(client, admin_headers):
    resp = client.put(
        "/api/retain-config",
        json={"enabled": True, "path": "/definitely/not/here/retain.bin"},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert "does not exist" in resp.get_json()["msg"]


def test_a_directory_is_refused_as_a_target(client, admin_headers, tmp_path):
    resp = client.put(
        "/api/retain-config", json={"enabled": True, "path": str(tmp_path)}, headers=admin_headers
    )
    assert resp.status_code == 400


def test_an_unnormalised_path_is_refused(client, admin_headers, tmp_path):
    # Not a security boundary — an admin here can already run code — but a
    # traversal makes the stored location unobvious, which is worth refusing.
    resp = client.put(
        "/api/retain-config",
        json={"enabled": True, "path": f"{tmp_path}/../retain.bin"},
        headers=admin_headers,
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("seconds", [0, -1, retain_config.MAX_FLUSH_SECONDS + 1, "soon", None])
def test_an_impossible_flush_period_is_refused(client, admin_headers, tmp_path, seconds):
    resp = client.put(
        "/api/retain-config",
        json={"enabled": True, "path": str(tmp_path / "r.bin"), "flushSeconds": seconds},
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_a_non_boolean_enabled_is_refused(client, admin_headers):
    resp = client.put("/api/retain-config", json={"enabled": "yes"}, headers=admin_headers)
    assert resp.status_code == 400


def test_enabling_without_ever_setting_a_path_uses_the_default(client, admin_headers, monkeypatch, tmp_path):
    # The default has to be usable as-is, or "enable" fails for no good reason
    # the first time anyone presses it.
    monkeypatch.setattr(retain_config, "DEFAULT_RETAIN_PATH", str(tmp_path / "retain.bin"))
    resp = client.put("/api/retain-config", json={"enabled": True}, headers=admin_headers)
    assert resp.status_code == 200, resp.get_json()


# --- reading a file written by hand ---------------------------------------


def test_comments_and_blank_lines_are_ignored(client, admin_headers, isolated_conf, tmp_path):
    isolated_conf.write_text(
        "# hand-edited\n\nenabled=true\n"
        f"path={tmp_path / 'x.bin'}\n"
        "flush_seconds=12\n"
    )
    body = client.get("/api/retain-config", headers=admin_headers).get_json()
    assert body["enabled"] is True
    assert body["flushSeconds"] == 12


def test_a_corrupt_flush_value_falls_back_to_the_default(client, admin_headers, isolated_conf):
    isolated_conf.write_text("enabled=0\nflush_seconds=banana\n")
    body = client.get("/api/retain-config", headers=admin_headers).get_json()
    assert body["flushSeconds"] == retain_config.DEFAULT_FLUSH_SECONDS
