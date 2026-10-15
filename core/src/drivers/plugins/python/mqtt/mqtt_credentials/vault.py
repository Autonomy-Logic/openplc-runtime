# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Operational credential vault: staging, activation and removal.

The webserver is the only writer. Every change replaces the vault file
atomically, so the plugin (a separate reader) never sees a partial state.
The protection boundary: a copy of the vault without the key file reveals no
secret and any tampering or rebinding fails authentication. Whoever holds both
files, or root on the host, can decrypt; this is not an HSM.
"""

from __future__ import annotations

import os
import stat
import threading
import time
import uuid
from collections.abc import Callable, Collection
from dataclasses import replace
from pathlib import Path

from .material import SealBroken, generate_key, open_sealed, seal
from .model import (
    EMPTY_VAULT,
    KEY_BYTES,
    MAX_STAGED_CREDENTIALS,
    MAX_TOMBSTONES,
    MAX_VAULT_BYTES,
    STAGING_TTL_MS,
    Actor,
    Binding,
    CertificateInfo,
    CredentialError,
    SealedRecord,
    SecretPayload,
    StagedRecord,
    StagingRequest,
    Tombstone,
    VaultCorrupt,
    VaultDocument,
    aad,
)

VAULT_FILE = "credentials.vault"
KEY_FILE = "mqtt-vault.key"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def owner_key(actor: Actor) -> str:
    """Staging owner: user id and name, so a reused id never inherits a staging."""
    return f"{actor.user_id}/{actor.username}"


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        path.chmod(0o700)


def _read_bounded(path: Path, limit: int) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError:
        return None
    if len(data) > limit:
        raise VaultCorrupt(f"{path.name} exceeds its size bound")
    return data


def read_key(key_path: Path) -> bytes | None:
    raw = _read_bounded(key_path, KEY_BYTES)
    if raw is None:
        return None
    if len(raw) != KEY_BYTES:
        raise VaultCorrupt("vault key has the wrong length")
    return raw


def read_document(vault_path: Path) -> VaultDocument:
    raw = _read_bounded(vault_path, MAX_VAULT_BYTES)
    return EMPTY_VAULT if raw is None else VaultDocument.from_bytes(raw)


def write_atomically(target: Path, data: bytes) -> None:
    temp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    dir_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def unseal_active(
    vault_path: Path, key_path: Path, credential_id: str, revision: int, binding: Binding
) -> SecretPayload:
    """Decrypt the active credential for a configuration's reference and binding."""
    key = read_key(key_path)
    active = read_document(vault_path).active
    if key is None or active is None:
        raise CredentialError("credential_missing", "no credential is stored on this runtime")
    if (active.credential_id, active.revision) != (credential_id, revision):
        raise CredentialError(
            "credential_missing", "the referenced credential revision is not the active one"
        )
    if active.binding != binding:
        raise CredentialError(
            "credential_binding_mismatch", "the credential is bound to another broker connection"
        )
    try:
        plaintext = open_sealed(
            key, active.nonce, active.ciphertext, aad(credential_id, revision, binding)
        )
    except SealBroken as exc:
        raise CredentialError(
            "credential_missing", "the stored credential cannot be authenticated"
        ) from exc
    payload = SecretPayload.from_bytes(plaintext)
    complete = (
        payload.username is not None and payload.password is not None
        if binding.method == "username_password"
        else payload.certificate_pem is not None and payload.key_pem is not None
    )
    if not complete:
        raise VaultCorrupt("sealed payload does not match its method")
    return payload


class CredentialVault:
    """Single writer of the vault and its key, serialized by one lock."""

    def __init__(
        self, directory: Path, key_directory: Path, clock_ms: Callable[[], int] = now_ms
    ) -> None:
        self.vault_path = directory / VAULT_FILE
        self.key_path = key_directory / KEY_FILE
        self._clock_ms = clock_ms
        self._lock = threading.RLock()

    # --- storage -----------------------------------------------------------

    def _read(self) -> VaultDocument:
        return read_document(self.vault_path)

    def _write(self, doc: VaultDocument) -> None:
        _private_dir(self.vault_path.parent)
        write_atomically(self.vault_path, doc.to_bytes())

    def _key(self) -> bytes:
        key = read_key(self.key_path)
        if key is not None:
            return key
        _private_dir(self.key_path.parent)
        key = generate_key()
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        # Records sealed under a lost key can never be opened again.
        if self.vault_path.exists():
            self._write(EMPTY_VAULT)
        return key

    def _swept(
        self, doc: VaultDocument, protect: Collection[str] = ()
    ) -> tuple[VaultDocument, list[str]]:
        now = self._clock_ms()
        expired = [s for s in doc.staged if s.expires_at_ms <= now and s.staging_id not in protect]
        staged = tuple(s for s in doc.staged if s not in expired)
        tombstones = [t for t in doc.tombstones if t.until_ms > now]
        tombstones += [Tombstone(s.staging_id, "expired", now + STAGING_TTL_MS) for s in expired]
        swept = VaultDocument(doc.active, staged, tuple(tombstones[-MAX_TOMBSTONES:]))
        return swept, [s.staging_id for s in expired]

    def _load_swept(self, protect: Collection[str] = ()) -> tuple[VaultDocument, list[str]]:
        doc = self._read()
        swept, expired = self._swept(doc, protect)
        if swept != doc:
            self._write(swept)
        return swept, expired

    # --- queries -----------------------------------------------------------

    def sweep(self, protect: Collection[str] = ()) -> list[str]:
        """Drop expired staging; return their ids."""
        with self._lock:
            return self._load_swept(protect)[1]

    def snapshot(self, protect: Collection[str] = ()) -> VaultDocument:
        with self._lock:
            return self._load_swept(protect)[0]

    def find(
        self, staging_id: str, protect: Collection[str] = ()
    ) -> StagedRecord | Tombstone | None:
        doc = self.snapshot(protect)
        for item in doc.staged:
            if item.staging_id == staging_id:
                return item
        for tombstone in doc.tombstones:
            if tombstone.staging_id == staging_id:
                return tombstone
        return None

    # --- changes -----------------------------------------------------------

    def stage(
        self,
        request: StagingRequest,
        payload: SecretPayload,
        certificate: CertificateInfo | None,
        actor: Actor,
    ) -> StagedRecord:
        with self._lock:
            key = self._key()
            doc, _ = self._load_swept()
            if len(doc.staged) >= MAX_STAGED_CREDENTIALS:
                raise CredentialError(
                    "staging_limit", "too many staged credentials; cancel one or let it expire"
                )
            if doc.active is not None:
                credential_id = doc.active.credential_id
                revisions = [doc.active.revision] + [
                    s.record.revision for s in doc.staged if s.record.credential_id == credential_id
                ]
                revision = max(revisions) + 1
            else:
                credential_id, revision = str(uuid.uuid4()), 1
            nonce, ciphertext = seal(
                key, aad(credential_id, revision, request.binding), payload.to_bytes()
            )
            now = self._clock_ms()
            record = SealedRecord(
                credential_id,
                revision,
                request.binding,
                payload.username is not None,
                certificate,
                now,
                actor.label(),
                nonce,
                ciphertext,
            )
            staged = StagedRecord(
                str(uuid.uuid4()),
                owner_key(actor),
                request.program_md5,
                now + STAGING_TTL_MS,
                record,
            )
            self._write(VaultDocument(doc.active, doc.staged + (staged,), doc.tombstones))
            return staged

    def cancel(self, staging_id: str, actor: Actor) -> StagedRecord:
        with self._lock:
            found = self.find(staging_id)
            if isinstance(found, StagedRecord):
                if found.owner_id != owner_key(actor):
                    raise CredentialError(
                        "staging_owner_mismatch", "only the user who staged it can cancel it"
                    )
                doc = self._read()
                staged = tuple(s for s in doc.staged if s.staging_id != staging_id)
                self._write(VaultDocument(doc.active, staged, doc.tombstones))
                return found
            raise _lookup_error(found)

    def discard(self, staging_id: str) -> bool:
        """Remove a staged credential without trace (failed or abandoned upload)."""
        with self._lock:
            doc = self._read()
            staged = tuple(s for s in doc.staged if s.staging_id != staging_id)
            if len(staged) == len(doc.staged):
                return False
            self._write(VaultDocument(doc.active, staged, doc.tombstones))
            return True

    def remove_active(self) -> SealedRecord:
        with self._lock:
            doc = self._read()
            if doc.active is None:
                raise CredentialError("credential_missing", "no credential is active")
            self._write(VaultDocument(None, doc.staged, doc.tombstones))
            return doc.active

    def promote(self, staging_id: str, actor: Actor) -> tuple[VaultDocument, SealedRecord]:
        """Make a staged credential the active one; return the previous document."""
        with self._lock:
            previous = self._read()
            staged = next((s for s in previous.staged if s.staging_id == staging_id), None)
            if staged is None:
                raise CredentialError("staging_not_found", "the staged credential is gone")
            now = self._clock_ms()
            active = replace(staged.record, updated_at_ms=now, updated_by=actor.label())
            remaining = tuple(s for s in previous.staged if s.staging_id != staging_id)
            tombstones = previous.tombstones + (
                Tombstone(staging_id, "consumed", now + STAGING_TTL_MS),
            )
            self._write(VaultDocument(active, remaining, tombstones[-MAX_TOMBSTONES:]))
            return previous, active

    def restore(self, doc: VaultDocument) -> None:
        with self._lock:
            self._write(doc)


def _lookup_error(found: Tombstone | None) -> CredentialError:
    if found is not None and found.reason == "consumed":
        return CredentialError("staging_consumed", "the staged credential was already used")
    if found is not None:
        return CredentialError("staging_expired", "the staged credential expired")
    return CredentialError("staging_not_found", "no staged credential has this id")
