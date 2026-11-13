# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Commercial runtime licences: storage and the report the editor verifies.

A licence is a JSON file signed by Autonomy Edge for one hardware ID v2 (spec:
docs/HARDWARE_ID.md). The runtime stores licences and reports them with its
hardware ID; it never checks a signature, never limits anything and never
contacts Edge.

Two layouts:

  * On its own hardware the runtime stores uploaded licences in ``licenses/``
    under the persistent data directory and reports all of them.
  * In a vPLC (hardware ID injected by orchestrator-agent) uploads go to the
    folder the agent collects host licences from, and only the licence in the
    seat file the agent writes into the container is reported.
"""

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path

from webserver import hardware_id
from webserver.config import PERSISTENT_DATA_DIR
from webserver.logger import get_logger
from webserver.vpp_license_debug import _write_license_atomically

logger, _ = get_logger("logger", use_buffer=True)

PRODUCT = "com.autonomy.openplc-runtime"
FORMAT = 1
SEAT_FILE_FORMAT = 1
MAX_SEATS = 1000
MAX_LICENSE_BYTES = 4096
MAX_SEAT_FILE_BYTES = 8192
MAX_STORED_LICENSES = 100

SEAT_FILE_PATH = Path("/etc/openplc-runtime/runtime.license")
VPLC_LICENSES_DIR = Path("/var/lib/openplc-runtime/licenses")
DEVICE_LICENSES_DIR = PERSISTENT_DATA_DIR / "licenses"

_HARDWARE_ID_RE = re.compile(r"^hw2:[0-9a-f]{64}$")
_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SIG_RE = re.compile(r"^[A-Za-z0-9+/]{86}==$")
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
    if data["fmt"] != FORMAT or data["product"] != PRODUCT:
        return None
    hw = data["hardware_id"]
    order_id = data["order_id"]
    key_id = data["key_id"]
    sig = data["sig"]
    if not isinstance(hw, str) or not _HARDWARE_ID_RE.match(hw):
        return None
    if not _is_int(data["seats"]) or not 1 <= data["seats"] <= MAX_SEATS:
        return None
    if not isinstance(order_id, str) or not _ORDER_ID_RE.match(order_id):
        return None
    if not isinstance(data["issued_at"], str) or not data["issued_at"]:
        return None
    if not _is_int(key_id) or key_id < 0:
        return None
    if not isinstance(sig, str) or not _SIG_RE.match(sig):
        return None
    payload = {key: value for key, value in data.items() if key != "sig"}
    return RuntimeLicense(
        hardware_id=hw,
        order_id=order_id,
        key_id=key_id,
        signature=base64.b64decode(sig),
        payload=payload,
    )


def parse_license_bytes(raw: bytes) -> RuntimeLicense | None:
    try:
        return parse_license(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return None


def _in_vplc(record: hardware_id.HardwareId) -> bool:
    return record.source == "orchestrator"


def licenses_dir() -> Path:
    """Where uploads go: the agent's intake folder in a vPLC, else the persistent folder."""
    record = hardware_id.current()
    return VPLC_LICENSES_DIR if record is not None and _in_vplc(record) else DEVICE_LICENSES_DIR


def store(raw: bytes) -> StoreResult:
    """Keep an uploaded licence as ``<order_id>.license``; only its shape is checked."""
    if len(raw) > MAX_LICENSE_BYTES:
        return StoreResult(False, f"Licence is larger than {MAX_LICENSE_BYTES} bytes")
    lic = parse_license_bytes(raw)
    if lic is None:
        return StoreResult(False, "Not an OpenPLC Runtime licence")
    record = hardware_id.current()
    if record is None:
        return StoreResult(False, "This device has no hardware ID")
    target_dir = VPLC_LICENSES_DIR if _in_vplc(record) else DEVICE_LICENSES_DIR
    try:
        _write_license_atomically(str(target_dir / f"{lic.order_id}.license"), lic.to_bytes())
    except OSError as exc:
        logger.error("Could not store runtime licence %s: %s", lic.order_id, exc)
        return StoreResult(False, "Licence could not be stored", internal=True)
    logger.info("Stored runtime licence %s for %s", lic.order_id, lic.hardware_id)
    return StoreResult(True, order_id=lic.order_id)


def _read_small(path: Path, limit: int) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return None
    return raw if len(raw) <= limit else None


def _seat_license(raw: bytes) -> RuntimeLicense | None:
    """The licence inside a seat file ``{fmt, vplc, license}``; None when empty or malformed."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("fmt") != SEAT_FILE_FORMAT:
        return None
    if not isinstance(data.get("vplc"), str) or not data["vplc"]:
        return None
    return parse_license(data.get("license"))


def _stored_licenses() -> list[RuntimeLicense]:
    try:
        names = sorted(p for p in DEVICE_LICENSES_DIR.iterdir() if p.suffix == ".license")
    except OSError:
        return []
    found = []
    for path in names[:MAX_STORED_LICENSES]:
        raw = _read_small(path, MAX_LICENSE_BYTES)
        lic = parse_license_bytes(raw) if raw is not None else None
        if lic is not None:
            found.append(lic)
    return found


def reported_licenses() -> list[RuntimeLicense]:
    """The licences the editor checks: every stored one, or in a vPLC only the seat file's."""
    record = hardware_id.current()
    if record is not None and _in_vplc(record):
        raw = _read_small(SEAT_FILE_PATH, MAX_SEAT_FILE_BYTES)
        lic = _seat_license(raw) if raw else None
        return [lic] if lic is not None else []
    return _stored_licenses()


def report() -> dict[str, object]:
    """``GET /api/runtime-license``: the hardware ID and the licences, as stored."""
    record = hardware_id.current()
    licenses = [json.loads(lic.to_bytes()) for lic in reported_licenses()]
    if record is None:
        return {"hardwareId": None, "source": None, "licenses": licenses}
    return {"hardwareId": record.value, "source": record.source, "licenses": licenses}
