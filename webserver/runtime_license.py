# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Commercial runtime licences: storage, signature check and the status the API reports.

A licence is a JSON file signed by Autonomy Edge for one hardware ID v2 (spec:
docs/HARDWARE_ID.md). The runtime only reports whether it holds a valid one; it
never limits anything and never contacts Edge.

Two layouts:

  * On its own hardware the runtime stores uploaded licences in ``licenses/``
    under the persistent data directory, and any that verifies for this device
    makes the status ``commercial``.
  * In a vPLC (hardware ID injected by orchestrator-agent) uploads go to the
    folder the agent collects host licences from, and only the seat file the
    agent writes into the container counts.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
import threading
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
VERIFY_TIMEOUT_S = 10

COMMERCIAL = "commercial"
GPL = "gpl"

SEAT_FILE_PATH = Path("/etc/openplc-runtime/runtime.license")
VPLC_LICENSES_DIR = Path("/var/lib/openplc-runtime/licenses")
DEVICE_LICENSES_DIR = PERSISTENT_DATA_DIR / "licenses"

# Production public keys by key_id: raw P-256 x||y, lowercase hex.
TRUSTED_KEYS: dict[int, str] = {}

# "<key_id>:<x||y hex>"; trusted in addition to TRUSTED_KEYS. Never set in production.
DEV_KEY_ENV = "OPENPLC_RUNTIME_LICENSE_DEV_KEY"

# DER prefix of a P-256 SubjectPublicKeyInfo holding an uncompressed point.
_P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")

_HARDWARE_ID_RE = re.compile(r"^hw2:[0-9a-f]{64}$")
_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SIG_RE = re.compile(r"^[A-Za-z0-9+/]{86}==$")
_RAW_KEY_RE = re.compile(r"^[0-9a-f]{128}$")
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


def canonical_payload(lic: RuntimeLicense) -> bytes:
    """The signed bytes: payload without ``sig``, keys sorted, no whitespace, UTF-8."""
    return json.dumps(
        lic.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def trusted_keys(key_id: int) -> list[str]:
    """Raw public keys accepted for ``key_id``: the built-in one, then the development key."""
    keys = [TRUSTED_KEYS[key_id]] if key_id in TRUSTED_KEYS else []
    dev = os.environ.get(DEV_KEY_ENV, "").strip()
    if dev:
        dev_id, _, dev_key = dev.partition(":")
        dev_key = dev_key.strip().lower()
        if dev_id.strip().isdigit() and _RAW_KEY_RE.match(dev_key):
            if int(dev_id) == key_id:
                keys.append(dev_key)
        else:
            logger.warning("Ignoring malformed %s", DEV_KEY_ENV)
    return keys


def _der_integer(value: bytes) -> bytes:
    stripped = value.lstrip(b"\x00") or b"\x00"
    if stripped[0] & 0x80:
        stripped = b"\x00" + stripped
    return b"\x02" + bytes([len(stripped)]) + stripped


def _der_signature(raw: bytes) -> bytes:
    """ECDSA-Sig-Value DER from raw r||s (32 bytes each)."""
    body = _der_integer(raw[:32]) + _der_integer(raw[32:])
    return b"\x30" + bytes([len(body)]) + body


def _public_key_pem(raw_hex: str) -> bytes:
    der = _P256_SPKI_PREFIX + b"\x04" + bytes.fromhex(raw_hex)
    body = base64.encodebytes(der).decode("ascii")
    return f"-----BEGIN PUBLIC KEY-----\n{body}-----END PUBLIC KEY-----\n".encode("ascii")


def verify_signature(message: bytes, signature: bytes, raw_key_hex: str) -> bool:
    """ECDSA P-256 / SHA-256 check through the ``openssl`` command line."""
    if len(signature) != 64 or not _RAW_KEY_RE.match(raw_key_hex):
        return False
    with tempfile.TemporaryDirectory(prefix="openplc-license-") as work:
        key_path = os.path.join(work, "key.pem")
        sig_path = os.path.join(work, "sig.der")
        msg_path = os.path.join(work, "payload")
        with open(key_path, "wb") as handle:
            handle.write(_public_key_pem(raw_key_hex))
        with open(sig_path, "wb") as handle:
            handle.write(_der_signature(signature))
        with open(msg_path, "wb") as handle:
            handle.write(message)
        try:
            result = subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-verify",
                    key_path,
                    "-signature",
                    sig_path,
                    msg_path,
                ],
                capture_output=True,
                timeout=VERIFY_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Runtime licence signature check could not run: %s", exc)
            return False
    return result.returncode == 0


def verifies_for(lic: RuntimeLicense, device_hardware_id: str) -> bool:
    """True when the licence names this hardware ID and a trusted key signed it."""
    if lic.hardware_id != device_hardware_id:
        return False
    message = canonical_payload(lic)
    return any(verify_signature(message, lic.signature, key) for key in trusted_keys(lic.key_id))


def _in_vplc(record: hardware_id.HardwareId) -> bool:
    return record.source == "orchestrator"


def licenses_dir() -> Path:
    """Where uploads go: the agent's intake folder in a vPLC, else the persistent folder."""
    record = hardware_id.current()
    return VPLC_LICENSES_DIR if record is not None and _in_vplc(record) else DEVICE_LICENSES_DIR


def store(raw: bytes) -> StoreResult:
    """Verify an uploaded licence and keep it as ``<order_id>.license``."""
    if len(raw) > MAX_LICENSE_BYTES:
        return StoreResult(False, f"Licence is larger than {MAX_LICENSE_BYTES} bytes")
    lic = parse_license_bytes(raw)
    if lic is None:
        return StoreResult(False, "Not an OpenPLC Runtime licence")
    record = hardware_id.current()
    if record is None:
        return StoreResult(False, "This device has no hardware ID")
    if lic.hardware_id != record.value:
        return StoreResult(False, "Licence is for another hardware ID")
    if not verifies_for(lic, record.value):
        return StoreResult(False, "Licence signature does not verify")
    target_dir = VPLC_LICENSES_DIR if _in_vplc(record) else DEVICE_LICENSES_DIR
    try:
        _write_license_atomically(str(target_dir / f"{lic.order_id}.license"), lic.to_bytes())
    except OSError as exc:
        logger.error("Could not store runtime licence %s: %s", lic.order_id, exc)
        return StoreResult(False, "Licence could not be stored", internal=True)
    logger.info("Stored runtime licence %s for %s", lic.order_id, record.value)
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


def compute_status() -> str:
    """``commercial`` when this runtime holds a licence that verifies for its hardware ID."""
    record = hardware_id.current()
    if record is None:
        return GPL
    if _in_vplc(record):
        raw = _read_small(SEAT_FILE_PATH, MAX_SEAT_FILE_BYTES)
        lic = _seat_license(raw) if raw else None
        candidates = [lic] if lic is not None else []
    else:
        candidates = _stored_licenses()
    return COMMERCIAL if any(verifies_for(c, record.value) for c in candidates) else GPL


def _stat_key(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _inputs_fingerprint() -> tuple[tuple[int, int, int] | None, ...]:
    return tuple(
        _stat_key(p)
        for p in (
            hardware_id.INJECTED_PATH,
            hardware_id.STORED_PATH,
            SEAT_FILE_PATH,
            DEVICE_LICENSES_DIR,
        )
    )


_cache_lock = threading.Lock()
_cache: dict[str, object] = {}


def status() -> str:
    """The cached status, recomputed only when a licence or hardware ID file changed."""
    fingerprint = _inputs_fingerprint()
    with _cache_lock:
        if _cache.get("fingerprint") == fingerprint and isinstance(_cache.get("status"), str):
            return str(_cache["status"])
        value = compute_status()
        _cache["fingerprint"] = fingerprint
        _cache["status"] = value
        return value


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def log_startup_status() -> None:
    value = status()
    if value == COMMERCIAL:
        logger.info("OpenPLC Runtime licence: commercial")
    else:
        logger.info("OpenPLC Runtime licence: GPLv3 (no commercial licence found)")
