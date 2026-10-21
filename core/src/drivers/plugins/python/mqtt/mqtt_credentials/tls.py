# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""TLS client context for the broker connection. Standard library only.

Verification is always on (TLS 1.2+, chain, validity, hostname); there is no
parameter that disables it. ``ssl`` loads a client identity only from file
paths, so the PEM goes through an anonymous memfd where the kernel has one,
otherwise through 0600 files in a private 0700 directory removed right after
loading.
"""

from __future__ import annotations

import os
import shutil
import ssl
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field

from contract.runtime_config import TlsSettings

_SHM_DIR = "/dev/shm"


class TlsMaterialError(ValueError):
    """CA or client identity rejected by the TLS library; ``part`` names which."""

    def __init__(self, part: str) -> None:
        super().__init__(f"TLS {part} rejected")
        self.part = part


@dataclass(frozen=True)
class ClientIdentity:
    certificate_chain_pem: bytes = field(repr=False)
    key_pem: bytes = field(repr=False)


@contextmanager
def _memfd_paths(certificate: bytes, key: bytes) -> Iterator[tuple[str, str]]:
    # pylint: disable=no-member
    cert_fd = os.memfd_create("mqtt-client-cert", os.MFD_CLOEXEC)
    try:
        key_fd = os.memfd_create("mqtt-client-key", os.MFD_CLOEXEC)
        try:
            os.write(cert_fd, certificate)
            os.write(key_fd, key)
            yield f"/proc/self/fd/{cert_fd}", f"/proc/self/fd/{key_fd}"
        finally:
            os.close(key_fd)
    finally:
        os.close(cert_fd)


def _write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


@contextmanager
def _private_file_paths(certificate: bytes, key: bytes) -> Iterator[tuple[str, str]]:
    parent = _SHM_DIR if os.path.isdir(_SHM_DIR) else None
    directory = tempfile.mkdtemp(prefix="mqtt-identity-", dir=parent)
    try:
        cert_path = os.path.join(directory, "certificate.pem")
        key_path = os.path.join(directory, "key.pem")
        _write_private(cert_path, certificate)
        _write_private(key_path, key)
        yield cert_path, key_path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def identity_paths(identity: ClientIdentity) -> AbstractContextManager[tuple[str, str]]:
    """Short-lived paths that hold the identity, removed when the context exits."""
    if hasattr(os, "memfd_create"):
        return _memfd_paths(identity.certificate_chain_pem, identity.key_pem)
    return _private_file_paths(identity.certificate_chain_pem, identity.key_pem)


def load_identity(context: ssl.SSLContext, identity: ClientIdentity) -> None:
    """Load the client chain and key; OpenSSL checks that they correspond."""
    with identity_paths(identity) as (cert_path, key_path):
        try:
            context.load_cert_chain(cert_path, key_path)
        except ssl.SSLError as exc:
            raise TlsMaterialError("identity") from exc


def build_tls_context(tls: TlsSettings, identity: ClientIdentity | None) -> ssl.SSLContext:
    """Verifying client context for an enabled TLS configuration."""
    if not tls.enabled or tls.trust is None:
        raise ValueError("a TLS context needs TLS enabled with a trust source")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    if tls.trust.source == "system":
        context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    else:
        try:
            context.load_verify_locations(cadata=tls.trust.ca_pem)
        except (ssl.SSLError, ValueError) as exc:
            raise TlsMaterialError("ca") from exc
    if identity is not None:
        load_identity(context, identity)
    return context


CA_POINTER = "/connection/tls/trust/ca_pem"
CA_MESSAGE = "the CA certificate cannot be loaded"


def check_ca_pem(ca_pem: str) -> bool:
    """True when every PEM block parses as a trust anchor."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        context.load_verify_locations(cadata=ca_pem)
    except (ssl.SSLError, ValueError):
        return False
    return True
