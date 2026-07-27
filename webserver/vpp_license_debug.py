"""VPP device-license debug function codes (0x48/0x49/0x4A), resolved at the
webserver level (D70a).

On runtime-v4 the licensing anchor and the license blob are HOST FILES
(``/proc/device-tree/serial-number`` and ``conf/<plugin>.license``), not plugin
memory, so these function codes are answered here in Python instead of the
realtime C core: it needs no core rebuild, works while the PLC is stopped
(resolves the chicken-and-egg of activating before a program runs), and reuses
the same license-path derivation as the bundle delivery in
``apply_vpp_plugin_conf``.

This lets the editor speak ONE license protocol over any transport (D70c): the
exact Modbus PDU it uses on Arduino, carried by the debug WebSocket. The frame is
raw Modbus PDU (no MBAP, no CRC), byte-identical to the editor's ``modbus-pdu.ts``:

  0x48 get-board-id : req [0x48]                 resp [0x48][status][id_len:u8][id...]
  0x49 write-license: req [0x49][len:u16BE][blob] resp [0x49][status]
  0x4A read-license : req [0x4A]                 resp [0x4A][status][len:u16BE][blob]

Anchor bytes are returned RAW (ASCII, trailing NUL/whitespace stripped) so the
editor derives the SAME device_id the .so does (D70d); no hex-decoding.
"""
import os
from typing import Optional

from webserver.plugin_config_model import PluginsConfiguration

# License function codes (mirror simulator/types.ts + firmware modbus_types.h).
FC_GET_BOARD_ID = 0x48
FC_WRITE_LICENSE = 0x49
FC_READ_LICENSE = 0x4A
_LICENSE_FCS = (FC_GET_BOARD_ID, FC_WRITE_LICENSE, FC_READ_LICENSE)

# Status bytes (shared with the Arduino firmware / editor).
ST_SUCCESS = 0x7E
ST_LIC_EMPTY = 0x83
ST_LIC_CORRUPT = 0x84
ST_LIC_UNSUPPORTED = 0x85

ANCHOR_PATH = "/proc/device-tree/serial-number"
VPP_CONF = "vpp_plugins.conf"
LIC_BLOB_SIZE = 98


def is_license_command(command_hex: str) -> bool:
    """True when the PDU's first byte is a license function code."""
    data = _bytes_from_hex(command_hex)
    return bool(data) and data[0] in _LICENSE_FCS


def _bytes_from_hex(command_hex: str) -> bytes:
    try:
        return bytes(int(tok, 16) for tok in command_hex.split())
    except ValueError:
        return b""


def _hex_from_bytes(data: bytes) -> str:
    # Uppercase 2-digit, space-joined -- the format the editor's
    # hexSpacedToBytes/bytesToHexSpaced round-trips.
    return " ".join(f"{b:02X}" for b in data)


def _read_anchor() -> bytes:
    try:
        with open(ANCHOR_PATH, "rb") as handle:
            raw = handle.read()
    except OSError:
        return b""
    # Strip trailing NUL / whitespace -- MUST match derive in rpi_plugin.c
    # (the device-tree serial is NUL-terminated).
    return raw.rstrip(b"\x00\r\n\t ")


def derive_license_path(config_path: str) -> str:
    """The ``.license`` sibling of a plugin's config_path: drop a trailing
    ``.json``, append ``.license``. MUST mirror ``derive_license_path()`` in
    the plugin's C source (rpi_plugin.c) exactly, or the .so reads the wrong
    path and falls back to demo.
    """
    base = config_path[:-5] if config_path.endswith(".json") else config_path
    return base + ".license"


def resolve_license_path(config_path: str, runtime_root: Optional[str] = None) -> Optional[str]:
    """``derive_license_path()`` plus the anti-traversal guard: never resolve
    to a path outside the runtime root, even if a forged ``vpp_plugins.conf``
    carries an escaping ``config_path``. 0x49 writes 98 bytes as root, and
    ``apply_vpp_plugin_conf`` copies an upload-supplied blob, so both refuse an
    escaping target rather than trust the conf. Returns None when it escapes.

    A bare ``.startswith(root)`` is not enough: a sibling directory that merely
    shares the root as a *string* prefix (e.g. root ``/opt/runtime`` vs. an
    escaping ``/opt/runtime-evil/x``) would wrongly pass. Anchoring on
    ``root + os.sep`` requires the escaping path to actually be a child of the
    root directory, not just share its name as a prefix.
    """
    root = os.path.abspath(runtime_root) if runtime_root else os.path.abspath(".")
    path = derive_license_path(config_path)
    if not os.path.abspath(path).startswith(root + os.sep):
        return None
    return path


def _license_path() -> Optional[str]:
    """The ``.license`` sibling of the installed licensed plugin's config_path,
    mirroring ``apply_vpp_plugin_conf`` so 0x49 and the bundle write the SAME
    file the .so reads. None when no VPP plugin config is installed yet (no
    upload). Multi-plugin disambiguation by vppId is a future extension (the
    PDU carries no plugin id); the common case is one VPP plugin.
    """
    if not os.path.exists(VPP_CONF):
        return None
    try:
        conf = PluginsConfiguration.from_file(VPP_CONF)
    except Exception:
        return None
    candidates = [p for p in conf.plugins if getattr(p, "config_path", None)]
    if not candidates:
        return None
    return resolve_license_path(candidates[0].config_path)


def handle_license_command(command_hex: str) -> Optional[str]:
    """Resolve a license function code and return the response as spaced hex.

    Returns ``None`` when the command is not a license FC, so the caller forwards
    it to the C core as before. Never raises for a well-formed license FC.
    """
    data = _bytes_from_hex(command_hex)
    if not data or data[0] not in _LICENSE_FCS:
        return None
    fc = data[0]

    if fc == FC_GET_BOARD_ID:
        anchor = _read_anchor()
        if not anchor:
            # Match the Arduino firmware (D57): no id -> SUCCESS with id_len=0, so
            # the editor sees a clean empty id (outcome no-id), not an error byte.
            return _hex_from_bytes(bytes([fc, ST_SUCCESS, 0]))
        length = min(len(anchor), 255)
        return _hex_from_bytes(bytes([fc, ST_SUCCESS, length]) + anchor[:length])

    if fc == FC_READ_LICENSE:
        path = _license_path()
        if not path or not os.path.exists(path):
            return _hex_from_bytes(bytes([fc, ST_LIC_EMPTY]))
        with open(path, "rb") as handle:
            blob = handle.read()
        if len(blob) != LIC_BLOB_SIZE:
            return _hex_from_bytes(bytes([fc, ST_LIC_CORRUPT]))
        length = len(blob)
        header = bytes([fc, ST_SUCCESS, (length >> 8) & 0xFF, length & 0xFF])
        return _hex_from_bytes(header + blob)

    if fc == FC_WRITE_LICENSE:
        # [0x49][len:u16BE][blob...]
        if len(data) < 3:
            return _hex_from_bytes(bytes([fc, ST_LIC_CORRUPT]))
        length = (data[1] << 8) | data[2]
        blob = data[3 : 3 + length]
        if len(blob) != length or length != LIC_BLOB_SIZE:
            return _hex_from_bytes(bytes([fc, ST_LIC_CORRUPT]))
        path = _license_path()
        if not path:
            return _hex_from_bytes(bytes([fc, ST_LIC_UNSUPPORTED]))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(blob)
        return _hex_from_bytes(bytes([fc, ST_SUCCESS]))

    return None
