# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""GET /api/mqtt/status and credential revocation through the real REST API.

The plugin side is the real ``mqtt_plugin`` module with its status channel;
only the MQTT transport is faked. Every request carries a real JWT.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conftest import auth

from webserver import app as app_module
from webserver import mqtt_api, mqtt_config, plcapp_management, restapi
from webserver.mqtt_api import mqtt_bp

RUNTIME_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RUNTIME_ROOT / "tests/pytest/plugins/mqtt"))

# isort: split
# pylint: disable=wrong-import-position,wrong-import-order
import mqtt_plugin
from api_schema import check
from contract.runtime_config import MqttRuntimeConfig
from mqtt_credentials.vault import CredentialVault
from mqtt_publisher.status import StatusWriter
from mqtt_publisher.status_channel import StatusServer
from program_fakes import FakeProgram
from publisher_fakes import FakeNetwork, FakeSession, RealTime, RecordingStrategy

# pylint: enable=wrong-import-position,wrong-import-order

CONFIG_DIR = RUNTIME_ROOT / "core/src/drivers/plugins/python/mqtt/contract/fixtures/runtime-config"
GENERIC = CONFIG_DIR / "valid" / "generic-on-change-anonymous-plaintext.json"
DEBUG_MAP = CONFIG_DIR / "debug-map.json"
MD5 = "3f2c9d0e8b7a6f5e4d3c2b1a09f8e7d6"
PASSWORD = b"MARKER-PW-status-api-7d2b"
ACTIVE = Path("core/src/drivers/plugins/python/mqtt/mqtt.json")
SOCKET = Path("core/src/drivers/plugins/python/mqtt/mqtt_status.socket")

if "mqtt_blueprint" not in restapi.app_restapi.blueprints:
    restapi.app_restapi.register_blueprint(mqtt_bp, url_prefix="/api/mqtt")


class _FakeRuntime:
    def __init__(self) -> None:
        self.state = "STOPPED"

    def status_plc(self) -> str:
        return f"STATUS:{self.state}\n"

    def stop_plc(self) -> str:
        self.state = "STOPPED"
        return "OK\n"

    def reset_crash_tracking(self) -> None:
        pass


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def wait(self) -> int:
        return 0


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class Harness:
    def __init__(self) -> None:
        self.network = FakeNetwork(RealTime())
        self.passwords: list[bytes | None] = []

    def strategy(self, config: MqttRuntimeConfig, _writer: StatusWriter) -> RecordingStrategy:
        assert config.generic is not None
        return RecordingStrategy(interval_ms=config.generic.interval_ms)

    def transport(self, spec) -> FakeSession:  # type: ignore[no-untyped-def]
        self.passwords.append(spec.password)
        return self.network.factory(spec)

    def factory(self) -> mqtt_plugin.PublisherFactory:
        builders: dict[str, mqtt_plugin.StrategyBuilder] = {"generic": self.strategy}
        return mqtt_plugin.PublisherFactory(builders, self.transport, log=lambda *_a: None)


@pytest.fixture
def runtime() -> _FakeRuntime:
    return _FakeRuntime()


@pytest.fixture
def device(
    client, monkeypatch: pytest.MonkeyPatch, runtime: _FakeRuntime
) -> Iterator[CredentialVault]:
    """A runtime working directory short enough for its Unix sockets, wired as in production."""
    base = Path(tempfile.mkdtemp(prefix="mqs", dir="/tmp"))
    device_dir = base / "d"
    device_dir.mkdir()
    monkeypatch.chdir(device_dir)
    shutil.copy(RUNTIME_ROOT / "plugins_default.conf", "plugins_default.conf")
    shutil.copy(RUNTIME_ROOT / "plugins_default.conf", "plugins.conf")
    monkeypatch.setattr(app_module, "runtime_manager", runtime)
    monkeypatch.setattr(app_module, "apply_vpp_plugin_conf", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "apply_retain_conf", lambda *a, **k: None)
    monkeypatch.setattr(app_module.etherdog_manager, "apply_busconfig", lambda _path: None)
    monkeypatch.setattr(app_module, "run_compile", lambda *a, **k: None)
    monkeypatch.setattr(app_module.build_state, "status", app_module.BuildStatus.SUCCESS)
    monkeypatch.setattr(restapi, "_handler_callback_post", app_module.restapi_callback_post)
    store = CredentialVault(base / "persist" / "mqtt", base / "persist" / "keys")
    monkeypatch.setattr(mqtt_config, "_vault", store)
    monkeypatch.setattr(mqtt_config, "_claim", None)
    monkeypatch.setattr(mqtt_config, "_plugin_python", lambda _plugin: sys.executable)
    monkeypatch.setattr(mqtt_api, "_plc_status", runtime.status_plc)
    yield store
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def plugin(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[mqtt_plugin.PluginState, Harness]]:
    harness = Harness()
    state = mqtt_plugin.PluginState(publishers=harness.factory())
    state.config_path = str(ACTIVE)
    monkeypatch.setattr(mqtt_plugin, "_state", state)
    monkeypatch.setattr(mqtt_plugin, "_logging", None)
    monkeypatch.setattr(mqtt_plugin, "_program", FakeProgram.from_debug_map())
    monkeypatch.setattr(mqtt_plugin, "_channel", None)
    yield state, harness
    mqtt_plugin.stop_loop()


@pytest.fixture
def audit_lines() -> Iterator[list[str]]:
    handler = _ListHandler()
    target = logging.getLogger("webserver.mqtt_config")
    target.addHandler(handler)
    yield handler.lines
    target.removeHandler(handler)


def _until(predicate: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _config(auth_doc: dict[str, object] | None = None) -> bytes:
    doc = json.loads(GENERIC.read_text(encoding="utf-8"))
    if auth_doc is not None:
        doc["auth"] = auth_doc
    return json.dumps(doc).encode()


def _program(config: bytes | None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("program.st", "PROGRAM p END_PROGRAM")
        zf.writestr("debug-map.json", DEBUG_MAP.read_bytes())
        if config is not None:
            zf.writestr("conf/mqtt.json", config)
    return buf.getvalue()


def _deploy(client, token: str, monkeypatch: pytest.MonkeyPatch, config: bytes | None) -> None:
    data = {"file": (io.BytesIO(_program(config)), "program.zip")}
    resp = client.post(
        "/api/upload-file", data=data, headers=auth(token), content_type="multipart/form-data"
    )
    assert resp.status_code == 200 and resp.get_json()["UploadFileFail"] == ""
    with monkeypatch.context() as scoped:
        scoped.setattr(plcapp_management.subprocess, "Popen", lambda *a, **k: _FakeProcess())
        plcapp_management.build_state.clear()
        plcapp_management.run_compile(app_module.runtime_manager, cwd="core/generated")
    assert plcapp_management.build_state.status.name == "SUCCESS"


def _stage_password(client, token: str) -> dict[str, object]:
    request = {
        "method": "username_password",
        "binding": {
            "host": "broker.example.net",
            "port": 1883,
            "tls_enabled": False,
            "client_id": "plc-linha1",
            "method": "username_password",
        },
        "programMd5": MD5,
        "username": "plc-user",
    }
    data = {
        "request": (io.BytesIO(json.dumps(request).encode()), "request.json", "application/json"),
        "password": (io.BytesIO(PASSWORD), "password"),
    }
    resp = client.post(
        "/api/mqtt/credentials/staging",
        data=data,
        headers=auth(token),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.get_json()
    ref = resp.get_json()["credentialRef"]
    assert isinstance(ref, dict)
    return ref


def _status(client, token: str) -> dict[str, object]:
    resp = client.get("/api/mqtt/status", headers=auth(token))
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body, dict) and check(body, "status") == [], body
    return body


# --- authentication and derived states ------------------------------------------


def test_requires_a_token(client, device) -> None:
    assert client.get("/api/mqtt/status").status_code == 401
    assert client.get("/api/mqtt/status", headers=auth("forged.token.x")).status_code in (401, 422)


def test_without_plugins_conf_is_disabled_and_creates_nothing(client, admin_token, device) -> None:
    Path("plugins.conf").unlink()
    body = _status(client, admin_token)
    assert (body["state"], body["configured"]) == ("disabled", False)
    assert not Path("plugins.conf").exists()


def test_program_without_mqtt_is_disabled(client, admin_token, device, monkeypatch) -> None:
    _deploy(client, admin_token, monkeypatch, None)
    body = _status(client, admin_token)
    assert body["state"] == "disabled" and body["error"] is None and "mode" not in body


def test_installed_but_stopped_plc_is_plc_stopped(
    client, admin_token, device, monkeypatch, runtime
) -> None:
    _deploy(client, admin_token, monkeypatch, _config())
    assert ACTIVE.is_file()
    body = _status(client, admin_token)
    assert body["state"] == "plc_stopped" and body["configured"] is True
    assert body["mode"] == "generic" and body["programMd5"] == MD5
    assert body["broker"] == {"host": "broker.example.net", "port": 1883, "tls": False}
    assert body["error"] is None


def test_running_plc_whose_plugin_does_not_answer_is_an_error(
    client, admin_token, device, monkeypatch, runtime
) -> None:
    _deploy(client, admin_token, monkeypatch, _config())
    runtime.state = "RUNNING"
    body = _status(client, admin_token)
    assert body["state"] == "config_error"
    assert body["error"]["category"] == "internal"  # type: ignore[index]
    assert body["error"]["code"] == "internal_error"  # type: ignore[index]


def test_an_installed_file_that_no_longer_decodes_is_a_configuration_error(
    client, admin_token, device, monkeypatch
) -> None:
    _deploy(client, admin_token, monkeypatch, _config())
    ACTIVE.write_text('{"contract":"openplc-mqtt-runtime-config","format_version":9}')
    body = _status(client, admin_token)
    assert body["state"] == "config_error" and body["configured"] is False
    assert body["error"]["code"] == "config_unknown_version"  # type: ignore[index]


@pytest.mark.parametrize(
    "reply",
    [
        {"contract": "something-else", "version": 1, "state": "connected", "configured": True},
        {"contract": "openplc-mqtt-status", "version": 2, "state": "connected", "configured": True},
        {"contract": "openplc-mqtt-status", "version": 1, "state": "on", "configured": True},
        {"error": "unknown_request"},
    ],
    ids=["contract", "version", "state", "error"],
)
def test_an_unexpected_reply_is_never_relayed(
    client, admin_token, device, monkeypatch, reply: dict[str, object]
) -> None:
    _deploy(client, admin_token, monkeypatch, _config())
    server = StatusServer(str(SOCKET), lambda _line: reply, lambda *_a: None)
    assert server.start()
    try:
        body = _status(client, admin_token)
    finally:
        server.stop()
    assert body["state"] == "config_error"
    assert body["error"]["code"] == "internal_error"  # type: ignore[index]


# --- live status from the running plugin -----------------------------------------


def test_running_plugin_status_is_served_as_reported(
    client, admin_token, device, monkeypatch, runtime, plugin
) -> None:
    state, _ = plugin
    _deploy(client, admin_token, monkeypatch, _config())
    runtime.state = "RUNNING"
    assert mqtt_plugin.start_loop() is True
    assert _until(lambda: state.snapshot()["state"] == "connected")
    assert _until(lambda: state.snapshot()["stats"]["published"] > 0)  # type: ignore[index]
    body = _status(client, admin_token)
    assert body["state"] == "connected" and body["mode"] == "generic"
    assert body["stats"]["leaves"] == 6  # type: ignore[index]
    assert body["stats"]["published"] > 0  # type: ignore[index]
    assert mqtt_plugin.stop_loop() is True
    runtime.state = "STOPPED"
    assert _status(client, admin_token)["state"] == "plc_stopped"


def test_removing_the_active_credential_stops_the_running_session(
    client, admin_token, device, monkeypatch, runtime, plugin, audit_lines
) -> None:
    state, harness = plugin
    ref = _stage_password(client, admin_token)
    _deploy(
        client,
        admin_token,
        monkeypatch,
        _config({"method": "username_password", "credential_ref": ref}),
    )
    runtime.state = "RUNNING"
    assert mqtt_plugin.start_loop() is True
    assert _until(lambda: _status(client, admin_token)["state"] == "connected")
    assert harness.passwords == [PASSWORD]
    resp = client.delete("/api/mqtt/credentials/active", headers=auth(admin_token))
    assert resp.status_code == 204
    assert harness.network.sessions[0].closed and state.publisher is None
    assert [t for t in threading.enumerate() if t.name.startswith("mqtt-publisher-")] == []
    body = _status(client, admin_token)
    assert body["state"] == "credential_error"
    assert body["error"]["code"] == "credential_missing"  # type: ignore[index]
    assert any("event=remove result=destroyed" in l and "session=stopped" in l for l in audit_lines)
    # A later start with the same configuration cannot reuse the removed credential.
    assert mqtt_plugin.start_loop() is False
    assert len(harness.network.sessions) == 1
    assert _status(client, admin_token)["error"]["code"] == "credential_missing"  # type: ignore[index]
    text = json.dumps(body) + "\n".join(audit_lines)
    assert PASSWORD.decode() not in text


def test_removal_without_a_running_plugin_is_still_destroyed(
    client, admin_token, device, monkeypatch, audit_lines
) -> None:
    ref = _stage_password(client, admin_token)
    _deploy(
        client,
        admin_token,
        monkeypatch,
        _config({"method": "username_password", "credential_ref": ref}),
    )
    resp = client.delete("/api/mqtt/credentials/active", headers=auth(admin_token))
    assert resp.status_code == 204 and device.snapshot().active is None
    assert any("event=remove" in l and "session=not_running" in l for l in audit_lines)
