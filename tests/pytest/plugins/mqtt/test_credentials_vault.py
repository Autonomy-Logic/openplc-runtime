# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Credential vault: staging rules, sealing at rest, validation and isolated unsealing."""

from __future__ import annotations

import base64
import json
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from api_schema import check
from contract.runtime_config import NON_CONFIG_ERROR_CATEGORY
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mqtt_credentials import material, unseal
from mqtt_credentials.material import ValidatedIdentity, validate_client_identity
from mqtt_credentials.model import (
    CODE_CATEGORY,
    HTTP_STATUS,
    Actor,
    Binding,
    CredentialError,
    SecretPayload,
    StagedRecord,
    StagingRequest,
    Tombstone,
    VaultCorrupt,
    VaultDocument,
    VaultLocator,
    parse_staging_request,
)
from mqtt_credentials.vault import CredentialVault, unseal_active
from mqtt_pki import Pki, key_pem

FIXTURES = Path(__file__).resolve().parents[4] / (
    "core/src/drivers/plugins/python/mqtt/contract/fixtures"
)
PASSWORD = b"MARKER-PW-vault-unit-4d1e"
ALICE = Actor("1", "alice", "10.0.0.5")
BOB = Actor("2", "bob", "10.0.0.6")
BINDING = Binding("broker.example.net", 8883, True, "plc-linha1", "username_password")
CERT_BINDING = replace(BINDING, method="client_certificate")
MD5 = "3f2c9d0e8b7a6f5e4d3c2b1a09f8e7d6"


@pytest.fixture(scope="module")
def pki() -> Pki:
    return Pki()


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000_000

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> CredentialVault:
    return CredentialVault(tmp_path / "data" / "mqtt", tmp_path / "data" / "keys", clock)


def _password_request(binding: Binding = BINDING) -> StagingRequest:
    return StagingRequest("username_password", binding, MD5, "plc-user")


def _stage(
    store: CredentialVault, actor: Actor = ALICE, binding: Binding = BINDING
) -> StagedRecord:
    payload = SecretPayload("plc-user", PASSWORD, None, None)
    return store.stage(_password_request(binding), payload, None, actor)


# --- error model ---------------------------------------------------------------


def test_every_code_matches_the_shared_catalog() -> None:
    catalog = json.loads((FIXTURES / "errors/error-codes.json").read_text())["codes"]
    for code, category in {**CODE_CATEGORY, **NON_CONFIG_ERROR_CATEGORY}.items():
        assert catalog[code] == category, code
    assert set(HTTP_STATUS) == set(CODE_CATEGORY)


def test_http_statuses_follow_the_contract() -> None:
    expected = {
        "credential_invalid_format": 400,
        "forbidden": 403,
        "staging_owner_mismatch": 403,
        "staging_not_found": 404,
        "staging_consumed": 409,
        "credential_binding_mismatch": 409,
        "staging_expired": 410,
        "credential_too_large": 413,
        "staging_limit": 429,
    }
    assert {code: HTTP_STATUS[code] for code in expected} == expected
    body = CredentialError("staging_expired", "expired", "/x").to_api()
    assert check(body, "error") == []


# --- staging request -----------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["staging-request-password.json", "staging-request-certificate.json"]
)
def test_shared_valid_requests_parse(name: str) -> None:
    request = parse_staging_request((FIXTURES / "api" / name).read_bytes())
    assert request.binding.host == "broker.example.net"
    assert request.program_md5 == MD5


@pytest.mark.parametrize(
    "name, pointer",
    [
        ("invalid-staging-request-password-without-username.json", "/username"),
        ("invalid-staging-request-certificate-plaintext.json", "/binding/tls_enabled"),
    ],
)
def test_shared_invalid_requests_are_rejected(name: str, pointer: str) -> None:
    with pytest.raises(CredentialError) as info:
        parse_staging_request((FIXTURES / "api" / name).read_bytes())
    assert (info.value.code, info.value.pointer) == ("credential_invalid_format", pointer)


def _request_doc(**changes: object) -> bytes:
    doc = json.loads((FIXTURES / "api/staging-request-password.json").read_text())
    doc.update(changes)
    return json.dumps(doc).encode()


@pytest.mark.parametrize(
    "raw, pointer",
    [
        (_request_doc(extra=1), "/extra"),
        (_request_doc(programMd5="ABC"), "/programMd5"),
        (_request_doc(method="client_certificate"), "/binding/method"),
        (_request_doc(username="a\u0000b"), "/username"),
        (_request_doc(username="x" * 257), "/username"),
        (b'{"method":"username_password","method":"client_certificate"}', ""),
        (b"[]", ""),
        (b"{" * 64, ""),
        (_request_doc(binding={"host": "broker.example.net"}), "/binding"),
        (_request_doc(password="MARKER"), "/password"),
    ],
)
def test_malformed_requests_are_rejected_with_a_pointer(raw: bytes, pointer: str) -> None:
    with pytest.raises(CredentialError) as info:
        parse_staging_request(raw)
    assert (info.value.code, info.value.pointer) == ("credential_invalid_format", pointer)
    assert "MARKER" not in info.value.message


def test_lone_surrogate_username_is_rejected() -> None:
    raw = _request_doc().replace(b'"plc-user"', b'"\\ud800"')
    with pytest.raises(CredentialError):
        parse_staging_request(raw)


# --- client identity validation ------------------------------------------------


def _validate(cert: bytes, key: bytes, passphrase: bytes | None = None) -> ValidatedIdentity:
    return validate_client_identity(cert, key, passphrase, time.time_ns() // 1_000_000)


def test_valid_ec_identity_is_normalized(pki: Pki) -> None:
    identity = _validate(pki.client_ok.cert_pem, pki.client_ok.key_pem)
    assert identity.certificate_chain_pem.startswith(b"-----BEGIN CERTIFICATE-----")
    assert identity.key_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert identity.info.subject == "CN=plc-linha1"
    assert len(identity.info.sha256) == 64
    assert "key_pem" not in repr(identity) and "BEGIN" not in repr(identity)


def test_encrypted_key_is_stored_without_its_passphrase(pki: Pki) -> None:
    key = serialization.load_pem_private_key(pki.client_rsa.key_pem, None)
    assert isinstance(key, rsa.RSAPrivateKey)
    encrypted = key_pem(key, b"MARKER-PASSPHRASE")
    identity = _validate(pki.client_rsa.cert_pem, encrypted, b"MARKER-PASSPHRASE")
    assert b"ENCRYPTED" not in identity.key_pem
    with pytest.raises(CredentialError) as wrong:
        _validate(pki.client_rsa.cert_pem, encrypted, b"wrong")
    assert wrong.value.code == "client_key_passphrase_invalid"
    with pytest.raises(CredentialError) as missing:
        _validate(pki.client_rsa.cert_pem, encrypted)
    assert missing.value.code == "client_key_passphrase_invalid"
    with pytest.raises(CredentialError) as extra:
        _validate(pki.client_rsa.cert_pem, pki.client_rsa.key_pem, b"unneeded")
    assert extra.value.code == "client_key_passphrase_invalid"


@pytest.mark.parametrize(
    "case, code",
    [
        ("mismatch", "client_key_mismatch"),
        ("expired", "client_certificate_expired"),
        ("future", "credential_invalid_format"),
        ("garbage_cert", "credential_invalid_format"),
        ("garbage_key", "credential_invalid_format"),
        ("weak_rsa", "credential_invalid_format"),
        ("dsa", "credential_invalid_format"),
    ],
)
def test_invalid_identities_are_refused_before_storage(pki: Pki, case: str, code: str) -> None:
    cert, key = {
        "mismatch": (pki.client_ok.cert_pem, pki.other_key_pem),
        "expired": (pki.client_expired.cert_pem, pki.client_expired.key_pem),
        "future": (pki.client_future.cert_pem, pki.client_future.key_pem),
        "garbage_cert": (
            b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
            pki.client_ok.key_pem,
        ),
        "garbage_key": (pki.client_ok.cert_pem, b"MARKER-not-a-key"),
        "weak_rsa": (pki.client_weak_rsa.cert_pem, pki.client_weak_rsa.key_pem),
        "dsa": (pki.client_ok.cert_pem, pki.dsa_key_pem),
    }[case]
    with pytest.raises(CredentialError) as info:
        _validate(cert, key)
    assert info.value.code == code
    assert "MARKER" not in str(info.value)


def test_unpinned_cryptography_disables_secret_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    import cryptography

    material.crypto_available.cache_clear()
    monkeypatch.setattr(cryptography, "__version__", "49.0.0")
    try:
        assert material.crypto_available() is False
        with pytest.raises(material.CryptoUnavailable):
            material.seal(b"k" * 32, b"", b"x")
    finally:
        monkeypatch.undo()
        material.crypto_available.cache_clear()
    assert material.crypto_available() is True


# --- storage and protection at rest --------------------------------------------


def test_staging_seals_the_secret_with_restricted_files(store: CredentialVault) -> None:
    staged = _stage(store)
    raw = store.vault_path.read_bytes()
    assert PASSWORD not in raw and base64.b64encode(PASSWORD) not in raw
    assert b"plc-user" not in raw
    assert stat.S_IMODE(store.vault_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.vault_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.key_path.parent.stat().st_mode) == 0o700
    assert len(store.key_path.read_bytes()) == 32
    assert staged.record.revision == 1 and staged.record.updated_by == "alice"
    assert check(staged.to_api(), "stagingStatus") == []
    assert "MARKER" not in repr(staged)


def _activate(
    store: CredentialVault, actor: Actor = ALICE, binding: Binding = BINDING
) -> StagedRecord:
    staged = _stage(store, actor, binding)
    store.promote(staged.staging_id, actor)
    return staged


def test_active_secret_round_trips_only_for_its_reference_and_binding(
    store: CredentialVault,
) -> None:
    staged = _activate(store)
    ref = staged.record
    payload = unseal_active(store.vault_path, store.key_path, ref.credential_id, 1, BINDING)
    assert (payload.username, payload.password) == ("plc-user", PASSWORD)
    with pytest.raises(CredentialError) as revision:
        unseal_active(store.vault_path, store.key_path, ref.credential_id, 2, BINDING)
    assert revision.value.code == "credential_missing"
    moved = replace(BINDING, host="attacker.example")
    with pytest.raises(CredentialError) as rebound:
        unseal_active(store.vault_path, store.key_path, ref.credential_id, 1, moved)
    assert rebound.value.code == "credential_binding_mismatch"


@pytest.mark.parametrize("tamper", ["ciphertext", "binding", "key"])
def test_tampering_or_rebinding_at_rest_fails_authentication(
    store: CredentialVault, tamper: str
) -> None:
    staged = _activate(store)
    doc = json.loads(store.vault_path.read_text())
    if tamper == "ciphertext":
        blob = bytearray(base64.b64decode(doc["active"]["ciphertext"]))
        blob[0] ^= 1
        doc["active"]["ciphertext"] = base64.b64encode(bytes(blob)).decode()
        store.vault_path.write_text(json.dumps(doc))
        binding = BINDING
    elif tamper == "binding":
        doc["active"]["binding"]["host"] = "attacker.example"
        store.vault_path.write_text(json.dumps(doc))
        binding = replace(BINDING, host="attacker.example")
    else:
        store.key_path.write_bytes(b"\x01" * 32)
        binding = BINDING
    with pytest.raises(CredentialError) as info:
        unseal_active(store.vault_path, store.key_path, staged.record.credential_id, 1, binding)
    assert info.value.code == "credential_missing"


def test_corrupt_vault_fails_closed(store: CredentialVault) -> None:
    _stage(store)
    store.vault_path.write_bytes(b'{"contract":"other"}')
    with pytest.raises(VaultCorrupt):
        store.snapshot()
    store.key_path.write_bytes(b"short")
    with pytest.raises(VaultCorrupt):
        unseal_active(store.vault_path, store.key_path, "x", 1, BINDING)


def test_losing_the_key_discards_records_it_sealed(store: CredentialVault) -> None:
    _activate(store)
    store.key_path.unlink()
    _stage(store)
    doc = store.snapshot()
    assert doc.active is None and len(doc.staged) == 1


def test_vault_document_round_trips() -> None:
    assert VaultDocument.from_bytes(VaultDocument(None, (), ()).to_bytes()) == VaultDocument(
        None, (), ()
    )
    with pytest.raises(VaultCorrupt):
        VaultDocument.from_bytes(b"x" * (2 * 1024 * 1024 + 1))


# --- staging lifecycle ---------------------------------------------------------


def test_staging_expires_and_leaves_a_tombstone(store: CredentialVault, clock: Clock) -> None:
    staged = _stage(store)
    clock.now += 600_000
    assert store.sweep() == [staged.staging_id]
    found = store.find(staged.staging_id)
    assert isinstance(found, Tombstone) and found.reason == "expired"
    with pytest.raises(CredentialError) as info:
        store.cancel(staged.staging_id, ALICE)
    assert info.value.code == "staging_expired"


def test_a_claimed_staging_survives_the_sweep(store: CredentialVault, clock: Clock) -> None:
    staged = _stage(store)
    clock.now += 600_000
    assert store.sweep(protect=(staged.staging_id,)) == []
    assert isinstance(store.find(staged.staging_id, (staged.staging_id,)), StagedRecord)


def test_only_the_creator_cancels(store: CredentialVault) -> None:
    staged = _stage(store)
    with pytest.raises(CredentialError) as other:
        store.cancel(staged.staging_id, BOB)
    assert other.value.code == "staging_owner_mismatch"
    reused_id = Actor("1", "mallory", "10.0.0.9")
    with pytest.raises(CredentialError):
        store.cancel(staged.staging_id, reused_id)
    store.cancel(staged.staging_id, ALICE)
    assert store.find(staged.staging_id) is None
    with pytest.raises(CredentialError) as again:
        store.cancel(staged.staging_id, ALICE)
    assert again.value.code == "staging_not_found"


def test_use_is_single_and_replacement_is_transactional(store: CredentialVault) -> None:
    first = _activate(store)
    second = _stage(store)
    assert second.record.credential_id == first.record.credential_id
    assert second.record.revision == 2
    previous, active = store.promote(second.staging_id, ALICE)
    assert active.revision == 2 and previous.active is not None
    with pytest.raises(CredentialError) as reused:
        store.cancel(second.staging_id, ALICE)
    assert reused.value.code == "staging_consumed"
    with pytest.raises(CredentialError):
        store.promote(second.staging_id, ALICE)
    store.restore(previous)
    assert store.snapshot().active == previous.active
    removed = store.remove_active()
    assert removed.revision == 1 and store.snapshot().active is None
    with pytest.raises(CredentialError) as none:
        store.remove_active()
    assert none.value.code == "credential_missing"


def test_staging_is_limited(store: CredentialVault) -> None:
    for _ in range(4):
        _stage(store)
    with pytest.raises(CredentialError) as info:
        _stage(store)
    assert info.value.code == "staging_limit"


def test_discard_destroys_without_tombstone(store: CredentialVault) -> None:
    staged = _stage(store)
    assert store.discard(staged.staging_id) is True
    assert store.discard(staged.staging_id) is False
    assert store.find(staged.staging_id) is None


# --- isolated unsealing (the plugin path) --------------------------------------


def _locator(store: CredentialVault, python: str = sys.executable) -> VaultLocator:
    return VaultLocator(str(store.vault_path), str(store.key_path), python)


def test_helper_returns_the_secret_over_a_pipe(store: CredentialVault) -> None:
    staged = _activate(store)
    result = unseal.request_unseal(_locator(store), staged.record.credential_id, 1, BINDING)
    assert isinstance(result, SecretPayload)
    assert (result.username, result.password) == ("plc-user", PASSWORD)
    assert "MARKER" not in repr(result)


def test_helper_returns_a_client_identity(store: CredentialVault, pki: Pki) -> None:
    identity = _validate(pki.client_ok.cert_pem, pki.client_ok.key_pem)
    request = StagingRequest("client_certificate", CERT_BINDING, MD5, None)
    payload = SecretPayload(None, None, identity.certificate_chain_pem, identity.key_pem)
    staged = store.stage(request, payload, identity.info, ALICE)
    store.promote(staged.staging_id, ALICE)
    raw = store.vault_path.read_bytes()
    assert identity.key_pem not in raw and b"PRIVATE KEY" not in raw
    result = unseal.request_unseal(_locator(store), staged.record.credential_id, 1, CERT_BINDING)
    assert isinstance(result, SecretPayload) and result.key_pem == identity.key_pem


@pytest.mark.parametrize(
    "case, code",
    [
        ("revision", "credential_missing"),
        ("binding", "credential_binding_mismatch"),
        ("no_key", "credential_missing"),
        ("no_python", "internal_error"),
        ("corrupt", "internal_error"),
    ],
)
def test_helper_failures_are_structured(store: CredentialVault, case: str, code: str) -> None:
    staged = _activate(store)
    ref, revision, binding = staged.record.credential_id, 1, BINDING
    locator = _locator(store)
    if case == "revision":
        revision = 7
    elif case == "binding":
        binding = replace(BINDING, port=1883, tls_enabled=False)
    elif case == "no_key":
        store.key_path.unlink()
    elif case == "no_python":
        locator = _locator(store, "/nonexistent/python3")
    else:
        store.vault_path.write_bytes(b"{")
    result = unseal.request_unseal(locator, ref, revision, binding)
    assert isinstance(result, unseal.UnsealFailure) and result.code == code


def test_helper_runs_isolated_from_the_caller_environment(store: CredentialVault) -> None:
    staged = _activate(store)
    probe = subprocess.run(
        [sys.executable, "-I", "-B", unseal.__file__],
        input=b"not json",
        capture_output=True,
        check=True,
        env={"PYTHONPATH": "/nonexistent"},
    )
    assert json.loads(probe.stdout) == {
        "ok": False,
        "code": "internal_error",
        "message": "malformed helper request",
    }
    assert staged.staging_id not in probe.stdout.decode()


def test_helper_timeout_is_reported(store: CredentialVault, tmp_path: Path) -> None:
    slow = tmp_path / "slow-python"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o700)
    result = unseal.request_unseal(_locator(store, str(slow)), "x", 1, BINDING, timeout_s=0.3)
    assert isinstance(result, unseal.UnsealFailure) and result.code == "internal_error"


def test_locator_requires_absolute_paths() -> None:
    good = VaultLocator("/a/v", "/a/k", "/a/p")
    assert VaultLocator.from_bytes(good.to_bytes()) == good
    for bad in (b"{}", VaultLocator("rel/v", "/a/k", "/a/p").to_bytes()):
        with pytest.raises(VaultCorrupt):
            VaultLocator.from_bytes(bad)
