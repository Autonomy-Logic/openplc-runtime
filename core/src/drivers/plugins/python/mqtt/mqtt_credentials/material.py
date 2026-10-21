# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""AES-256-GCM sealing and client identity validation (``cryptography``).

``cryptography`` is imported on first use only and must be the pinned,
reviewed version; any other version, or none, makes secret-based methods
unavailable instead of running on an untested implementation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Final

from .model import (
    KEY_BYTES,
    MAX_SUBJECT_CHARS,
    NONCE_BYTES,
    CertificateInfo,
    CredentialError,
    clean_label,
)

CRYPTOGRAPHY_PINNED: Final = "50.0.1"
MIN_RSA_BITS: Final = 2048


class CryptoUnavailable(RuntimeError):
    """The pinned ``cryptography`` is not importable in this interpreter."""


class SealBroken(Exception):
    """Ciphertext, associated data or key do not match (tamper or rebinding)."""


@lru_cache(maxsize=1)
def crypto_available() -> bool:
    try:
        import cryptography  # pylint: disable=import-outside-toplevel
        from cryptography.hazmat.primitives.ciphers.aead import (  # noqa: F401  pylint: disable=import-outside-toplevel,unused-import
            AESGCM,
        )
    except ImportError:
        return False
    return cryptography.__version__ == CRYPTOGRAPHY_PINNED


def _require() -> None:
    if not crypto_available():
        raise CryptoUnavailable("the pinned cryptography package is not available")


def generate_key() -> bytes:
    _require()
    return os.urandom(KEY_BYTES)


def seal(key: bytes, associated: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt with a fresh random 96-bit nonce."""
    _require()
    # pylint: disable-next=import-outside-toplevel
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, associated)


def open_sealed(key: bytes, nonce: bytes, ciphertext: bytes, associated: bytes) -> bytes:
    _require()
    # pylint: disable=import-outside-toplevel
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    # pylint: enable=import-outside-toplevel

    try:
        return AESGCM(key).decrypt(nonce, ciphertext, associated)
    except InvalidTag as exc:
        raise SealBroken("sealed credential does not authenticate") from exc


@dataclass(frozen=True)
class ValidatedIdentity:
    certificate_chain_pem: bytes = field(repr=False)
    key_pem: bytes = field(repr=False)
    info: CertificateInfo


def _encrypted_pem(key_pem: bytes) -> bool:
    return b"ENCRYPTED PRIVATE KEY" in key_pem or b"Proc-Type: 4,ENCRYPTED" in key_pem


def validate_client_identity(  # pylint: disable=too-many-locals
    certificate_pem: bytes, key_pem: bytes, passphrase: bytes | None, now_ms: int
) -> ValidatedIdentity:
    """Parse, check validity and key correspondence; return normalized PEM.

    The key is returned unencrypted (PKCS#8): it is only ever stored sealed,
    so the passphrase is not kept.
    """
    _require()
    # pylint: disable=import-outside-toplevel
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa

    # pylint: enable=import-outside-toplevel
    try:
        chain = x509.load_pem_x509_certificates(certificate_pem)
    except ValueError as exc:
        raise CredentialError(
            "credential_invalid_format", "the client certificate is not a PEM certificate"
        ) from exc
    leaf = chain[0]
    not_before_ms = int(leaf.not_valid_before_utc.timestamp() * 1000)
    not_after_ms = int(leaf.not_valid_after_utc.timestamp() * 1000)
    if now_ms > not_after_ms:
        raise CredentialError("client_certificate_expired", "the client certificate has expired")
    if now_ms < not_before_ms:
        raise CredentialError(
            "credential_invalid_format", "the client certificate is not valid yet"
        )
    try:
        key = serialization.load_pem_private_key(key_pem, password=passphrase)
    except TypeError as exc:
        raise CredentialError(
            "client_key_passphrase_invalid",
            "the key passphrase is missing, or given for an unencrypted key",
        ) from exc
    except ValueError as exc:
        if passphrase is not None and _encrypted_pem(key_pem):
            raise CredentialError(
                "client_key_passphrase_invalid", "the key passphrase is not correct"
            ) from exc
        raise CredentialError(
            "credential_invalid_format", "the client key is not a PEM private key"
        ) from exc
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < MIN_RSA_BITS:
            raise CredentialError(
                "credential_invalid_format", "RSA client keys must have at least 2048 bits"
            )
    elif not isinstance(
        key, (ec.EllipticCurvePrivateKey, ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)
    ):
        raise CredentialError("credential_invalid_format", "unsupported client key type")
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    der = serialization.Encoding.DER
    if leaf.public_key().public_bytes(der, spki) != key.public_key().public_bytes(der, spki):
        raise CredentialError(
            "client_key_mismatch", "the client key does not match the client certificate"
        )
    normalized_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    normalized_chain = b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in chain)
    info = CertificateInfo(
        leaf.fingerprint(hashes.SHA256()).hex(),
        clean_label(leaf.subject.rfc4514_string(), MAX_SUBJECT_CHARS),
        not_after_ms,
    )
    return ValidatedIdentity(normalized_chain, normalized_key, info)
