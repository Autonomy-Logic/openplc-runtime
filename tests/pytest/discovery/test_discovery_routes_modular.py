# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Tests for the modular-coupler discovery routes.

Covers:
  * ``POST /api/discovery/ethercat/scan-modules`` — modular coupler module ident scan.
  * ``POST /api/discovery/ethercat/diagnostics`` — per-slave coupler + module diagnostics.
  * ``GET  /api/discovery/ethercat/capabilities`` — EtherDOG features captured on hello.

Each test spins up a lightweight Flask app with the discovery blueprint registered and
plants a fake EtherDOG manager in ``current_app.config``. The real EtherDOG
unix-socket transport is covered by ``test_etherdog_manager.py``; here we focus on
request parsing, response reshaping, and failure translation.
"""

from __future__ import annotations

from typing import Any

import pytest
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from webserver.discovery.discovery_routes import discovery_bp


class _FakeManager:
    """Mimics ``EtherDogManager`` for route tests.

    ``plugin_style_command`` returns a canned reply per command, ``get_last_hello``
    returns a frozen (features, version) pair. Every call is recorded in
    ``self.calls`` so a test can assert the request shape it produced.
    """

    def __init__(
        self,
        *,
        reply_by_command: dict[str, dict[str, Any]] | None = None,
        hello: tuple[list[str] | None, str | None] = (None, None),
    ) -> None:
        self.reply_by_command = reply_by_command or {}
        self._hello = hello
        self.calls: list[dict[str, Any]] = []

    def plugin_style_command(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.calls.append(request)
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
        {"slot": 0, "ident": "0x00001A0F"},
        {"slot": 1, "ident": "0x00001A10"},
    ]
    mgr = app.config["ETHERDOG_MANAGER"]
    assert mgr.calls == [
        {"command": "scan-modules", "params": {"master": 0, "slave": 1}}
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
        reply_by_command={
            "scan-modules": {"status": "error", "message": "slave 7 unreachable"}
        }
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
                    {"slot": 0, "status_byte": 0x00},
                    {"slot": 1, "status_byte": 0x00},
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
        {"slot": 0, "status_byte": 0},
        {"slot": 1, "status_byte": 0},
    ]
    mgr = app.config["ETHERDOG_MANAGER"]
    assert mgr.calls == [
        {"command": "diagnostics-slave", "params": {"master": 0, "slave": 1}}
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


def test_capabilities_reports_empty_for_pre_r4_etherdog(app, client):
    # EtherDOG < R4 answers hello without a `features` array; the manager caches
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
