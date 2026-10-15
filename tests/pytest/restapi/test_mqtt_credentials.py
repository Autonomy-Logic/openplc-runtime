# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""MQTT credentials through the real REST API: staging, upload transaction, activation.

Every request carries a real JWT. Marker secrets are searched afterwards in
responses, files, logs, build logs and the retrievable project snapshot.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import shutil
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import auth, create_user, token_for

from webserver import app as app_module
from webserver import mqtt_config, plcapp_management, project_snapshot, restapi
from webserver.mqtt_api import mqtt_bp

RUNTIME_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RUNTIME_ROOT / "tests/pytest/plugins/mqtt"))

# isort: split
# pylint: disable=wrong-import-position,wrong-import-order
import mqtt_plugin
from api_schema import check
from mqtt_credentials.model import Actor
from mqtt_credentials.vault import CredentialVault
from mqtt_pki import Pki

# pylint: enable=wrong-import-position,wrong-import-order

CONFIG_DIR = RUNTIME_ROOT / "core/src/drivers/plugins/python/mqtt/contract/fixtures/runtime-config"
GENERIC = CONFIG_DIR / "valid" / "generic-on-change-anonymous-plaintext.json"
DEBUG_MAP = CONFIG_DIR / "debug-map.json"
MD5 = "3f2c9d0e8b7a6f5e4d3c2b1a09f8e7d6"
PASSWORD = b"MARKER-PW-runtime-e2e-31f7"
PASSPHRASE = b"MARKER-PASSPHRASE-9a0c"
ACTIVE = Path("core/src/drivers/plugins/python/mqtt/mqtt.json")
LOCATOR = Path("core/src/drivers/plugins/python/mqtt/mqtt.vault.json")

if "mqtt_blueprint" not in restapi.app_restapi.blueprints:
    restapi.app_restapi.register_blueprint(mqtt_bp, url_prefix="/api/mqtt")


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000_000

    def __call__(self) -> int:
        return self.now


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class _FakeRuntime:
    def __init__(self) -> None:
        self.state = "STOPPED"
        self.stoppable = True

    def status_plc(self) -> str:
        return f"STATUS:{self.state}\n"

    def stop_plc(self) -> str:
        if self.stoppable:
            self.state = "STOPPED"
        return "OK\n"

    def reset_crash_tracking(self) -> None:
        pass


class _FakeProcess:
    def __init__(self, exit_code: int) -> None:
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self._exit_code = exit_code

    def wait(self) -> int:
        return self._exit_code


@pytest.fixture(scope="module")
def pki() -> Pki:
    return Pki()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def audit_lines() -> Iterator[list[str]]:
    handler = _ListHandler()
    target = logging.getLogger("webserver.mqtt_config")
    target.addHandler(handler)
    yield handler.lines
    target.removeHandler(handler)


@pytest.fixture
def device(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> Iterator[CredentialVault]:
    """A runtime working directory, its vault and the upload route wired as in production."""
    device_dir = tmp_path / "device"
    device_dir.mkdir()
    monkeypatch.chdir(device_dir)
    shutil.copy(RUNTIME_ROOT / "plugins_default.conf", "plugins_default.conf")
    shutil.copy(RUNTIME_ROOT / "plugins_default.conf", "plugins.conf")
    runtime = _FakeRuntime()
    monkeypatch.setattr(app_module, "runtime_manager", runtime)
    monkeypatch.setattr(app_module, "apply_vpp_plugin_conf", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "apply_retain_conf", lambda *a, **k: None)
    monkeypatch.setattr(app_module.etherdog_manager, "apply_busconfig", lambda _path: None)
    monkeypatch.setattr(app_module, "run_compile", lambda *a, **k: None)
    monkeypatch.setattr(app_module.build_state, "status", app_module.BuildStatus.SUCCESS)
    monkeypatch.setattr(restapi, "_handler_callback_post", app_module.restapi_callback_post)
    store = CredentialVault(tmp_path / "persist" / "mqtt", tmp_path / "persist" / "keys", clock)
    monkeypatch.setattr(mqtt_config, "_vault", store)
    monkeypatch.setattr(mqtt_config, "_claim", None)
    monkeypatch.setattr(mqtt_config, "_plugin_python", lambda _plugin: sys.executable)
    monkeypatch.setattr(app_module, "_fake_runtime", runtime, raising=False)
    yield store


@pytest.fixture
def bob_token(client, admin_token: str) -> str:
    assert create_user(client, "bob", "bob-pass", token=admin_token).status_code == 201
    return token_for(client, "bob", "bob-pass")


# --- helpers -------------------------------------------------------------------


def _binding(method: str, host: str = "broker.example.net", tls: bool = False) -> dict[str, object]:
    return {
        "host": host,
        "port": 8883 if tls else 1883,
        "tls_enabled": tls,
        "client_id": "plc-linha1",
        "method": method,
    }


def _stage_password(
    client, token: str, password: bytes = PASSWORD, md5: str = MD5, host: str = "broker.example.net"
):
    request = {
        "method": "username_password",
        "binding": _binding("username_password", host),
        "programMd5": md5,
        "username": "plc-user",
    }
    data = {
        "request": (io.BytesIO(json.dumps(request).encode()), "request.json", "application/json"),
        "password": (io.BytesIO(password), "password"),
    }
    return client.post(
        "/api/mqtt/credentials/staging",
        data=data,
        headers=auth(token),
        content_type="multipart/form-data",
    )


def _stage_certificate(
    client, token: str, cert: bytes, key: bytes, passphrase: bytes | None = None
):
    request = {
        "method": "client_certificate",
        "binding": _binding("client_certificate", tls=True),
        "programMd5": MD5,
    }
    data: dict[str, object] = {
        "request": (io.BytesIO(json.dumps(request).encode()), "request.json", "application/json"),
        "client_certificate": (io.BytesIO(cert), "client.crt"),
        "client_key": (io.BytesIO(key), "client.key"),
    }
    if passphrase is not None:
        data["client_key_passphrase"] = (io.BytesIO(passphrase), "passphrase")
    return client.post(
        "/api/mqtt/credentials/staging",
        data=data,
        headers=auth(token),
        content_type="multipart/form-data",
    )


def _config(
    ref: dict[str, object], method: str, host: str = "broker.example.net", tls=None
) -> bytes:
    doc = json.loads(GENERIC.read_text())
    doc["auth"] = {"method": method, "credential_ref": ref}
    doc["connection"]["host"] = host
    if tls is not None:
        doc["connection"]["tls"] = tls
        doc["connection"]["port"] = 8883
    return json.dumps(doc).encode()


def _program(config: bytes | None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("program.st", "PROGRAM p END_PROGRAM")
        zf.writestr("debug-map.json", DEBUG_MAP.read_bytes())
        if config is not None:
            zf.writestr("conf/mqtt.json", config)
    return buf.getvalue()


def _upload(client, token: str, blob: bytes, snapshot: bytes | None = None) -> dict[str, object]:
    data: dict[str, object] = {"file": (io.BytesIO(blob), "program.zip")}
    if snapshot is not None:
        data["snapshot"] = (io.BytesIO(snapshot), "project.zip")
        data["snapshot_metadata"] = json.dumps({"formatVersion": 1, "projectName": "line 1"})
    resp = client.post(
        "/api/upload-file", data=data, headers=auth(token), content_type="multipart/form-data"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body, dict)
    return body


def _build(monkeypatch: pytest.MonkeyPatch, exit_code: int = 0) -> str:
    """Run the real build orchestration with the compiler scripts faked."""
    with monkeypatch.context() as scoped:
        scoped.setattr(
            plcapp_management.subprocess, "Popen", lambda *a, **k: _FakeProcess(exit_code)
        )
        plcapp_management.build_state.clear()
        plcapp_management.run_compile(app_module.runtime_manager, cwd="core/generated")
    return plcapp_management.build_state.status.name


def _staged_password_upload(client, token: str) -> tuple[dict[str, object], bytes]:
    resp = _stage_password(client, token)
    assert resp.status_code == 201, resp.get_json()
    ref = resp.get_json()["credentialRef"]
    return resp.get_json(), _config(ref, "username_password")


def _active_ref(store: CredentialVault) -> dict[str, object]:
    active = store.snapshot().active
    assert active is not None
    return {"id": active.credential_id, "revision": active.revision, "staging_id": None}


def _mqtt_error(body: dict[str, object]) -> dict[str, str]:
    error = body["MqttConfigError"]
    assert isinstance(error, dict)
    return error


# --- endpoints -----------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/mqtt/credentials"),
        ("post", "/api/mqtt/credentials/staging"),
        ("delete", "/api/mqtt/credentials/staging/0d9e8f7a-6b5c-4d3e-8f1a-2b3c4d5e6f70"),
        ("delete", "/api/mqtt/credentials/active"),
    ],
)
def test_every_credential_endpoint_requires_a_token(client, device, method: str, path: str) -> None:
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers=auth("forged.token.x")).status_code in (401, 422)


def test_no_endpoint_reads_back_a_secret(device) -> None:
    rules = [r for r in restapi.app_restapi.url_map.iter_rules() if r.rule.startswith("/api/mqtt")]
    readable = sorted(r.rule for r in rules if "GET" in (r.methods or set()))
    assert readable == ["/api/mqtt/capabilities", "/api/mqtt/credentials", "/api/mqtt/status"]


def test_password_staging_is_write_only(client, admin_token: str, device, audit_lines) -> None:
    resp = _stage_password(client, admin_token)
    assert resp.status_code == 201
    body = resp.get_json()
    assert check(body, "stagingStatus") == []
    assert body["createdBy"] == "admin" and body["programMd5"] == MD5
    listing = client.get("/api/mqtt/credentials", headers=auth(admin_token))
    assert listing.status_code == 200 and check(listing.get_json(), "credentials") == []
    assert listing.get_json()["staged"][0]["stagingId"] == body["stagingId"]
    for text in (resp.data, listing.data, "\n".join(audit_lines).encode()):
        assert PASSWORD not in text and base64.b64encode(PASSWORD) not in text
    assert any(
        "event=stage result=accepted actor='admin'" in line and "origin=127.0.0.1" in line
        for line in audit_lines
    )


def test_certificate_staging_reports_only_public_metadata(
    client, admin_token: str, device, pki: Pki
) -> None:
    from cryptography.hazmat.primitives import serialization
    from mqtt_pki import key_pem

    key = serialization.load_pem_private_key(pki.client_ok.key_pem, None)
    encrypted = key_pem(key, PASSPHRASE)  # type: ignore[arg-type]
    resp = _stage_certificate(client, admin_token, pki.client_ok.cert_pem, encrypted, PASSPHRASE)
    assert resp.status_code == 201, resp.get_json()
    info = resp.get_json()["clientCertificate"]
    assert info["subject"] == "CN=plc-linha1" and len(info["sha256"]) == 64
    raw = device.vault_path.read_bytes()
    assert b"PRIVATE KEY" not in raw and PASSPHRASE not in raw and PASSPHRASE not in resp.data


@pytest.mark.parametrize(
    "case, status, code",
    [
        ("mismatch", 400, "client_key_mismatch"),
        ("expired", 400, "client_certificate_expired"),
        ("password_part_for_certificate", 400, "credential_invalid_format"),
        ("missing_key", 400, "credential_invalid_format"),
        ("unknown_part", 400, "credential_invalid_format"),
        ("no_request", 400, "credential_invalid_format"),
        ("empty_password", 400, "credential_invalid_format"),
        ("large_password", 413, "credential_too_large"),
        ("large_request", 413, "credential_too_large"),
    ],
)
def test_invalid_staging_is_refused_before_anything_is_stored(
    client, admin_token: str, device, pki: Pki, case: str, status: int, code: str
) -> None:
    if case == "mismatch":
        resp = _stage_certificate(client, admin_token, pki.client_ok.cert_pem, pki.other_key_pem)
    elif case == "expired":
        expired = pki.client_expired
        resp = _stage_certificate(client, admin_token, expired.cert_pem, expired.key_pem)
    elif case == "empty_password":
        resp = _stage_password(client, admin_token, password=b"")
    elif case == "large_password":
        resp = _stage_password(client, admin_token, password=b"x" * 1025)
    else:
        request = {
            "method": "client_certificate",
            "binding": _binding("client_certificate", tls=True),
            "programMd5": MD5,
        }
        data: dict[str, object] = {
            "request": (io.BytesIO(json.dumps(request).encode()), "r.json"),
            "client_certificate": (io.BytesIO(pki.client_ok.cert_pem), "c"),
            "client_key": (io.BytesIO(pki.client_ok.key_pem), "k"),
        }
        if case == "password_part_for_certificate":
            data["password"] = (io.BytesIO(PASSWORD), "p")
        elif case == "missing_key":
            del data["client_key"]
        elif case == "unknown_part":
            data["ciphertext"] = (io.BytesIO(b"x"), "x")
        elif case == "no_request":
            del data["request"]
        elif case == "large_request":
            # Only the Content-Length check answers 413 here; parsing would answer 400.
            data["padding"] = (io.BytesIO(b"x" * 300_000), "p")
        resp = client.post(
            "/api/mqtt/credentials/staging",
            data=data,
            headers=auth(admin_token),
            content_type="multipart/form-data",
        )
    assert resp.status_code == status, resp.get_json()
    assert resp.get_json()["error"]["code"] == code
    assert check(resp.get_json(), "error") == []
    assert not device.vault_path.exists() or device.snapshot().staged == ()
    assert PASSWORD not in resp.data


def test_methods_without_the_pinned_cryptography_are_refused(
    client, admin_token: str, device, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mqtt_config.material, "crypto_available", lambda: False)
    resp = _stage_password(client, admin_token)
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "credential_method_mismatch"


def test_staging_limit(client, admin_token: str, device) -> None:
    for _ in range(4):
        assert _stage_password(client, admin_token).status_code == 201
    resp = _stage_password(client, admin_token)
    assert (resp.status_code, resp.get_json()["error"]["code"]) == (429, "staging_limit")


def test_cancel_is_restricted_to_the_creator(
    client, admin_token: str, bob_token: str, device, clock: Clock, monkeypatch
) -> None:
    staging_id = _stage_password(client, admin_token).get_json()["stagingId"]
    path = f"/api/mqtt/credentials/staging/{staging_id}"
    resp = client.delete(path, headers=auth(bob_token))
    assert (resp.status_code, resp.get_json()["error"]["code"]) == (403, "staging_owner_mismatch")
    assert client.delete(path, headers=auth(admin_token)).status_code == 204
    assert client.delete(path, headers=auth(admin_token)).status_code == 404
    assert (
        client.delete("/api/mqtt/credentials/staging/nope", headers=auth(admin_token)).status_code
        == 404
    )
    expiring = _stage_password(client, admin_token).get_json()["stagingId"]
    clock.now += 600_000
    resp = client.delete(f"/api/mqtt/credentials/staging/{expiring}", headers=auth(admin_token))
    assert (resp.status_code, resp.get_json()["error"]["code"]) == (410, "staging_expired")


# --- upload transaction --------------------------------------------------------


def test_staged_credential_activates_with_its_program(
    client, admin_token: str, device, monkeypatch, audit_lines
) -> None:
    receipt, config = _staged_password_upload(client, admin_token)
    body = _upload(client, admin_token, _program(config))
    assert body["UploadFileFail"] == "" and "MqttConfigError" not in body
    assert device.snapshot().active is None
    assert _build(monkeypatch) == "SUCCESS"
    active = device.snapshot().active
    assert active is not None and active.revision == receipt["credentialRef"]["revision"]
    assert device.snapshot().staged == ()
    listing = client.get("/api/mqtt/credentials", headers=auth(admin_token)).get_json()
    assert check(listing, "credentials") == [] and listing["active"]["usernameSet"] is True
    locator = json.loads(LOCATOR.read_text())
    assert locator["vault_path"] == str(device.vault_path.resolve())
    # The plugin opens the credential in its own interpreter, from the installed files.
    generation = mqtt_plugin.PluginState().activate(str(ACTIVE))
    assert isinstance(generation, mqtt_plugin.ActiveGeneration)
    assert generation.session.password == PASSWORD
    assert any("event=activate result=committed" in line for line in audit_lines)
    # Single use: the same reference is refused afterwards.
    again = _mqtt_error(_upload(client, admin_token, _program(config)))
    assert (again["code"], again["category"]) == ("staging_consumed", "credential")


def test_reopening_reuses_the_active_credential_only_for_the_same_broker(
    client, admin_token: str, device, monkeypatch
) -> None:
    _, config = _staged_password_upload(client, admin_token)
    _upload(client, admin_token, _program(config))
    assert _build(monkeypatch) == "SUCCESS"
    ref = _active_ref(device)
    assert (
        _upload(client, admin_token, _program(_config(ref, "username_password")))["UploadFileFail"]
        == ""
    )
    assert _build(monkeypatch) == "SUCCESS"
    moved = _mqtt_error(
        _upload(client, admin_token, _program(_config(ref, "username_password", "evil.example")))
    )
    assert moved["code"] == "config_credential_binding_mismatch"
    stale = dict(ref, revision=99)
    assert (
        _mqtt_error(_upload(client, admin_token, _program(_config(stale, "username_password"))))[
            "code"
        ]
        == "config_credential_missing"
    )


def test_replacement_keeps_the_old_credential_until_activation(
    client, admin_token: str, device, monkeypatch
) -> None:
    _, config = _staged_password_upload(client, admin_token)
    _upload(client, admin_token, _program(config))
    assert _build(monkeypatch) == "SUCCESS"
    first = device.snapshot().active
    resp = _stage_password(client, admin_token, password=b"MARKER-PW-second")
    ref = resp.get_json()["credentialRef"]
    assert ref["id"] == first.credential_id and ref["revision"] == first.revision + 1
    _upload(client, admin_token, _program(_config(ref, "username_password")))
    assert _build(monkeypatch, exit_code=1) == "FAILED"
    assert device.snapshot().active == first and device.snapshot().staged == ()


def test_activation_failure_discards_the_staging_and_keeps_the_previous_credential(
    client, admin_token: str, device, monkeypatch
) -> None:
    _, config = _staged_password_upload(client, admin_token)
    _upload(client, admin_token, _program(config))
    assert _build(monkeypatch) == "SUCCESS"
    first = device.snapshot().active
    ref = _stage_password(client, admin_token).get_json()["credentialRef"]
    assert (
        _upload(client, admin_token, _program(_config(ref, "username_password")))["UploadFileFail"]
        == ""
    )
    Path("core/generated/conf/mqtt.json").write_bytes(
        _config(ref, "username_password", "evil.example")
    )
    assert _build(monkeypatch) == "FAILED"
    assert device.snapshot().active == first and device.snapshot().staged == ()
    assert not ACTIVE.exists()


def test_another_users_staging_is_refused_and_kept(
    client, admin_token: str, bob_token: str, device
) -> None:
    receipt, config = _staged_password_upload(client, admin_token)
    error = _mqtt_error(_upload(client, bob_token, _program(config)))
    assert (error["code"], error["category"]) == ("staging_owner_mismatch", "credential")
    assert [s.staging_id for s in device.snapshot().staged] == [receipt["stagingId"]]


@pytest.mark.parametrize(
    "case, code",
    [
        ("program", "staging_program_mismatch"),
        ("broker", "config_credential_binding_mismatch"),
        ("expired", "staging_expired"),
        ("revision", "config_credential_missing"),
        ("other_target", "config_credential_missing"),
    ],
)
def test_upload_rejections_discard_the_callers_staging(
    client,
    admin_token: str,
    device,
    clock: Clock,
    monkeypatch,
    tmp_path: Path,
    case: str,
    code: str,
) -> None:
    md5 = "0" * 32 if case == "program" else MD5
    receipt = _stage_password(client, admin_token, md5=md5).get_json()
    ref = dict(receipt["credentialRef"])
    host = "broker.example.net"
    if case == "broker":
        host = "evil.example"
    elif case == "expired":
        clock.now += 600_000
    elif case == "revision":
        ref["revision"] += 1
    elif case == "other_target":
        other = CredentialVault(tmp_path / "other" / "mqtt", tmp_path / "other" / "keys", clock)
        monkeypatch.setattr(mqtt_config, "_vault", other)
    error = _mqtt_error(
        _upload(client, admin_token, _program(_config(ref, "username_password", host)))
    )
    assert error["code"] == code
    monkeypatch.setattr(mqtt_config, "_vault", device)
    # Another runtime never sees the staging, so the original one keeps it until it expires.
    assert len(device.snapshot().staged) == (1 if case == "other_target" else 0)


def test_upload_failure_after_validation_discards_the_claimed_staging(
    client, admin_token: str, device, monkeypatch
) -> None:
    _, config = _staged_password_upload(client, admin_token)
    monkeypatch.setattr(app_module, "ensure_plc_stopped", lambda *a, **k: (False, True))
    body = _upload(client, admin_token, _program(config))
    assert "could not be stopped" in str(body["UploadFileFail"])
    assert device.snapshot().staged == () and mqtt_config.begin_build() is None


def test_unauthenticated_activation_of_a_staging_fails_closed(device, monkeypatch) -> None:
    store = device
    from mqtt_credentials.model import Binding, SecretPayload, StagingRequest

    binding = Binding("broker.example.net", 1883, False, "plc-linha1", "username_password")
    request = StagingRequest("username_password", binding, MD5, "u")
    staged = store.stage(
        request, SecretPayload("u", PASSWORD, None, None), None, Actor("1", "admin", "")
    )
    generated = Path("core/generated/conf")
    generated.mkdir(parents=True)
    ref = {"id": staged.record.credential_id, "revision": 1, "staging_id": staged.staging_id}
    (generated / "mqtt.json").write_bytes(_config(ref, "username_password"))
    shutil.copy(DEBUG_MAP, "core/generated/debug-map.json")
    with pytest.raises(mqtt_config.MqttActivationError):
        plcapp_management.update_plugin_configurations("core/generated")
    assert store.snapshot().active is None


def test_startup_sweep_destroys_expired_staging(client, admin_token: str, device, clock) -> None:
    _stage_password(client, admin_token)
    clock.now += 600_000
    mqtt_config.sweep_staging()
    assert device.snapshot().staged == ()


def test_remove_active(client, admin_token: str, device, monkeypatch) -> None:
    resp = client.delete("/api/mqtt/credentials/active", headers=auth(admin_token))
    assert (resp.status_code, resp.get_json()["error"]["code"]) == (404, "credential_missing")
    _, config = _staged_password_upload(client, admin_token)
    _upload(client, admin_token, _program(config))
    assert _build(monkeypatch) == "SUCCESS"
    assert (
        client.delete("/api/mqtt/credentials/active", headers=auth(admin_token)).status_code == 204
    )
    assert (
        client.get("/api/mqtt/credentials", headers=auth(admin_token)).get_json()["active"] is None
    )
    result = mqtt_plugin.PluginState().activate(str(ACTIVE))
    assert not isinstance(result, mqtt_plugin.ActiveGeneration)
    assert result.code == "credential_missing"


# --- secret inspection ---------------------------------------------------------


def _files_under(*roots: Path) -> Iterator[Path]:
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file():
                yield path


def test_marker_secrets_appear_nowhere_but_sealed(
    client, admin_token: str, device, monkeypatch, pki: Pki, audit_lines, tmp_path: Path
) -> None:
    from cryptography.hazmat.primitives import serialization
    from mqtt_pki import key_pem

    key = serialization.load_pem_private_key(pki.client_ok.key_pem, None)
    encrypted = key_pem(key, PASSPHRASE)  # type: ignore[arg-type]
    plain_key = key_pem(key)  # type: ignore[arg-type]
    responses: list[bytes] = []
    receipt = _stage_certificate(client, admin_token, pki.client_ok.cert_pem, encrypted, PASSPHRASE)
    responses.append(receipt.data)
    tls = {"enabled": True, "trust": {"source": "pem", "ca_pem": pki.broker_ca_pem.decode()}}
    config = _config(receipt.get_json()["credentialRef"], "client_certificate", tls=tls)
    snapshot = _program(None)
    body = _upload(client, admin_token, _program(config), snapshot=snapshot)
    responses.append(json.dumps(body).encode())
    assert _build(monkeypatch) == "SUCCESS"
    _staged_password_upload(client, admin_token)
    responses.append(client.get("/api/mqtt/credentials", headers=auth(admin_token)).data)
    retrieved = client.get("/api/project-snapshot", headers=auth(admin_token))
    assert retrieved.status_code == 200
    responses.append(retrieved.data)
    responses.append(base64.b64decode(retrieved.get_json()["contentBase64"]))
    generation = mqtt_plugin.PluginState().activate(str(ACTIVE))
    assert isinstance(generation, mqtt_plugin.ActiveGeneration)
    responses.append(repr(generation).encode())

    key_body = b"".join(plain_key.splitlines()[1:-1])
    markers = [PASSWORD, PASSPHRASE, key_body[48:96], base64.b64encode(PASSWORD)]
    persistent = Path(project_snapshot.SNAPSHOT_DIR).parent
    texts = [path.read_bytes() for path in _files_under(tmp_path, persistent)]
    texts += responses
    texts.append("\n".join(audit_lines).encode())
    texts.append("".join(plcapp_management.build_state.logs).encode())
    for marker in markers:
        assert not any(marker in text for text in texts), marker[:12]
    for location in (device.vault_path, device.key_path):
        assert not location.resolve().is_relative_to(Path("core/generated").resolve())
        assert not location.resolve().is_relative_to(ACTIVE.parent.resolve())


def test_default_vault_lives_outside_snapshot_generated_and_plugin_dirs(monkeypatch) -> None:
    monkeypatch.setattr(mqtt_config, "_vault", None)
    store = mqtt_config.vault()
    snapshot_dir = Path(project_snapshot.SNAPSHOT_DIR).resolve()
    for location in (store.vault_path, store.key_path):
        resolved = location.resolve()
        assert not resolved.is_relative_to(snapshot_dir)
        assert not resolved.is_relative_to((RUNTIME_ROOT / "core").resolve())
    assert store.vault_path.name == "credentials.vault"
    assert store.key_path.parent.name == "keys" and store.key_path.name == "mqtt-vault.key"


def test_unloadable_ca_is_refused_at_upload(client, admin_token: str, device) -> None:
    fake = json.loads((CONFIG_DIR / "valid/generic-periodic-password-private-ca.json").read_text())
    doc = json.loads(GENERIC.read_text())
    doc["connection"]["tls"] = fake["connection"]["tls"]
    doc["connection"]["port"] = 8883
    error = _mqtt_error(_upload(client, admin_token, _program(json.dumps(doc).encode())))
    assert (error["code"], error["pointer"]) == ("config_schema", "/connection/tls/trust/ca_pem")
