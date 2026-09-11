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

Anchor bytes are returned RAW (ASCII, trailing NUL/CR/LF/space stripped) so the
editor derives the SAME device_id the .so does (D70d); no hex-decoding. The
canonical normalization is the C one (``rpi_plugin.c``) because the C is what
decides whether a license verifies -- see ``ANCHOR_STRIP_BYTES`` below.

Blob integrity is checked on BOTH directions (0x49 and 0x4A) with the same
function, mirroring what ``license_store_read`` validates on bare metal so the
two targets answer the same bytes for the same file.
"""
import os
import tempfile
import zlib
from typing import Optional

from webserver import config
from webserver.logger import get_logger
from webserver.plugin_config_model import PluginsConfiguration

logger, _ = get_logger("vpp_license", use_buffer=True)

# License function codes (mirror simulator/types.ts + firmware modbus_types.h).
FC_GET_BOARD_ID = 0x48
FC_WRITE_LICENSE = 0x49
FC_READ_LICENSE = 0x4A
_LICENSE_FCS = (FC_GET_BOARD_ID, FC_WRITE_LICENSE, FC_READ_LICENSE)

# Status bytes (shared with the Arduino firmware / editor).
ST_SUCCESS = 0x7E
# 0x81/0x82 are MB_DEBUG_ERROR_OUT_OF_BOUNDS / MB_DEBUG_ERROR_OUT_OF_MEMORY,
# which license_store.h:44-49 REUSES for LIC_STORE_TOO_LARGE / LIC_STORE_IO_ERROR.
# The bare-metal store already answers these two and the editor already parses
# them (modbus-pdu.ts statusError), so emitting them here adds no ABI.
ST_LIC_TOO_LARGE = 0x81
ST_LIC_IO_ERROR = 0x82
ST_LIC_EMPTY = 0x83
ST_LIC_CORRUPT = 0x84
ST_LIC_UNSUPPORTED = 0x85

ANCHOR_PATH = "/proc/device-tree/serial-number"
VPP_CONF = "vpp_plugins.conf"
LIC_BLOB_SIZE = 98

# Bytes stripped from the END of the raw anchor: NUL, CR, LF and SPACE -- and
# ONLY those four, because that is the list in rpi_plugin.c:103-107, and the C
# is canonical (it is the side that decides whether the license verifies).
# TAB used to be in this list and never was in the C one, while both comments
# claimed byte-identity: an anchor ending in 0x09 derived a DIFFERENT device_id
# here than on the .so, so the purchased license silently never worked. Do NOT
# add bytes "for safety" -- every byte in this set changes the device_id.
# Parity is pinned by tests/pytest/plugins/test_vpp_anchor_cross_language.py,
# which executes the real C.
ANCHOR_STRIP_BYTES = b"\x00\r\n "
# rpi_plugin.c:99 reads the anchor into `uint8_t anchor[64]`, so the .so never
# sees more than 64 bytes. Refuse a longer anchor instead of putting bytes on
# the wire that would derive a device_id the .so cannot reproduce (it would
# hash the first 64; the editor would hash all of them -> DEVICE_MISMATCH ->
# demo). Never truncate silently.
ANCHOR_MAX_BYTES = 64

# Blob layout (contract/firmware/license_blob.h, license-blob.ts): LE u32 magic
# at 0, CRC-32/ISO-HDLC over bytes [0..93] stored as a LE u32 at 94.
LIC_MAGIC_LE = 0x434C504F  # bytes 4F 50 4C 43
LIC_OFF_CRC32 = 94


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
    # Strip trailing NUL / CR / LF / SPACE -- exactly the four bytes
    # rpi_plugin.c strips, no more (the device-tree serial is NUL-terminated).
    return raw.rstrip(ANCHOR_STRIP_BYTES)


def validate_license_blob(blob: bytes) -> Optional[int]:
    """``None`` when the blob is one the closed verifier could accept; otherwise
    the status byte to answer with.

    Same checks, same order, same status bytes as ``license_store_read`` on bare
    metal (``license_store.h:24-26``, ``license_store_esp32.cpp:59-68``): wrong
    size -> CORRUPT, magic absent -> EMPTY, crc32 mismatch -> CORRUPT.

    Why this matters on the Linux path specifically: 0x4A used to test the
    length ONLY, so a 98-byte file that does not verify (an SD card cloned from
    another Pi, a corrupted flash, a torn write) answered SUCCESS. The editor
    reads SUCCESS as "a valid license blob is present and intact (magic + crc32)"
    and returns 'already-licensed' BEFORE talking to the backend -- so the one
    automatic repair path (recover a fresh license) never ran, precisely because
    the editor believed the blob was good, while license_core refused it and the
    plugin dropped to demo and stopped actuating 15 minutes later.

    This is NOT a verdict on the license: signature and device binding are the
    closed license-core's job and it is the only verifier (the runtime only
    transports). These are the checks that need no key and no anchor, which is
    exactly the set the bare-metal store already performs.

    ``zlib.crc32`` IS CRC-32/ISO-HDLC from the stdlib -- not a reimplementation
    of ours, so there is no second derivation to drift.
    """
    if len(blob) != LIC_BLOB_SIZE:
        return ST_LIC_CORRUPT
    if int.from_bytes(blob[0:4], "little") != LIC_MAGIC_LE:
        return ST_LIC_EMPTY
    stored = int.from_bytes(blob[LIC_OFF_CRC32 : LIC_OFF_CRC32 + 4], "little")
    if zlib.crc32(blob[:LIC_OFF_CRC32]) != stored:
        return ST_LIC_CORRUPT
    return None


def derive_license_path(config_path: str) -> str:
    """The ``.license`` sibling of a plugin's config_path: drop a trailing
    ``.json``, append ``.license``. MUST mirror ``derive_license_path()`` in
    the plugin's C source (rpi_plugin.c) exactly, or the .so reads the wrong
    path and falls back to demo.

    Two details are copied from the C rather than written the idiomatic way,
    because "exactly" is the whole contract:

    * The C strips the extension only when ``len > strlen(".json")``, so a
      config_path of literally ``".json"`` keeps it and becomes
      ``".json.license"``. A plain ``endswith`` would strip it and yield
      ``".license"`` -- the runtime would write one file and the .so read
      another.
    * The C leaves ``out`` empty for a NULL/empty config_path; an empty string
      in, an empty string out.
    """
    if not config_path:
        return ""
    ext = ".json"
    base = config_path[: -len(ext)] if len(config_path) > len(ext) and config_path.endswith(ext) else config_path
    return base + ".license"


def is_inside_root(path: str, runtime_root: Optional[str] = None) -> bool:
    """True when ``path`` really resolves to a location under the runtime root.

    Shared by every write path that trusts an editor-supplied ``config_path``
    (0x49 writes 98 bytes as root; ``apply_vpp_plugin_conf`` copies an
    upload-supplied blob), so all of them agree on one definition of
    "contained".

    Two traps this avoids:

    * A bare ``.startswith(root)`` accepts a sibling that merely shares the
      root as a *string* prefix -- root ``/opt/runtime`` vs. an escaping
      ``/opt/runtime-evil/x``.
    * ``abspath`` normalises lexically only, so it does NOT follow symlinks. If
      any component inside the root is a link out (a deploy link, a data volume
      mounted under ``build/``), a perfectly innocent-looking relative path
      resolves outside the root and the guard still reports success.
      ``realpath`` resolves the links; on a path that does not exist yet it
      resolves the existing prefix and appends the remainder, which is exactly
      what a not-yet-written ``.license`` needs.

    ``commonpath`` is used instead of a separator-anchored prefix so that a
    root of ``/`` (``abspath`` yields ``"/"``, and ``"/" + os.sep`` is ``"//"``,
    which nothing starts with) does not silently refuse every write.
    """
    root = os.path.realpath(runtime_root) if runtime_root else os.path.realpath(".")
    try:
        return os.path.commonpath([root, os.path.realpath(path)]) == root
    except ValueError:
        # Raised for paths that share no root at all (different drives on
        # Windows, or a mix of absolute and relative that cannot be compared).
        return False


def resolve_license_path(config_path: str, runtime_root: Optional[str] = None) -> Optional[str]:
    """``derive_license_path()`` plus the anti-traversal guard: never resolve
    to a path outside a KNOWN root, even if a forged ``vpp_plugins.conf``
    carries an escaping ``config_path``. Returns None when it escapes.

    TWO roots are accepted, on purpose:

    * the runtime root (the legacy ``build/vpp`` location), and
    * ``config.VPP_DATA_DIR`` under PERSISTENT_DATA_DIR, where
      ``apply_vpp_plugin_conf`` now relocates VPP configs+licenses so a runtime
      version update (which wipes ``$OPENPLC_DIR/build``) can no longer delete a
      purchased license. After that relocation the ``config_path`` in
      vpp_plugins.conf is an absolute path under VPP_DATA_DIR, and a guard that
      only knew the runtime root would refuse the device its own license.

    This widens the guard to a SECOND known root, NOT to "anywhere": ``..`` and
    arbitrary absolute paths still resolve outside both roots and are refused.
    """
    path = derive_license_path(config_path)
    # An empty derivation (empty config_path) would resolve to the cwd, which
    # IS inside the root -- refuse it rather than let it through as a target.
    if not path:
        return None
    if is_inside_root(path, runtime_root) or is_inside_root(path, str(config.VPP_DATA_DIR)):
        return path
    return None


def _license_path() -> Optional[str]:
    """The ``.license`` sibling of the FIRST installed plugin that carries a
    ``config_path``, mirroring ``apply_vpp_plugin_conf`` so 0x49 and the bundle
    write the SAME file the .so reads. None when no VPP plugin config is
    installed yet (no upload).

    KNOWN LIMITATION -- documented, deliberately not fixed here. The PDU carries
    no plugin id, so 0x49 and 0x4A always act on ``candidates[0]``. Note what
    ``candidates`` filters on: having a ``config_path``, NOT being licensable.
    So this bites with a SINGLE licensable VPP installed -- one FREE VPP ahead of
    it in ``vpp_plugins.conf`` is enough for the license to land on the free
    plugin's ``.license``, after which 0x4A reads that same wrong file back and
    reports SUCCESS while the licensed plugin stays in demo and stops actuating.
    **Install the licensable VPP alone, or first in the list.**

    Note that the blob validation above does NOT catch this case: the blob is
    perfectly valid, it is just in the wrong file.

    Disambiguating for real is out of scope by decision: it needs either a plugin
    id on the wire (the PDU is frozen) or ``licensable``/``vppId`` on
    ``PluginConfig``, which carries neither -- so the runtime has no
    ``name``->``vppId`` mapping and cannot even compare the blob's ``product_id``.
    Failing closed on >1 candidate was rejected too: it would block a device that
    legitimately runs a free VPP alongside a paid one. What is left is a WARN, so
    that the day this bites leaves a trace in the log.
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
    if len(candidates) > 1:
        logger.warning(
            "vpp_plugins.conf lists %d plugins with a config_path; license FCs "
            "(0x49/0x4A) act on the FIRST one (%s). If the licensable VPP is not "
            "that one, its license lands on the wrong file and 0x4A still reports "
            "SUCCESS while the plugin runs in demo. Install the licensable VPP "
            "alone, or first in the list. Candidates: %s",
            len(candidates),
            getattr(candidates[0], "name", "?"),
            [getattr(p, "name", "?") for p in candidates],
        )
    return resolve_license_path(candidates[0].config_path)


def _fsync_directory(directory: str) -> None:
    """Best-effort fsync of ``directory`` so the RENAME itself is durable.

    Without it the replaced name can still be lost to a power cut even though
    the file contents were fsynced. Not available on every platform (Windows
    refuses to open a directory), and a failure here does not undo a rename that
    already succeeded -- so every error is swallowed on purpose.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_license_atomically(path: str, blob: bytes) -> None:
    """Write ``blob`` to ``path`` so that a failure NEVER destroys the license
    that is already there. Raises ``OSError`` for the caller to map.

    ``open(path, "wb")`` truncates before writing, so ENOSPC, a power cut or the
    process dying mid-activation would leave a partial file -- destroying the
    PREVIOUS, VALID license and dropping the device to demo on the next start.
    An activation that fails must not leave the device worse than it was.

    A temporary sibling plus ``os.replace`` makes the swap atomic within the
    filesystem; the ``fsync`` before it is what makes the bytes durable rather
    than merely visible. ``mkstemp`` (not a fixed ``.tmp`` name) so two writers
    cannot interleave into the same temporary and then rename the mess over a
    good license.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".license-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600; the license is public signed data and the bundle
        # delivery path writes it under the default mask -- keep the two alike so
        # the .so reads the same file either way.
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except OSError:
        # Leave no debris behind, and leave the previous license untouched.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _fsync_directory(directory)


def handle_license_command(command_hex: str) -> Optional[str]:
    """Resolve a license function code and return the response as spaced hex.

    Returns ``None`` when the command is not a license FC, so the caller forwards
    it to the C core as before.

    Never raises for a well-formed license FC -- and now actually keeps that
    promise: filesystem failures are mapped to the status bytes the bare-metal
    store already answers (IO_ERROR -> 0x82, TOO_LARGE -> 0x81) instead of
    propagating an exception that the WebSocket layer would flatten into a
    ``{"success": false}`` envelope, which the editor's PDU parser never sees as
    a status byte at all.
    """
    data = _bytes_from_hex(command_hex)
    if not data or data[0] not in _LICENSE_FCS:
        return None
    fc = data[0]

    if fc == FC_GET_BOARD_ID:
        anchor = _read_anchor()
        if not anchor:
            # NOT the Arduino convention (review 2026-08-20, R2). On this medium
            # 0x48 is ONLY the licensing anchor -- SUCCESS with id_len=0 made the
            # editor hash an EMPTY pre-image, so every anchor-less host (x86 box,
            # container, unmounted /proc/device-tree) derived the SAME deviceId,
            # a purchase bound to it never validated on the .so, and the buyer
            # got a 2-hour demo forever. UNSUPPORTED is the truth: this device
            # has no hardware anchor to license against.
            return _hex_from_bytes(bytes([fc, ST_LIC_UNSUPPORTED]))
        if len(anchor) > ANCHOR_MAX_BYTES:
            # REFUSE. The .so only ever reads 64 bytes, so anything longer would
            # make the editor derive a device_id the verifier cannot reproduce --
            # the license would be bought against an identity that never
            # validates. An error byte is recoverable; a wrong device_id is not.
            logger.error(
                "Anchor at %s is %d bytes after normalization, over the %d-byte "
                "ceiling the license verifier reads; refusing 0x48 rather than "
                "returning bytes that derive a device_id the verifier cannot "
                "reproduce.",
                ANCHOR_PATH,
                len(anchor),
                ANCHOR_MAX_BYTES,
            )
            return _hex_from_bytes(bytes([fc, ST_LIC_TOO_LARGE]))
        return _hex_from_bytes(bytes([fc, ST_SUCCESS, len(anchor)]) + anchor)

    if fc == FC_READ_LICENSE:
        path = _license_path()
        if not path or not os.path.exists(path):
            return _hex_from_bytes(bytes([fc, ST_LIC_EMPTY]))
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
        except OSError as exc:
            # A read-only or failing SD card is the number-one failure mode of an
            # industrial Pi. It has a status byte on bare metal (IO_ERROR ->
            # 0x82); raising here instead would surface as a generic transport
            # error the editor cannot tell from a dropped link.
            logger.error("0x4A could not read %s: %s", path, exc)
            return _hex_from_bytes(bytes([fc, ST_LIC_IO_ERROR]))
        bad_status = validate_license_blob(blob)
        if bad_status is not None:
            logger.warning(
                "0x4A: license at %s failed validation (%d bytes) -> status 0x%02X",
                path,
                len(blob),
                bad_status,
            )
            return _hex_from_bytes(bytes([fc, bad_status]))
        header = bytes([fc, ST_SUCCESS, (LIC_BLOB_SIZE >> 8) & 0xFF, LIC_BLOB_SIZE & 0xFF])
        return _hex_from_bytes(header + blob)

    if fc == FC_WRITE_LICENSE:
        # [0x49][len:u16BE][blob...]
        if len(data) < 3:
            return _hex_from_bytes(bytes([fc, ST_LIC_CORRUPT]))
        length = (data[1] << 8) | data[2]
        blob = data[3 : 3 + length]
        if len(blob) != length:
            return _hex_from_bytes(bytes([fc, ST_LIC_CORRUPT]))
        # Validate BEFORE touching the filesystem, with the same function 0x4A
        # uses: a blob that would read back as EMPTY/CORRUPT must never replace a
        # license that is already there. The size check that used to live here is
        # the first check inside validate_license_blob, and answers the same
        # 0x84.
        bad_status = validate_license_blob(blob)
        if bad_status is not None:
            logger.warning(
                "0x49 refused a %d-byte blob that does not validate -> status 0x%02X",
                len(blob),
                bad_status,
            )
            return _hex_from_bytes(bytes([fc, bad_status]))
        path = _license_path()
        if not path:
            return _hex_from_bytes(bytes([fc, ST_LIC_UNSUPPORTED]))
        try:
            _write_license_atomically(path, blob)
        except OSError as exc:
            logger.error("0x49 could not write %s: %s", path, exc)
            return _hex_from_bytes(bytes([fc, ST_LIC_IO_ERROR]))
        return _hex_from_bytes(bytes([fc, ST_SUCCESS]))

    return None
