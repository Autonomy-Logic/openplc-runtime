# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Vault document, staging request and error model. Standard library only.

The vault document holds only non-secret metadata in clear; every secret is
inside an AES-256-GCM ciphertext whose associated data binds it to the
credential id, revision and broker binding.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final, Literal

from contract.constants import MAX_STAGED, STAGING_TTL_S
from contract.names import validate_client_id
from contract.runtime_config import (
    ConfigRejected,
    JsonValue,
    MqttRuntimeConfig,
    is_uuid_v4,
    is_valid_host,
    parse_json_document,
)

SecretMethod = Literal["username_password", "client_certificate"]
TombstoneReason = Literal["consumed", "expired"]

SECRET_METHODS: Final[tuple[SecretMethod, ...]] = ("username_password", "client_certificate")
VAULT_CONTRACT: Final = "openplc-mqtt-credential-vault"
LOCATOR_CONTRACT: Final = "openplc-mqtt-vault-locator"
FORMAT_VERSION: Final = 1

STAGING_TTL_MS: Final = STAGING_TTL_S * 1000
MAX_STAGED_CREDENTIALS: Final = MAX_STAGED
MAX_TOMBSTONES: Final = 32
MAX_REQUEST_BYTES: Final = 262_144
MAX_REQUEST_JSON_BYTES: Final = 16_384
MAX_USERNAME_CHARS: Final = 256
MAX_PASSWORD_BYTES: Final = 1024
MAX_CERTIFICATE_BYTES: Final = 65_536
MAX_KEY_BYTES: Final = 65_536
MAX_PASSPHRASE_BYTES: Final = 1024
MAX_VAULT_BYTES: Final = 2 * 1024 * 1024
MAX_LOCATOR_BYTES: Final = 8192
MAX_ACTOR_CHARS: Final = 128
MAX_SUBJECT_CHARS: Final = 512
KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12

CODE_CATEGORY: Final[dict[str, str]] = {
    "credential_invalid_format": "credential",
    "credential_method_mismatch": "credential",
    "credential_missing": "credential",
    "credential_too_large": "credential",
    "credential_binding_mismatch": "credential",
    "client_certificate_expired": "certificate",
    "client_key_mismatch": "certificate",
    "client_key_passphrase_invalid": "certificate",
    "staging_consumed": "credential",
    "staging_expired": "credential",
    "staging_limit": "credential",
    "staging_not_found": "credential",
    "staging_owner_mismatch": "credential",
    "forbidden": "authorization",
    "internal_error": "internal",
}

HTTP_STATUS: Final[dict[str, int]] = {
    "credential_invalid_format": 400,
    "credential_method_mismatch": 400,
    "client_certificate_expired": 400,
    "client_key_mismatch": 400,
    "client_key_passphrase_invalid": 400,
    "forbidden": 403,
    "staging_owner_mismatch": 403,
    "credential_missing": 404,
    "staging_not_found": 404,
    "staging_consumed": 409,
    "credential_binding_mismatch": 409,
    "staging_expired": 410,
    "credential_too_large": 413,
    "staging_limit": 429,
    "internal_error": 500,
}


class CredentialError(Exception):
    """Sanitized rejection: the message never carries secret or request values."""

    def __init__(self, code: str, message: str, pointer: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.pointer = pointer

    @property
    def category(self) -> str:
        return CODE_CATEGORY[self.code]

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.code]

    def to_api(self) -> dict[str, dict[str, str]]:
        body = {"code": self.code, "category": self.category, "message": self.message}
        if self.pointer is not None:
            body["pointer"] = self.pointer
        return {"error": body}


class VaultCorrupt(Exception):
    """The vault or locator file cannot be trusted; callers fail closed."""


def format_ms(epoch_ms: int) -> str:
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_ms % 1000:03d}Z"


def clean_label(value: str, limit: int) -> str:
    """Printable, bounded text for actor names and certificate subjects."""
    return "".join(ch for ch in value if ch.isprintable())[:limit]


@dataclass(frozen=True)
class Binding:
    """Broker identity a credential may be sent to."""

    host: str
    port: int
    tls_enabled: bool
    client_id: str
    method: SecretMethod

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "host": self.host,
            "port": self.port,
            "tls_enabled": self.tls_enabled,
            "client_id": self.client_id,
            "method": self.method,
        }

    @staticmethod
    def from_config(config: MqttRuntimeConfig) -> Binding | None:
        method = config.auth.method
        if method == "anonymous":
            return None
        connection = config.connection
        return Binding(
            connection.host, connection.port, connection.tls.enabled, connection.client_id, method
        )


def aad(credential_id: str, revision: int, binding: Binding) -> bytes:
    """Associated data of a sealed credential (canonical JSON)."""
    return json.dumps(
        {"id": credential_id, "revision": revision, "binding": binding.to_json()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


@dataclass(frozen=True)
class Actor:
    """Authenticated caller as established by the runtime's own JWT check."""

    user_id: str
    username: str
    origin: str

    def label(self) -> str:
        return clean_label(self.username, MAX_ACTOR_CHARS) or f"user {self.user_id}"


@dataclass(frozen=True)
class CertificateInfo:
    sha256: str
    subject: str
    not_after_ms: int

    def to_api(self) -> dict[str, JsonValue]:
        return {
            "sha256": self.sha256,
            "subject": self.subject,
            "notAfter": format_ms(self.not_after_ms),
        }


@dataclass(frozen=True)
class SealedRecord:
    credential_id: str
    revision: int
    binding: Binding
    username_set: bool
    certificate: CertificateInfo | None
    updated_at_ms: int
    updated_by: str
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)

    @property
    def method(self) -> SecretMethod:
        return self.binding.method

    def to_api(self) -> dict[str, JsonValue]:
        body: dict[str, JsonValue] = {
            "id": self.credential_id,
            "revision": self.revision,
            "method": self.method,
            "binding": self.binding.to_json(),
            "updatedAt": format_ms(self.updated_at_ms),
            "updatedBy": self.updated_by,
        }
        if self.method == "username_password":
            body["usernameSet"] = self.username_set
        if self.certificate is not None:
            body["clientCertificate"] = self.certificate.to_api()
        return body


@dataclass(frozen=True)
class StagedRecord:
    staging_id: str
    owner_id: str
    program_md5: str
    expires_at_ms: int
    record: SealedRecord

    def to_api(self) -> dict[str, JsonValue]:
        body: dict[str, JsonValue] = {
            "stagingId": self.staging_id,
            "credentialRef": {
                "id": self.record.credential_id,
                "revision": self.record.revision,
                "staging_id": self.staging_id,
            },
            "method": self.record.method,
            "binding": self.record.binding.to_json(),
            "programMd5": self.program_md5,
            "expiresAt": format_ms(self.expires_at_ms),
            "createdBy": self.record.updated_by,
        }
        if self.record.certificate is not None:
            body["clientCertificate"] = self.record.certificate.to_api()
        return body


@dataclass(frozen=True)
class Tombstone:
    staging_id: str
    reason: TombstoneReason
    until_ms: int


@dataclass(frozen=True)
class VaultDocument:
    active: SealedRecord | None
    staged: tuple[StagedRecord, ...]
    tombstones: tuple[Tombstone, ...]

    def to_bytes(self) -> bytes:
        doc = {
            "contract": VAULT_CONTRACT,
            "version": FORMAT_VERSION,
            "active": None if self.active is None else _record_json(self.active),
            "staged": [_staged_json(item) for item in self.staged],
            "tombstones": [
                {"staging_id": t.staging_id, "reason": t.reason, "until_ms": t.until_ms}
                for t in self.tombstones
            ],
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("ascii")

    @staticmethod
    def from_bytes(raw: bytes) -> VaultDocument:
        if len(raw) > MAX_VAULT_BYTES:
            raise VaultCorrupt("vault exceeds its size bound")
        try:
            tree = parse_json_document(raw, MAX_VAULT_BYTES, 8)
        except ConfigRejected as exc:
            raise VaultCorrupt("vault is not valid JSON") from exc
        try:
            return _document(tree)
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise VaultCorrupt("vault does not match its format") from exc


EMPTY_VAULT: Final = VaultDocument(None, (), ())


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: JsonValue) -> bytes:
    if not isinstance(text, str):
        raise TypeError("expected base64 text")
    return base64.b64decode(text.encode("ascii"), validate=True)


def _record_json(record: SealedRecord) -> dict[str, JsonValue]:
    certificate: JsonValue = None
    if record.certificate is not None:
        certificate = {
            "sha256": record.certificate.sha256,
            "subject": record.certificate.subject,
            "not_after_ms": record.certificate.not_after_ms,
        }
    return {
        "id": record.credential_id,
        "revision": record.revision,
        "binding": record.binding.to_json(),
        "username_set": record.username_set,
        "certificate": certificate,
        "updated_at_ms": record.updated_at_ms,
        "updated_by": record.updated_by,
        "nonce": _b64(record.nonce),
        "ciphertext": _b64(record.ciphertext),
    }


def _staged_json(item: StagedRecord) -> dict[str, JsonValue]:
    return {
        "staging_id": item.staging_id,
        "owner_id": item.owner_id,
        "program_md5": item.program_md5,
        "expires_at_ms": item.expires_at_ms,
        "record": _record_json(item.record),
    }


def _obj(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    return value


def _str(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a string")
    return value


def _int(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected an integer")
    return value


def _bool(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected a boolean")
    return value


def _uuid(value: JsonValue) -> str:
    text = _str(value)
    if not is_uuid_v4(text):
        raise ValueError("expected a UUID v4")
    return text


def parse_binding(value: JsonValue) -> Binding:
    """Strict binding object; raises ValueError/TypeError/KeyError."""
    obj = _obj(value)
    if set(obj) != {"host", "port", "tls_enabled", "client_id", "method"}:
        raise ValueError("binding members")
    host = _str(obj["host"])
    port = _int(obj["port"])
    tls_enabled = _bool(obj["tls_enabled"])
    client_id = _str(obj["client_id"])
    method = obj["method"]
    if not is_valid_host(host) or not 1 <= port <= 65_535:
        raise ValueError("binding host or port")
    if validate_client_id(client_id) is not None:
        raise ValueError("binding client_id")
    if method == "username_password":
        return Binding(host, port, tls_enabled, client_id, "username_password")
    if method == "client_certificate":
        return Binding(host, port, tls_enabled, client_id, "client_certificate")
    raise ValueError("binding method")


def _record(value: JsonValue) -> SealedRecord:
    obj = _obj(value)
    certificate = None
    if obj["certificate"] is not None:
        cert = _obj(obj["certificate"])
        certificate = CertificateInfo(
            _str(cert["sha256"]), _str(cert["subject"]), _int(cert["not_after_ms"])
        )
    nonce = _unb64(obj["nonce"])
    if len(nonce) != NONCE_BYTES:
        raise ValueError("nonce length")
    revision = _int(obj["revision"])
    if revision < 1:
        raise ValueError("revision")
    return SealedRecord(
        _uuid(obj["id"]),
        revision,
        parse_binding(obj["binding"]),
        _bool(obj["username_set"]),
        certificate,
        _int(obj["updated_at_ms"]),
        _str(obj["updated_by"]),
        nonce,
        _unb64(obj["ciphertext"]),
    )


def _document(tree: JsonValue) -> VaultDocument:
    obj = _obj(tree)
    if obj["contract"] != VAULT_CONTRACT or obj["version"] != FORMAT_VERSION:
        raise ValueError("unknown vault format")
    active = None if obj["active"] is None else _record(obj["active"])
    staged = []
    for item in _list(obj["staged"]):
        entry = _obj(item)
        staged.append(
            StagedRecord(
                _uuid(entry["staging_id"]),
                _str(entry["owner_id"]),
                _str(entry["program_md5"]),
                _int(entry["expires_at_ms"]),
                _record(entry["record"]),
            )
        )
    tombstones = []
    for item in _list(obj["tombstones"]):
        entry = _obj(item)
        reason = entry["reason"]
        if reason not in ("consumed", "expired"):
            raise ValueError("tombstone reason")
        tombstones.append(
            Tombstone(
                _uuid(entry["staging_id"]),
                "consumed" if reason == "consumed" else "expired",
                _int(entry["until_ms"]),
            )
        )
    return VaultDocument(active, tuple(staged), tuple(tombstones))


def _list(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise TypeError("expected an array")
    return value


@dataclass(frozen=True)
class StagingRequest:
    method: SecretMethod
    binding: Binding
    program_md5: str
    username: str | None


def _invalid(pointer: str, message: str) -> CredentialError:
    return CredentialError("credential_invalid_format", message, pointer)


def parse_staging_request(raw: bytes) -> StagingRequest:
    """The multipart ``request`` part (``$defs/stagingRequest``), strictly."""
    try:
        tree = parse_json_document(raw, MAX_REQUEST_JSON_BYTES, 4)
    except ConfigRejected as exc:
        raise _invalid("", "the request part is not a valid JSON document") from exc
    if not isinstance(tree, dict):
        raise _invalid("", "the request part must be a JSON object")
    allowed = {"method", "binding", "programMd5", "username"}
    for key in tree:
        if key not in allowed:
            raise _invalid(f"/{key}", "unknown member")
    for key in ("method", "binding", "programMd5"):
        if key not in tree:
            raise _invalid("", f"missing member '{key}'")
    method = tree["method"]
    if method not in SECRET_METHODS:
        raise _invalid("/method", "unknown credential method")
    try:
        binding = parse_binding(tree["binding"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid("/binding", "the broker binding is not valid") from exc
    if binding.method != method:
        raise _invalid("/binding/method", "the binding method differs from the request method")
    if method == "client_certificate" and not binding.tls_enabled:
        raise _invalid("/binding/tls_enabled", "a client certificate requires TLS")
    md5 = tree["programMd5"]
    if not isinstance(md5, str) or len(md5) != 32 or any(c not in "0123456789abcdef" for c in md5):
        raise _invalid("/programMd5", "expected a lowercase MD5 hex digest")
    username = tree.get("username")
    if method == "username_password":
        if not isinstance(username, str) or not 1 <= len(username) <= MAX_USERNAME_CHARS:
            raise _invalid("/username", "a username of 1 to 256 characters is required")
        if "\x00" in username or not _encodable(username):
            raise _invalid("/username", "the username is not a valid MQTT UTF-8 string")
        return StagingRequest("username_password", binding, md5, username)
    if username is not None:
        raise _invalid("/username", "a username is only valid with username_password")
    return StagingRequest("client_certificate", binding, md5, None)


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@dataclass(frozen=True)
class SecretPayload:
    """Plaintext sealed inside a record. Never logged, never returned by the API."""

    username: str | None = field(repr=False)
    password: bytes | None = field(repr=False)
    certificate_pem: bytes | None = field(repr=False)
    key_pem: bytes | None = field(repr=False)

    def to_bytes(self) -> bytes:
        doc = {
            "username": self.username,
            "password": None if self.password is None else _b64(self.password),
            "certificate": None if self.certificate_pem is None else _b64(self.certificate_pem),
            "key": None if self.key_pem is None else _b64(self.key_pem),
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("ascii")

    @staticmethod
    def from_bytes(raw: bytes) -> SecretPayload:
        try:
            obj = _obj(json.loads(raw.decode("ascii")))

            def optional(name: str) -> bytes | None:
                value = obj[name]
                return None if value is None else _unb64(value)

            username = obj["username"]
            if username is not None and not isinstance(username, str):
                raise TypeError("username")
            return SecretPayload(
                username, optional("password"), optional("certificate"), optional("key")
            )
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise VaultCorrupt("sealed payload does not match its format") from exc


@dataclass(frozen=True)
class VaultLocator:
    """Where the plugin finds the vault; written by the webserver next to mqtt.json."""

    vault_path: str
    key_path: str
    python: str

    def to_bytes(self) -> bytes:
        doc = {
            "contract": LOCATOR_CONTRACT,
            "version": FORMAT_VERSION,
            "vault_path": self.vault_path,
            "key_path": self.key_path,
            "python": self.python,
        }
        return json.dumps(doc, sort_keys=True, indent=2).encode("utf-8")

    @staticmethod
    def from_bytes(raw: bytes) -> VaultLocator:
        try:
            tree = parse_json_document(raw, MAX_LOCATOR_BYTES, 2)
            obj = _obj(tree)
            if set(obj) != {"contract", "version", "vault_path", "key_path", "python"}:
                raise ValueError("locator members")
            if obj["contract"] != LOCATOR_CONTRACT or obj["version"] != FORMAT_VERSION:
                raise ValueError("unknown locator format")
            paths = [_str(obj[name]) for name in ("vault_path", "key_path", "python")]
        except (ConfigRejected, KeyError, TypeError, ValueError) as exc:
            raise VaultCorrupt("vault locator does not match its format") from exc
        if not all(path.startswith("/") and "\x00" not in path for path in paths):
            raise VaultCorrupt("vault locator paths must be absolute")
        return VaultLocator(paths[0], paths[1], paths[2])
