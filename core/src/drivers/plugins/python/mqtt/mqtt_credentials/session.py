# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Connection material of one plugin generation: credential and TLS context.

Built when a generation is activated, so a missing credential, a credential
bound to another broker or an identity the TLS library refuses keeps the
generation from starting. Standard library only (decryption is delegated to
``unseal``).
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from contract.runtime_config import (
    NON_CONFIG_ERROR_CATEGORY,
    ConfigError,
    MqttRuntimeConfig,
)

from .model import MAX_LOCATOR_BYTES, Binding, SecretPayload, VaultCorrupt, VaultLocator
from .tls import (
    CA_MESSAGE,
    CA_POINTER,
    ClientIdentity,
    TlsMaterialError,
    build_tls_context,
)
from .unseal import UnsealFailure, request_unseal

LOCATOR_NAME: Final = "mqtt.vault.json"
_REF_POINTER: Final = "/auth/credential_ref"

Unsealer = Callable[[VaultLocator, str, int, Binding], SecretPayload | UnsealFailure]


@dataclass(frozen=True)
class SessionMaterial:
    """What the transport needs to connect; secrets are excluded from repr."""

    username: str | None = field(repr=False)
    password: bytes | None = field(repr=False)
    tls_context: ssl.SSLContext | None


def locator_path(config_path: str) -> Path:
    return Path(config_path).with_name(LOCATOR_NAME)


def _credential_error(code: str, message: str) -> ConfigError:
    known = code if code in NON_CONFIG_ERROR_CATEGORY else "internal_error"
    return ConfigError("credential", known, _REF_POINTER, message)


def _read_locator(config_path: str) -> VaultLocator | ConfigError:
    try:
        with open(locator_path(config_path), "rb") as handle:
            raw = handle.read(MAX_LOCATOR_BYTES + 1)
        return VaultLocator.from_bytes(raw)
    except (OSError, VaultCorrupt):
        return _credential_error(
            "credential_missing", "the credential vault is not configured for this integration"
        )


def prepare_session(
    config: MqttRuntimeConfig, config_path: str, unseal: Unsealer = request_unseal
) -> SessionMaterial | ConfigError:
    binding = Binding.from_config(config)
    payload: SecretPayload | None = None
    ref = config.auth.credential_ref
    if binding is not None and ref is not None:
        locator = _read_locator(config_path)
        if isinstance(locator, ConfigError):
            return locator
        result = unseal(locator, ref.id, ref.revision, binding)
        if isinstance(result, UnsealFailure):
            return _credential_error(result.code, result.message)
        payload = result
    context = None
    if config.connection.tls.enabled:
        identity = None
        if payload is not None and binding is not None and binding.method == "client_certificate":
            if payload.certificate_pem is None or payload.key_pem is None:
                return _credential_error("internal_error", "the stored identity is incomplete")
            identity = ClientIdentity(payload.certificate_pem, payload.key_pem)
        try:
            context = build_tls_context(config.connection.tls, identity)
        except TlsMaterialError as exc:
            if exc.part == "ca":
                return ConfigError("credential", "config_schema", CA_POINTER, CA_MESSAGE)
            return _credential_error(
                "client_key_mismatch", "the TLS library refused the client identity"
            )
    if payload is not None and binding is not None and binding.method == "username_password":
        return SessionMaterial(payload.username, payload.password, context)
    return SessionMaterial(None, None, context)
