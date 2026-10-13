# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Decrypt the active credential in a separate, isolated interpreter.

Plugins share one interpreter and its module cache, and a later plugin venv
takes precedence on ``sys.path``, so ``cryptography`` imported inside the
runtime could be another plugin's version. The plugin therefore runs this
file with the MQTT venv interpreter in isolated mode and receives the secret
over a pipe; nothing is written to disk.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# pylint: disable=wrong-import-position
from mqtt_credentials.model import (
    Binding,
    CredentialError,
    SecretPayload,
    VaultCorrupt,
    VaultLocator,
    parse_binding,
)

# pylint: enable=wrong-import-position

UNSEAL_TIMEOUT_S: Final = 10.0
MAX_EXCHANGE_BYTES: Final = 262_144
_HELPER: Final = Path(__file__).resolve()


@dataclass(frozen=True)
class UnsealFailure:
    code: str
    message: str


def _request(locator: VaultLocator, credential_id: str, revision: int, binding: Binding) -> bytes:
    return json.dumps(
        {
            "vault_path": locator.vault_path,
            "key_path": locator.key_path,
            "id": credential_id,
            "revision": revision,
            "binding": binding.to_json(),
        }
    ).encode("utf-8")


def _payload_from(obj: dict[str, object]) -> SecretPayload:
    secret = obj["secret"]
    if not isinstance(secret, dict):
        raise TypeError("secret")

    def optional(name: str) -> bytes | None:
        value = secret[name]
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError(name)
        return base64.b64decode(value.encode("ascii"), validate=True)

    username = secret["username"]
    if username is not None and not isinstance(username, str):
        raise TypeError("username")
    return SecretPayload(username, optional("password"), optional("certificate"), optional("key"))


def request_unseal(
    locator: VaultLocator,
    credential_id: str,
    revision: int,
    binding: Binding,
    timeout_s: float = UNSEAL_TIMEOUT_S,
) -> SecretPayload | UnsealFailure:
    """Run the helper with ``locator.python``; never raises, never logs secrets."""
    try:
        completed = subprocess.run(
            [locator.python, "-I", "-B", str(_HELPER)],
            input=_request(locator, credential_id, revision, binding),
            capture_output=True,
            timeout=timeout_s,
            check=False,
            close_fds=True,
            env={"PATH": os.defpath},
        )
    except (OSError, subprocess.TimeoutExpired):
        return UnsealFailure("internal_error", "the credential helper could not run")
    if completed.returncode != 0 or len(completed.stdout) > MAX_EXCHANGE_BYTES:
        return UnsealFailure("internal_error", "the credential helper failed")
    try:
        obj = json.loads(completed.stdout.decode("utf-8"))
        if not isinstance(obj, dict):
            raise TypeError("response")
        if obj.get("ok") is True:
            return _payload_from(obj)
        code, message = obj["code"], obj["message"]
        if not isinstance(code, str) or not isinstance(message, str):
            raise TypeError("failure")
        return UnsealFailure(code, message)
    except (KeyError, TypeError, ValueError, binascii.Error):
        return UnsealFailure("internal_error", "the credential helper answered malformed data")


def _encode(value: bytes | None) -> str | None:
    return None if value is None else base64.b64encode(value).decode("ascii")


def _serve(raw: bytes) -> dict[str, object]:
    # pylint: disable=import-outside-toplevel
    from mqtt_credentials.material import CryptoUnavailable
    from mqtt_credentials.vault import unseal_active

    # pylint: enable=import-outside-toplevel
    try:
        request = json.loads(raw.decode("utf-8"))
        vault_path = Path(request["vault_path"])
        key_path = Path(request["key_path"])
        credential_id = request["id"]
        revision = request["revision"]
        if not isinstance(credential_id, str) or not isinstance(revision, int):
            raise TypeError("reference")
        binding = parse_binding(request["binding"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "code": "internal_error", "message": "malformed helper request"}
    try:
        payload = unseal_active(vault_path, key_path, credential_id, revision, binding)
    except CredentialError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message}
    except (VaultCorrupt, CryptoUnavailable, OSError):
        return {"ok": False, "code": "internal_error", "message": "the vault cannot be read"}
    return {
        "ok": True,
        "secret": {
            "username": payload.username,
            "password": _encode(payload.password),
            "certificate": _encode(payload.certificate_pem),
            "key": _encode(payload.key_pem),
        },
    }


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_EXCHANGE_BYTES + 1)
    if len(raw) > MAX_EXCHANGE_BYTES:
        response: dict[str, object] = {
            "ok": False,
            "code": "internal_error",
            "message": "helper request too large",
        }
    else:
        response = _serve(raw)
    sys.stdout.buffer.write(json.dumps(response).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
