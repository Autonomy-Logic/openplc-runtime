# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Broker TLS context against real loopback handshakes, and the plugin session material."""

from __future__ import annotations

import inspect
import json
import os
import socket
import ssl
import stat
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import mqtt_plugin
import pytest
from contract.runtime_config import (
    ConfigError,
    TlsSettings,
    TlsTrust,
    decode_runtime_config,
)
from mqtt_credentials import tls
from mqtt_credentials.model import (
    Actor,
    Binding,
    SecretPayload,
    StagingRequest,
    VaultLocator,
)
from mqtt_credentials.session import LOCATOR_NAME, SessionMaterial, prepare_session
from mqtt_credentials.tls import ClientIdentity, TlsMaterialError, build_tls_context
from mqtt_credentials.unseal import UnsealFailure
from mqtt_credentials.vault import CredentialVault
from mqtt_pki import Pair, Pki

CONFIG_DIR = Path(__file__).resolve().parents[4] / (
    "core/src/drivers/plugins/python/mqtt/contract/fixtures/runtime-config"
)
PASSWORD = b"MARKER-PW-session-77b0"
ALICE = Actor("1", "alice", "127.0.0.1")


@pytest.fixture(scope="module")
def pki() -> Pki:
    return Pki()


def _trust(pki: Pki) -> TlsSettings:
    return TlsSettings(True, TlsTrust("pem", pki.broker_ca_pem.decode()))


@contextmanager
def _server(
    pki: Pki,
    server: Pair,
    client_ca: bytes | None = None,
    max_version: ssl.TLSVersion | None = None,
) -> Iterator[tuple[int, list[str]]]:
    """One-shot TLS server on 127.0.0.1; yields its port and the server-side outcomes."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory() as directory:
        cert = pki.write(Path(directory), "server.crt", server.cert_pem)
        key = pki.write(Path(directory), "server.key", server.key_pem)
        context.load_cert_chain(cert, key)
    if client_ca is not None:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=client_ca.decode())
    if max_version is not None:
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        context.maximum_version = max_version
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(5)
    outcomes: list[str] = []

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        try:
            with context.wrap_socket(conn, server_side=True) as tls_conn:
                tls_conn.sendall(b"ok")
                outcomes.append("handshake")
        except (ssl.SSLError, OSError) as exc:
            outcomes.append(type(exc).__name__)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], outcomes
    finally:
        listener.close()
        thread.join(5)


def _connect(context: ssl.SSLContext, port: int, host: str = "localhost") -> bytes:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=host) as conn,
    ):
        return conn.recv(2)


def test_private_ca_is_trusted(pki: Pki) -> None:
    with _server(pki, pki.server_ok) as (port, _):
        assert _connect(build_tls_context(_trust(pki), None), port) == b"ok"


@pytest.mark.parametrize(
    "server, verify_code",
    [("server_wrong_host", 62), ("server_expired", 10), ("server_untrusted", 20)],
)
def test_broker_identity_failures_are_rejected(pki: Pki, server: str, verify_code: int) -> None:
    with (
        _server(pki, getattr(pki, server)) as (port, _),
        pytest.raises(ssl.SSLCertVerificationError) as info,
    ):
        _connect(build_tls_context(_trust(pki), None), port)
    assert info.value.verify_code == verify_code


def test_system_trust_does_not_accept_a_private_ca(pki: Pki) -> None:
    system = TlsSettings(True, TlsTrust("system", None))
    with (
        _server(pki, pki.server_ok) as (port, _),
        pytest.raises(ssl.SSLCertVerificationError),
    ):
        _connect(build_tls_context(system, None), port)


def test_tls_below_1_2_is_refused(pki: Pki) -> None:
    try:
        with (
            _server(pki, pki.server_ok, max_version=ssl.TLSVersion.TLSv1_1) as (port, _),
            pytest.raises((ssl.SSLError, ConnectionError)),
        ):
            _connect(build_tls_context(_trust(pki), None), port)
    except ssl.SSLError:
        pytest.skip("this OpenSSL cannot even configure a TLS 1.1 server")


def test_client_certificate_is_presented_and_required(pki: Pki) -> None:
    identity = ClientIdentity(pki.client_ok.cert_pem, pki.client_ok.key_pem)
    with _server(pki, pki.server_ok, pki.client_ca_pem) as (port, outcomes):
        assert _connect(build_tls_context(_trust(pki), identity), port) == b"ok"
    assert outcomes == ["handshake"]
    with (
        _server(pki, pki.server_ok, pki.client_ca_pem) as (port, outcomes),
        pytest.raises((ssl.SSLError, ConnectionError)),
    ):
        _connect(build_tls_context(_trust(pki), None), port)
    assert outcomes and outcomes[0] != "handshake"
    rogue = ClientIdentity(pki.client_rogue.cert_pem, pki.client_rogue.key_pem)
    with (
        _server(pki, pki.server_ok, pki.client_ca_pem) as (port, outcomes),
        pytest.raises((ssl.SSLError, ConnectionError)),
    ):
        _connect(build_tls_context(_trust(pki), rogue), port)
    assert outcomes and outcomes[0] != "handshake"


def test_verification_cannot_be_turned_off(pki: Pki) -> None:
    assert list(inspect.signature(build_tls_context).parameters) == ["tls", "identity"]
    context = build_tls_context(_trust(pki), None)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    with pytest.raises(ValueError):
        build_tls_context(TlsSettings(False, None), None)


def test_bad_material_is_reported_by_part(pki: Pki) -> None:
    fake = (CONFIG_DIR / "valid/generic-periodic-password-private-ca.json").read_text()
    ca_pem = json.loads(fake)["connection"]["tls"]["trust"]["ca_pem"]
    assert tls.check_ca_pem(ca_pem) is False
    assert tls.check_ca_pem(pki.broker_ca_pem.decode()) is True
    with pytest.raises(TlsMaterialError) as ca:
        build_tls_context(TlsSettings(True, TlsTrust("pem", ca_pem)), None)
    assert ca.value.part == "ca"
    mismatched = ClientIdentity(pki.client_ok.cert_pem, pki.other_key_pem)
    with pytest.raises(TlsMaterialError) as identity:
        build_tls_context(_trust(pki), mismatched)
    assert identity.value.part == "identity"


# --- short-lived identity files ------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="memfd is Linux-only")
def test_identity_never_touches_a_filesystem_with_memfd(
    pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("identity must not be written to a filesystem")

    monkeypatch.setattr(tls.tempfile, "mkdtemp", forbidden)
    identity = ClientIdentity(pki.client_ok.cert_pem, pki.client_ok.key_pem)
    build_tls_context(_trust(pki), identity)


def test_fallback_files_are_private_and_removed(pki: Pki, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(tls.os, "memfd_create", raising=False)
    seen: list[tuple[int, int, int]] = []
    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp
    real_load = ssl.SSLContext.load_cert_chain

    def spying_mkdtemp(*args: str, **kwargs: str) -> str:
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    def spying_load(self: ssl.SSLContext, certfile: str, keyfile: str) -> None:
        seen.append(
            (
                stat.S_IMODE(os.stat(os.path.dirname(certfile)).st_mode),
                stat.S_IMODE(os.stat(certfile).st_mode),
                stat.S_IMODE(os.stat(keyfile).st_mode),
            )
        )
        real_load(self, certfile, keyfile)

    monkeypatch.setattr(tls.tempfile, "mkdtemp", spying_mkdtemp)
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", spying_load)
    identity = ClientIdentity(pki.client_ok.cert_pem, pki.client_ok.key_pem)
    build_tls_context(_trust(pki), identity)
    with pytest.raises(TlsMaterialError):
        build_tls_context(_trust(pki), ClientIdentity(pki.client_ok.cert_pem, b"bad"))
    assert seen == [(0o700, 0o600, 0o600)] * 2
    assert len(created) == 2 and not any(os.path.exists(path) for path in created)


# --- plugin session material ---------------------------------------------------


def _config(auth: dict[str, object], tls_doc: dict[str, object], port: int = 8883) -> bytes:
    doc = json.loads((CONFIG_DIR / "valid/generic-on-change-anonymous-plaintext.json").read_text())
    doc["auth"] = auth
    doc["connection"]["tls"] = tls_doc
    doc["connection"]["port"] = port
    return json.dumps(doc).encode()


def _ref(credential_id: str, revision: int, method: str) -> dict[str, object]:
    return {
        "method": method,
        "credential_ref": {"id": credential_id, "revision": revision, "staging_id": None},
    }


def _pem_tls(pki: Pki) -> dict[str, object]:
    return {"enabled": True, "trust": {"source": "pem", "ca_pem": pki.broker_ca_pem.decode()}}


def _write(tmp_path: Path, raw: bytes, store: CredentialVault | None) -> str:
    path = tmp_path / "mqtt.json"
    path.write_bytes(raw)
    if store is not None:
        locator = VaultLocator(str(store.vault_path), str(store.key_path), sys.executable)
        (tmp_path / LOCATOR_NAME).write_bytes(locator.to_bytes())
    return str(path)


def _active_vault(
    tmp_path: Path, binding: Binding, payload: SecretPayload
) -> tuple[CredentialVault, str]:
    store = CredentialVault(tmp_path / "vault", tmp_path / "keys")
    request = StagingRequest(binding.method, binding, "0" * 32, payload.username)
    staged = store.stage(request, payload, None, ALICE)
    store.promote(staged.staging_id, ALICE)
    return store, staged.record.credential_id


def test_anonymous_plaintext_needs_no_material(tmp_path: Path) -> None:
    raw = _config({"method": "anonymous"}, {"enabled": False}, 1883)
    session = prepare_session(decode_runtime_config(raw, None), _write(tmp_path, raw, None))
    assert session == SessionMaterial(None, None, None)


def test_password_generation_holds_the_secret_without_showing_it(tmp_path: Path, pki: Pki) -> None:
    binding = Binding("broker.example.net", 8883, True, "plc-linha1", "username_password")
    store, credential_id = _active_vault(
        tmp_path, binding, SecretPayload("plc-user", PASSWORD, None, None)
    )
    raw = _config(_ref(credential_id, 1, "username_password"), _pem_tls(pki))
    state = mqtt_plugin.PluginState()
    generation = state.activate(_write(tmp_path, raw, store))
    assert isinstance(generation, mqtt_plugin.ActiveGeneration)
    assert generation.session.password == PASSWORD
    assert generation.session.username == "plc-user"
    assert isinstance(generation.session.tls_context, ssl.SSLContext)
    assert "MARKER" not in repr(generation) and "plc-user" not in repr(generation)


def test_certificate_generation_completes_a_mutual_tls_handshake(tmp_path: Path, pki: Pki) -> None:
    binding = Binding("localhost", 8883, True, "plc-linha1", "client_certificate")
    payload = SecretPayload(None, None, pki.client_ok.cert_pem, pki.client_ok.key_pem)
    store, credential_id = _active_vault(tmp_path, binding, payload)
    doc = json.loads(_config(_ref(credential_id, 1, "client_certificate"), _pem_tls(pki)))
    doc["connection"]["host"] = "localhost"
    raw = json.dumps(doc).encode()
    generation = mqtt_plugin.PluginState().activate(_write(tmp_path, raw, store))
    assert isinstance(generation, mqtt_plugin.ActiveGeneration)
    assert generation.session.password is None
    context = generation.session.tls_context
    assert context is not None
    with _server(pki, pki.server_ok, pki.client_ca_pem) as (port, outcomes):
        assert _connect(context, port) == b"ok"
    assert outcomes == ["handshake"]


@pytest.mark.parametrize(
    "case, code",
    [
        ("no_locator", "credential_missing"),
        ("other_revision", "credential_missing"),
        ("other_broker", "credential_binding_mismatch"),
        ("removed", "credential_missing"),
    ],
)
def test_generation_refuses_to_start_without_its_credential(
    tmp_path: Path, pki: Pki, case: str, code: str
) -> None:
    binding = Binding("broker.example.net", 8883, True, "plc-linha1", "username_password")
    store, credential_id = _active_vault(
        tmp_path, binding, SecretPayload("plc-user", PASSWORD, None, None)
    )
    revision = 2 if case == "other_revision" else 1
    raw = _config(_ref(credential_id, revision, "username_password"), _pem_tls(pki))
    if case == "other_broker":
        raw = raw.replace(b"broker.example.net", b"attacker.example.net")
    if case == "removed":
        store.remove_active()
    state = mqtt_plugin.PluginState()
    result = state.activate(_write(tmp_path, raw, None if case == "no_locator" else store))
    assert isinstance(result, ConfigError) and result.code == code
    assert result.pointer == "/auth/credential_ref" and state.generation is None
    assert "MARKER" not in result.message


def test_unknown_helper_codes_become_internal_errors(tmp_path: Path, pki: Pki) -> None:
    raw = _config(
        _ref("6f1c2a4e-8d3b-4c5a-9e7f-0a1b2c3d4e5f", 1, "username_password"), _pem_tls(pki)
    )
    store = CredentialVault(tmp_path / "vault", tmp_path / "keys")
    config = decode_runtime_config(raw, None)
    path = _write(tmp_path, raw, store)
    weird = prepare_session(config, path, lambda *_a: UnsealFailure("surprise", "x"))
    assert isinstance(weird, ConfigError) and weird.code == "internal_error"
    assert weird.category == "internal"


def test_identity_refused_by_the_tls_library_blocks_activation(tmp_path: Path, pki: Pki) -> None:
    raw = _config(
        _ref("6f1c2a4e-8d3b-4c5a-9e7f-0a1b2c3d4e5f", 1, "client_certificate"), _pem_tls(pki)
    )
    config = decode_runtime_config(raw, None)
    path = _write(tmp_path, raw, CredentialVault(tmp_path / "vault", tmp_path / "keys"))
    mismatched = SecretPayload(None, None, pki.client_ok.cert_pem, pki.other_key_pem)
    result = prepare_session(config, path, lambda *_a: mismatched)
    assert isinstance(result, ConfigError) and result.code == "client_key_mismatch"
    assert result.category == "certificate"


def test_unloadable_ca_blocks_activation(tmp_path: Path) -> None:
    raw = (CONFIG_DIR / "valid/generic-periodic-password-private-ca.json").read_bytes()
    config = decode_runtime_config(raw, None)
    path = _write(tmp_path, raw, CredentialVault(tmp_path / "vault", tmp_path / "keys"))
    password = SecretPayload("plc-user", PASSWORD, None, None)
    result = prepare_session(config, path, lambda *_a: password)
    assert isinstance(result, ConfigError)
    assert (result.code, result.pointer) == ("config_schema", "/connection/tls/trust/ca_pem")


def test_session_repr_hides_secrets() -> None:
    session = SessionMaterial("plc-user", PASSWORD, None)
    assert "plc-user" not in repr(session) and "MARKER" not in repr(session)
    assert replace(session, password=None).password is None
