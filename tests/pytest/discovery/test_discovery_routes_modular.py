# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Tests for the modular-coupler discovery routes.

Covers:
  * ``POST /api/discovery/ethercat/scan-modules`` — modular coupler module ident scan.
  * ``POST /api/discovery/ethercat/diagnostics`` — per-slave coupler + module diagnostics.
  * ``GET  /api/discovery/ethercat/diagnostics`` — master-wide diagnostics.
  * ``GET  /api/discovery/ethercat/capabilities`` — EtherDOG features captured on hello.

Each test spins up a lightweight Flask app with the discovery blueprint registered and
plants a fake EtherDOG manager in ``current_app.config``. The real EtherDOG
unix-socket transport is covered by ``test_etherdog_manager.py``; here we focus on
request parsing, response reshaping, and failure translation.
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from webserver.discovery.discovery_routes import discovery_bp

_STATUS: dict[str, object] = {
    "configured": True,
    "masters": [{"name": "ethercat0"}, {"name": "ethercat1"}],
}


class _FakeManager:
    """Mimics ``EtherDogManager`` for route tests.

    ``plugin_style_command`` returns a canned reply per command, ``get_last_hello``
    returns a frozen (features, version) pair. Every call is recorded in
    ``self.calls`` so a test can assert the request shape it produced.
    """

    def __init__(
        self,
        *,
        reply_by_command: dict[str, dict[str, object]] | None = None,
        hello: tuple[list[str] | None, str | None] = (None, None),
    ) -> None:
        self.reply_by_command = {"status": _STATUS, **(reply_by_command or {})}
        self._hello = hello
        self.calls: list[dict[str, object]] = []
        self.timeouts: list[float] = []

    def plugin_style_command(self, request: dict[str, object], timeout: float) -> dict[str, object]:
        self.calls.append(request)
        self.timeouts.append(timeout)
        cmd = request.get("command")
        if cmd in self.reply_by_command:
            return self.reply_by_command[cmd]
        return {"error": f"no reply configured for {cmd}"}

    def get_last_hello(self) -> tuple[list[str] | None, str | None]:
        return self._hello


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["JWT_SECRET_KEY"] = "test-secret"
    JWTManager(app)
    app.register_blueprint(discovery_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _auth(app) -> dict[str, str]:
    with app.app_context():
        token = create_access_token(identity="tester")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# scan-modules
# ---------------------------------------------------------------------------


def test_scan_modules_translates_idents_to_slot_indexed_modules(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={
            "scan-modules": {
                "status": "success",
                "master": 0,
                "slave": 1,
                "idents": ["0x00001A0F", "0x00001A10"],
            }
        }
    )
    resp = client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"bus_name": "ethercat0", "slave_position": 1},
        headers=_auth(app),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["bus_name"] == "ethercat0"
    assert body["slave_position"] == 1
    assert body["modules"] == [
        {"slot": 1, "ident": "0x00001A0F"},
        {"slot": 2, "ident": "0x00001A10"},
    ]
    mgr = app.config["ETHERDOG_MANAGER"]
    assert mgr.calls == [
        {"command": "status"},
        {"command": "scan-modules", "params": {"master": 0, "slave": 1}},
    ]


def test_scan_modules_rejects_missing_bus_name(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"slave_position": 1},
        headers=_auth(app),
    )
    assert resp.status_code == 400
    assert "bus_name" in resp.get_json()["message"]


def test_scan_modules_rejects_non_integer_slave_position(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"bus_name": "ethercat0", "slave_position": "1"},
        headers=_auth(app),
    )
    assert resp.status_code == 400
    assert "slave_position" in resp.get_json()["message"]


def test_scan_modules_rejects_boolean_slave_position(app, client):
    # bool is a subclass of int in Python; the handler must still reject it.
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"bus_name": "ethercat0", "slave_position": True},
        headers=_auth(app),
    )
    assert resp.status_code == 400


def test_scan_modules_forwards_etherdog_error_as_502(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"scan-modules": {"error": "EtherDOG is not reachable"}}
    )
    resp = client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"bus_name": "ethercat0", "slave_position": 1},
        headers=_auth(app),
    )
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["status"] == "error"
    assert body["message"] == "EtherDOG is not reachable"
    assert body["modules"] == []


def test_scan_modules_forwards_etherdog_failure_status_as_502(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"scan-modules": {"status": "error", "message": "slave 7 unreachable"}}
    )
    resp = client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"bus_name": "ethercat0", "slave_position": 7},
        headers=_auth(app),
    )
    assert resp.status_code == 502
    assert resp.get_json()["message"] == "slave 7 unreachable"


def test_scan_modules_requires_auth(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"bus_name": "ethercat0", "slave_position": 1},
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["scan-modules", "diagnostics"])
def test_bus_name_selects_the_etherdog_master_by_name(app, client, path):
    command = "scan-modules" if path == "scan-modules" else "diagnostics-slave"
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={command: {"status": "success", "idents": [], "modules": []}}
    )
    resp = client.post(
        f"/api/discovery/ethercat/{path}",
        json={"bus_name": "ethercat1", "slave_position": 3},
        headers=_auth(app),
    )
    assert resp.status_code == 200
    assert app.config["ETHERDOG_MANAGER"].calls[-1] == {
        "command": command,
        "params": {"master": 1, "slave": 3},
    }


@pytest.mark.parametrize("path", ["scan-modules", "diagnostics"])
def test_unknown_bus_name_is_404_without_a_bus_command(app, client, path):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.post(
        f"/api/discovery/ethercat/{path}",
        json={"bus_name": "missing", "slave_position": 1},
        headers=_auth(app),
    )
    assert resp.status_code == 404
    assert "missing" in resp.get_json()["message"]
    assert app.config["ETHERDOG_MANAGER"].calls == [{"command": "status"}]


def test_scan_of_a_bus_not_loaded_goes_through_its_interface(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"scan-modules": {"status": "success", "idents": ["0x00091F84"]}}
    )
    resp = client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"bus_name": "new_bus", "slave_position": 2, "interface": "eth1"},
        headers=_auth(app),
    )
    assert resp.status_code == 200
    assert resp.get_json()["modules"] == [{"slot": 1, "ident": "0x00091F84"}]
    assert app.config["ETHERDOG_MANAGER"].calls[-1] == {
        "command": "scan-modules",
        "params": {"interface": "eth1", "slave": 2},
    }


def test_a_loaded_bus_is_scanned_by_master_even_with_an_interface(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"scan-modules": {"status": "success", "idents": []}}
    )
    client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"bus_name": "ethercat1", "slave_position": 1, "interface": "eth1"},
        headers=_auth(app),
    )
    assert app.config["ETHERDOG_MANAGER"].calls[-1]["params"] == {"master": 1, "slave": 1}


@pytest.mark.parametrize("interface", [5, "", "eth0; rm -rf /"])
def test_scan_refuses_an_invalid_interface(app, client, interface):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"bus_name": "new_bus", "slave_position": 1, "interface": interface},
        headers=_auth(app),
    )
    assert resp.status_code == 400
    assert app.config["ETHERDOG_MANAGER"].calls == []


def test_scan_of_a_bus_not_loaded_still_502s_when_etherdog_is_down(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(reply_by_command={"status": {"error": "down"}})
    resp = client.post(
        "/api/discovery/ethercat/scan-modules",
        json={"bus_name": "new_bus", "slave_position": 1, "interface": "eth1"},
        headers=_auth(app),
    )
    assert resp.status_code == 502


@pytest.mark.parametrize("path", ["scan-modules", "diagnostics"])
def test_etherdog_status_failure_is_502(app, client, path):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"status": {"error": "EtherDOG not connected"}}
    )
    resp = client.post(
        f"/api/discovery/ethercat/{path}",
        json={"bus_name": "ethercat0", "slave_position": 1},
        headers=_auth(app),
    )
    assert resp.status_code == 502
    assert "not connected" in resp.get_json()["message"]


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


def test_diagnostics_success_forwards_slave_and_shapes_response(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={
            "diagnostics-slave": {
                "status": "success",
                "master": 0,
                "slave": 1,
                "coupler": {"status_word": 0, "poll_at_ms": 1700000000000.0},
                "modules": [
                    {"slot": 1, "status_byte": 0x00},
                    {"slot": 2, "status_byte": 0x00},
                ],
            }
        }
    )
    resp = client.post(
        "/api/discovery/ethercat/diagnostics",
        json={"bus_name": "ethercat0", "slave_position": 1},
        headers=_auth(app),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["bus_name"] == "ethercat0"
    assert body["slave_position"] == 1
    assert body["coupler"]["status_word"] == 0
    assert body["coupler"]["poll_at_ms"] == 1700000000000.0
    assert body["modules"] == [
        {"slot": 1, "status_byte": 0},
        {"slot": 2, "status_byte": 0},
    ]
    mgr = app.config["ETHERDOG_MANAGER"]
    assert mgr.calls == [
        {"command": "status"},
        {"command": "diagnostics-slave", "params": {"master": 0, "slave": 1}},
    ]


def test_diagnostics_drops_poll_at_ms_when_absent(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={
            "diagnostics-slave": {
                "status": "success",
                "coupler": {"status_word": 0xC0},
                "modules": [],
            }
        }
    )
    resp = client.post(
        "/api/discovery/ethercat/diagnostics",
        json={"bus_name": "ethercat0", "slave_position": 1},
        headers=_auth(app),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["coupler"] == {"status_word": 0xC0}  # no poll_at_ms
    assert body["modules"] == []


def test_diagnostics_rejects_missing_params(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.post("/api/discovery/ethercat/diagnostics", json={}, headers=_auth(app))
    assert resp.status_code == 400


def test_diagnostics_forwards_etherdog_error(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"diagnostics-slave": {"error": "EtherDOG unreachable"}}
    )
    resp = client.post(
        "/api/discovery/ethercat/diagnostics",
        json={"bus_name": "ethercat0", "slave_position": 1},
        headers=_auth(app),
    )
    assert resp.status_code == 502
    assert "unreachable" in resp.get_json()["message"]


def test_diagnostics_requires_auth(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.post(
        "/api/discovery/ethercat/diagnostics",
        json={"bus_name": "ethercat0", "slave_position": 1},
    )
    assert resp.status_code == 401


def test_diagnostics_passes_unknown_values_through_as_null(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={
            "diagnostics-slave": {
                "status": "success",
                "coupler": {"status_word": None},
                "modules": [{"slot": 1, "status_byte": None}, {"slot": 2, "status_byte": 0x20}],
            }
        }
    )
    resp = client.post(
        "/api/discovery/ethercat/diagnostics",
        json={"bus_name": "ethercat0", "slave_position": 1},
        headers=_auth(app),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["coupler"] == {"status_word": None}
    assert body["modules"] == [{"slot": 1, "status_byte": None}, {"slot": 2, "status_byte": 0x20}]


@pytest.mark.parametrize("status_word", ["0x00C0", -1, 0x10000, True, 1.5])
def test_diagnostics_refuses_an_invalid_status_word(app, client, status_word):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={
            "diagnostics-slave": {
                "status": "success",
                "coupler": {"status_word": status_word},
                "modules": [],
            }
        }
    )
    resp = client.post(
        "/api/discovery/ethercat/diagnostics",
        json={"bus_name": "ethercat0", "slave_position": 1},
        headers=_auth(app),
    )
    assert resp.status_code == 502
    assert "status word" in resp.get_json()["message"]


def test_diagnostics_drops_modules_with_slot_0_or_bad_status(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={
            "diagnostics-slave": {
                "status": "success",
                "coupler": {"status_word": 0},
                "modules": [
                    {"slot": 0, "status_byte": 0},
                    {"slot": 1, "status_byte": 256},
                    {"slot": 2, "status_byte": "0"},
                    {"slot": 3, "status_byte": 1},
                ],
            }
        }
    )
    resp = client.post(
        "/api/discovery/ethercat/diagnostics",
        json={"bus_name": "ethercat0", "slave_position": 1},
        headers=_auth(app),
    )
    assert resp.get_json()["modules"] == [{"slot": 3, "status_byte": 1}]


def test_master_diagnostics_get_is_served_on_the_diagnostics_path(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"diagnostics": {"masters": [{"name": "m", "state": "OPERATIONAL"}]}}
    )
    resp = client.get("/api/discovery/ethercat/diagnostics", headers=_auth(app))
    assert resp.status_code == 200
    assert resp.get_json()["masters"][0]["plugin_state"] == "OPERATIONAL"
    assert app.config["ETHERDOG_MANAGER"].calls == [{"command": "diagnostics"}]


def test_master_diagnostics_get_reports_etherdog_unavailable(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.get("/api/discovery/ethercat/diagnostics", headers=_auth(app))
    assert resp.status_code == 503


def test_unreleased_master_diagnostics_path_is_gone(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.get("/api/discovery/ethercat/master-diagnostics", headers=_auth(app))
    assert resp.status_code == 404


@pytest.mark.parametrize("path", ["scan-modules", "diagnostics"])
@pytest.mark.parametrize("position", [0, -1, 65536])
def test_slave_position_outside_1_to_65535_is_refused(app, client, path, position):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.post(
        f"/api/discovery/ethercat/{path}",
        json={"bus_name": "ethercat0", "slave_position": position},
        headers=_auth(app),
    )
    assert resp.status_code == 400
    assert "1 to 65535" in resp.get_json()["message"]
    assert app.config["ETHERDOG_MANAGER"].calls == []


@pytest.mark.parametrize(
    ("timeout_ms", "etherdog_timeout"),
    [
        (None, 7.0),
        (10, 2.1),
        (3000, 5.0),
        (10**9, 62.0),
        ("5000", 7.0),
        (True, 7.0),
        (float("nan"), 7.0),
        (float("inf"), 7.0),
    ],
)
def test_scan_modules_timeout_is_clamped(app, client, timeout_ms, etherdog_timeout):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"scan-modules": {"status": "success", "idents": []}}
    )
    body: dict[str, object] = {"bus_name": "ethercat0", "slave_position": 1}
    if timeout_ms is not None:
        body["timeout_ms"] = timeout_ms
    resp = client.post("/api/discovery/ethercat/scan-modules", json=body, headers=_auth(app))
    assert resp.status_code == 200
    assert app.config["ETHERDOG_MANAGER"].timeouts[-1] == pytest.approx(etherdog_timeout)


def test_non_object_body_is_refused(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.post("/api/discovery/ethercat/scan-modules", json=[1, 2], headers=_auth(app))
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


def test_capabilities_returns_cached_hello_fields(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"status": {"status": "success"}},
        hello=(
            [
                "ethercat.dynamic_alloc",
                "ethercat.sdo_byte_string",
                "ethercat.sdo_complete_access",
                "ethercat.scan_modules",
            ],
            "0.5.0",
        ),
    )
    resp = client.get("/api/discovery/ethercat/capabilities", headers=_auth(app))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is True
    assert body["features"] == [
        "ethercat.dynamic_alloc",
        "ethercat.sdo_byte_string",
        "ethercat.sdo_complete_access",
        "ethercat.scan_modules",
    ]
    assert body["version"] == "0.5.0"


def test_capabilities_reports_empty_for_an_etherdog_without_features(app, client):
    # An older EtherDOG answers hello without a `features` array; the manager caches
    # that as (None, None) and the endpoint must still answer available=True.
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"status": {"status": "success"}},
        hello=(None, None),
    )
    resp = client.get("/api/discovery/ethercat/capabilities", headers=_auth(app))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is True
    assert body["features"] == []
    assert body["version"] is None


def test_capabilities_reports_unavailable_when_etherdog_unreachable(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager(
        reply_by_command={"status": {"error": "EtherDOG is not reachable"}},
    )
    resp = client.get("/api/discovery/ethercat/capabilities", headers=_auth(app))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is False
    assert body["features"] == []
    assert "not reachable" in body["message"]


def test_capabilities_requires_auth(app, client):
    app.config["ETHERDOG_MANAGER"] = _FakeManager()
    resp = client.get("/api/discovery/ethercat/capabilities")
    assert resp.status_code == 401
