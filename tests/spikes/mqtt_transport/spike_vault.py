# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Evidence for the at-rest protection primitive chosen for the runtime credential vault."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import cryptography
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MARKER = b"MARKER-PW-vault-7f3c"


def aad(record_id: str, revision: int, binding: dict[str, object]) -> bytes:
    return json.dumps(
        {"id": record_id, "revision": revision, "binding": binding},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def create_key(path: Path) -> bytes:
    key = AESGCM.generate_key(bit_length=256)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


BINDING = {
    "host": "broker.example.net",
    "port": 8883,
    "tls_enabled": True,
    "client_id": "plc",
    "method": "username_password",
}


def test_pinned_version() -> None:
    assert cryptography.__version__ == "50.0.1"


def test_round_trip_and_key_file_mode() -> None:
    with tempfile.TemporaryDirectory() as d:
        key_path = Path(d) / "vault.key"
        key = create_key(key_path)
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        nonce = os.urandom(12)
        blob = AESGCM(key).encrypt(nonce, MARKER, aad("id-1", 1, BINDING))
        assert MARKER not in blob
        assert AESGCM(key).decrypt(nonce, blob, aad("id-1", 1, BINDING)) == MARKER


@pytest.mark.parametrize("mutation", ["ciphertext", "revision", "binding-host", "other-key"])
def test_tamper_and_rebinding_are_detected(mutation: str) -> None:
    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    blob = bytearray(AESGCM(key).encrypt(nonce, MARKER, aad("id-1", 1, BINDING)))
    use_key, use_aad = key, aad("id-1", 1, BINDING)
    if mutation == "ciphertext":
        blob[0] ^= 1
    elif mutation == "revision":
        use_aad = aad("id-1", 2, BINDING)
    elif mutation == "binding-host":
        use_aad = aad("id-1", 1, dict(BINDING, host="attacker.example"))
    else:
        use_key = AESGCM.generate_key(bit_length=256)
    with pytest.raises(InvalidTag):
        AESGCM(use_key).decrypt(nonce, bytes(blob), use_aad)
