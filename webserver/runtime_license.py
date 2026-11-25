# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Commercial runtime licences: storage and the report the editor verifies.

A licence is a JSON file signed by Autonomy Edge for one hardware ID v2 (spec:
docs/HARDWARE_ID.md). The runtime stores one licence and reports it with its
hardware ID; it never checks a signature, never limits anything and never
contacts Edge.

The runtime keeps one licence, ``runtime.license`` in the persistent data
directory, on a device and in a vPLC alike; each upload replaces it.
"""

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path

from webserver import hardware_id
from webserver.atomic_file import write_file_atomically
from webserver.config import PERSISTENT_DATA_DIR
from webserver.logger import get_logger

logger, _ = get_logger("logger", use_buffer=True)

PRODUCT = "com.autonomy.openplc-runtime"
FORMAT = 1
MAX_SEATS = 1000
MAX_LICENSE_BYTES = 4096
MAX_KEY_ID = 255

LICENSE_PATH = PERSISTENT_DATA_DIR / "runtime.license"

_HARDWARE_ID_RE = re.compile(r"hw2:[0-9a-f]{64}")
_ORDER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SIG_RE = re.compile(r"[A-Za-z0-9+/]{86}==")
_FIELDS = frozenset(
    ("fmt", "product", "hardware_id", "seats", "order_id", "issued_at", "key_id", "sig")
)


@dataclass(frozen=True)
class RuntimeLicense:
    """One licence file, with the fields the signature covers kept as issued."""

    hardware_id: str
    order_id: str
    key_id: int
    signature: bytes
    payload: dict[str, object]

    def to_bytes(self) -> bytes:
        """The file as stored: the payload and ``sig``, keys sorted, no whitespace."""
        content = dict(self.payload)
        content["sig"] = base64.b64encode(self.signature).decode("ascii")
        return json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class StoreResult:
    ok: bool
    error: str = ""
    order_id: str = ""
    internal: bool = False


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_license(data: object) -> RuntimeLicense | None:
    """A licence from its decoded JSON; None unless it has exactly the runtime licence shape."""
    if not isinstance(data, dict) or set(data) != _FIELDS:
        return None
    if not _is_int(data["fmt"]) or data["fmt"] != FORMAT or data["product"] != PRODUCT:
        return None
    hw = data["hardware_id"]
    order_id = data["order_id"]
    key_id = data["key_id"]
    sig = data["sig"]
    if not isinstance(hw, str) or not _HARDWARE_ID_RE.fullmatch(hw):
        return None
    if not _is_int(data["seats"]) or not 1 <= data["seats"] <= MAX_SEATS:
        return None
    if not isinstance(order_id, str) or not _ORDER_ID_RE.fullmatch(order_id):
        return None
    if not isinstance(data["issued_at"], str) or not data["issued_at"]:
        return None
    if not _is_int(key_id) or not 0 <= key_id <= MAX_KEY_ID:
        return None
    if not isinstance(sig, str) or not _SIG_RE.fullmatch(sig):
        return None
    payload = {key: value for key, value in data.items() if key != "sig"}
    return RuntimeLicense(
        hardware_id=hw,
        order_id=order_id,
        key_id=key_id,
        signature=base64.b64decode(sig, validate=True),
        payload=payload,
    )


def parse_license_bytes(raw: bytes) -> RuntimeLicense | None:
    try:
        return parse_license(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return None


def store(raw: bytes) -> StoreResult:
    """Replace the stored licence with an uploaded one; only its shape is checked."""
    if len(raw) > MAX_LICENSE_BYTES:
        return StoreResult(False, f"Licence is larger than {MAX_LICENSE_BYTES} bytes")
    lic = parse_license_bytes(raw)
    if lic is None:
        return StoreResult(False, "Not an OpenPLC Runtime licence")
    try:
        write_file_atomically(LICENSE_PATH, lic.to_bytes())
    except OSError as exc:
        logger.error("Could not store runtime licence %s: %s", lic.order_id, exc)
        return StoreResult(False, "Licence could not be stored", internal=True)
    logger.info("Stored runtime licence %s for %s", lic.order_id, lic.hardware_id)
    return StoreResult(True, order_id=lic.order_id)


def remove() -> bool:
    """Delete the licence file; True only when it held a licence with the licence shape.

    An empty or malformed file is deleted too and gives False. Raises OSError on failure.
    """
    held = bool(reported_licenses())
    try:
        LICENSE_PATH.unlink()
    except FileNotFoundError:
        return False
    if held:
        logger.info("Removed the stored runtime licence")
    return held


def _read_small(path: Path, limit: int) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return None
    return raw if len(raw) <= limit else None


def reported_licenses() -> list[RuntimeLicense]:
    """The stored licence, when there is one with the licence shape."""
    raw = _read_small(LICENSE_PATH, MAX_LICENSE_BYTES)
    lic = parse_license_bytes(raw) if raw else None
    return [lic] if lic is not None else []


def report() -> dict[str, object]:
    """``GET /api/runtime-license``: the hardware ID and the licence, as stored."""
    record = hardware_id.current()
    licenses = [json.loads(lic.to_bytes()) for lic in reported_licenses()]
    return {"hardwareId": record.value if record is not None else None, "licenses": licenses}
