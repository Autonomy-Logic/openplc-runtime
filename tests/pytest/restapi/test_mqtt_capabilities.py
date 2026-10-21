# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""GET /api/mqtt/capabilities: authenticated, and exactly the shared contract shape."""

import json
from pathlib import Path

import pytest
from conftest import auth

from webserver import mqtt_config, restapi
from webserver.mqtt_api import mqtt_bp

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "core/src/drivers/plugins/python/mqtt/contract/fixtures/api/capabilities.json"
)

if "mqtt_blueprint" not in restapi.app_restapi.blueprints:
    restapi.app_restapi.register_blueprint(mqtt_bp, url_prefix="/api/mqtt")


def test_requires_a_token(client) -> None:
    assert client.get("/api/mqtt/capabilities").status_code == 401


def test_rejects_a_forged_token(client) -> None:
    resp = client.get("/api/mqtt/capabilities", headers=auth("not.a.jwt"))
    assert resp.status_code in (401, 422)


def test_reports_exactly_the_contract_with_the_vault_available(client, admin_token) -> None:
    resp = client.get("/api/mqtt/capabilities", headers=auth(admin_token))
    assert resp.status_code == 200
    assert resp.get_json() == json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_reports_no_secret_methods_without_the_pinned_cryptography(
    client, admin_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mqtt_config.material, "crypto_available", lambda: False)
    resp = client.get("/api/mqtt/capabilities", headers=auth(admin_token))
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expected["credentials"]["methods"] = []
    assert resp.get_json() == expected


def test_the_unauthenticated_capabilities_stay_unchanged(client) -> None:
    body = client.get("/api/capabilities").get_json()
    assert "mqtt" not in json.dumps(body).lower()
