"""
OPC-UA plugin memory access — STruC++ debugger surface.

Replaces the MatIEC-era direct-ctypes-pointer fast path. Variable I/O
now goes through the runtime's strucpp_debug_* C function pointers
exposed on plugin_runtime_args_t:

    args.debug_read(arr, elem, dest)         → bytes written
    args.debug_write(arr, elem, bytes, len)  → status (soft write)
    args.debug_set(arr, elem, force, ...)    → status (force/unforce)
    args.debug_size(arr, elem)               → bytes for that leaf

The plugin is variable-position-agnostic: the editor resolves user-
selected variables to (arr, elem) tuples in opcua_config.json and the
plugin forwards them through these calls. No flat-index lookups, no
debug-map.json reads, no direct ctypes pointer arithmetic.

OPC-UA Writes use debug_write — soft writes that the next scan cycle
can overwrite. Use debug_set(forcing=True) only if exposing a
"force" capability to clients (not the default).
"""

import ctypes
import os
import struct
import sys
from typing import Any, Dict, Iterable, Optional, Tuple

# Add directories to path for module access
_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

# Import local modules (handle both package and direct loading)
try:
    from .opcua_types import VariableMetadata
    from .opcua_logging import log_debug, log_error, log_warn
except ImportError:
    from opcua_types import VariableMetadata
    from opcua_logging import log_debug, log_error, log_warn


# TIME-related datatypes are encoded as 8-byte signed integers in
# nanoseconds (matching strucpp's TIME_t / DATE_t / TOD_t / DT_t).
TIME_DATATYPES = frozenset(["TIME", "DATE", "TOD", "DT"])

# Maximum byte size for read buffers. Generous bound so a STRING
# (126 bytes + 1 length byte = 127) plus future variable-length
# encodings fit without reallocating per read.
_READ_BUFFER_SIZE = 256

# Status code from debug_dispatch.hpp
STATUS_OK = 0x7E

# STRING / WSTRING are variable-length and share one wire format with
# strucpp's debug surface: a single count byte, then the payload.
#
#   STRING   [count][count bytes]            padded to 127 bytes
#   WSTRING  [count][count * 2 bytes LE]     padded to 253 bytes
#
# `count` is in BYTES for STRING and in UTF-16 CODE UNITS for WSTRING, and is
# capped at DEBUG_STRING_CAP on both sides -- strucpp's `validate_payload`
# REFUSES a longer write outright rather than truncating it, so the truncation
# has to happen here.
#
# BYTES, not characters: `IECString` stores `char data_[MaxLen + 1]` with
# `length_` counting bytes, and `_truncate_utf8` below spends its whole body on
# that fact. The two comments used to contradict each other, and the difference
# is user-visible -- 126 bytes is ~63 two-byte accented characters, or ~31
# four-byte emoji, not 126 of either.
STRING_DATATYPES = frozenset(["STRING", "WSTRING"])
DEBUG_STRING_CAP = 126


# Warnings raised from the READ path, which asyncua calls once per variable per
# client Read. A leaf that is persistently malformed is not a new event every
# poll -- at a one-second poll and a handful of clients it is a log that scrolls
# its own cause off the screen. Say it once per distinct problem and stay quiet
# after that; the condition is a property of the program, not of the poll.
_warned: set = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    log_warn(f"{message} (further identical warnings suppressed)")


def _is_string(datatype: str) -> bool:
    return (datatype or "").upper() in STRING_DATATYPES


def _decode_string(datatype: str, buf: Any, n: int) -> Optional[Any]:
    """Decode strucpp's [count][payload] wire form.

    STRING comes back as `str`, WSTRING as `bytes` of UTF-16LE code units --
    NOT as `str`. Transcoding to UTF-8 here would be lossy for lone surrogates
    and would not match how the value is exposed over OPC-UA (a ByteString),
    so the bytes are handed over verbatim and the encoding is documented on
    the node.
    """
    if n < 1:
        return None
    count = int(buf[0])
    if count > DEBUG_STRING_CAP:
        _warn_once(
            f"malformed-count:{datatype}",
            f"string payload claims {count} units, cap is {DEBUG_STRING_CAP}; truncating",
        )
        count = DEBUG_STRING_CAP
    wide = datatype.upper() == "WSTRING"
    payload_len = count * 2 if wide else count
    if 1 + payload_len > n:
        _warn_once(
            f"payload-overruns-read:{datatype}",
            f"string payload of {payload_len} bytes exceeds the {n} bytes read",
        )
        return None
    raw = bytes(bytearray(buf[1:1 + payload_len]))
    if wide:
        return raw
    # UTF-8, because that is what the rest of the system already agrees on:
    # the editor's debugger decodes this same wire form with the `len8-utf8`
    # codec (`variable-sizes.ts`). `errors="replace"` rather than strict so a
    # truncated multi-byte sequence degrades to one replacement character
    # instead of taking down the read.
    return raw.decode("utf-8", errors="replace")


def _encode_string(datatype: str, value: Any) -> Optional[bytes]:
    """Encode a Python value into strucpp's [count][payload] wire form."""
    wide = datatype.upper() == "WSTRING"
    if wide:
        if isinstance(value, str):
            raw = value.encode("utf-16-le")
        elif isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
        else:
            return None
        # An odd byte count is not a short string, it is a malformed one.
        if len(raw) % 2:
            log_warn("WSTRING payload has an odd byte count; dropping the trailing byte")
            raw = raw[:-1]
        count = min(len(raw) // 2, DEBUG_STRING_CAP)
        # Do not cut between the halves of a surrogate pair. The STRING path
        # goes to real trouble not to split a UTF-8 sequence (_truncate_utf8);
        # the same care is owed here, because a lone high surrogate is not a
        # shorter string, it is an undecodable one -- `bytes.decode('utf-16-le')`
        # raises on it. Astral characters (emoji, most CJK extensions) are the
        # common case.
        if count > 0:
            last = int.from_bytes(raw[(count - 1) * 2 : count * 2], "little")
            if 0xD800 <= last <= 0xDBFF:  # high surrogate with its pair cut off
                count -= 1
        payload = raw[: count * 2]
    else:
        if isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
        else:
            raw = str(value).encode("utf-8")
        payload = _truncate_utf8(raw, DEBUG_STRING_CAP)
        count = len(payload)
    return bytes([count]) + payload


def _truncate_utf8(raw: bytes, limit: int) -> bytes:
    """Cut `raw` to at most `limit` bytes without splitting a character.

    The cap is a BYTE budget, but UTF-8 characters are 1-4 bytes, so a blind
    slice can leave a half-character that decodes to a replacement on the way
    back out. Walk back off any continuation byte (0b10xxxxxx) instead.
    """
    if len(raw) <= limit:
        return raw
    end = limit
    while end > 0 and (raw[end] & 0xC0) == 0x80:
        end -= 1
    return raw[:end]


def _ctype_for(datatype: str) -> Optional[Any]:
    """Map an IEC type name to the ctypes scalar that owns its bytes
    on disk. Returns None for variable-length types (STRING/WSTRING)
    which need special handling."""
    t = (datatype or "").upper()
    if t == "BOOL":
        return ctypes.c_uint8  # 1 byte 0/1
    if t == "SINT":
        return ctypes.c_int8
    if t in ("USINT", "BYTE"):
        return ctypes.c_uint8
    if t == "INT":
        return ctypes.c_int16
    if t in ("UINT", "WORD"):
        return ctypes.c_uint16
    if t == "DINT":
        return ctypes.c_int32
    if t in ("UDINT", "DWORD"):
        return ctypes.c_uint32
    if t == "LINT":
        return ctypes.c_int64
    if t in ("ULINT", "LWORD"):
        return ctypes.c_uint64
    if t == "REAL":
        return ctypes.c_float
    if t == "LREAL":
        return ctypes.c_double
    if t in TIME_DATATYPES:
        # strucpp encodes time-family types as int64 nanoseconds (TIME_t).
        return ctypes.c_int64
    if t in STRING_DATATYPES:
        # Variable-length: no fixed-width ctype owns these. They ARE readable
        # and writable -- strucpp wires read_string / write_string /
        # read_wstring / write_wstring into type_ops[] at tags 19/20 -- through
        # _decode_string / _encode_string instead. Callers must therefore pair
        # a None from here with an _is_string() check rather than giving up.
        return None
    return None


def debug_read_value(args: Any, arr: int, elem: int, datatype: str) -> Optional[Any]:
    """Read a single PLC variable through args.debug_read and decode
    it into a Python value matching the IEC datatype.

    Returns None on read failure (program not loaded, address out-of-
    bounds, type stub not implemented). Callers should treat None as
    "skip this variable for the current cycle".
    """
    ctype = _ctype_for(datatype)
    if ctype is None and not _is_string(datatype):
        return None

    buf = (ctypes.c_uint8 * _READ_BUFFER_SIZE)()
    try:
        n = args.debug_read(
            ctypes.c_uint8(arr),
            ctypes.c_uint16(elem),
            ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)),
        )
    except Exception as e:
        log_error(f"debug_read({arr}, {elem}) raised: {e}")
        return None
    if n == 0:
        # Out-of-bounds or no program loaded — skip.
        return None

    if _is_string(datatype):
        return _decode_string(datatype, buf, n)

    # Reinterpret the leading bytes as the typed scalar.
    typed = ctypes.cast(buf, ctypes.POINTER(ctype)).contents
    return typed.value


def debug_write_value(args: Any, arr: int, elem: int, datatype: str, value: Any) -> bool:
    """Soft-write a Python value to a PLC variable through
    args.debug_write. Encodes the value per the IEC datatype, then
    forwards to the runtime which calls IECVar::set() on the leaf
    (write is silently ignored if the variable is currently forced;
    matches the editor's debugger contract).

    Returns True on STATUS_OK, False otherwise.
    """
    ctype = _ctype_for(datatype)
    if ctype is None and not _is_string(datatype):
        return False

    if _is_string(datatype):
        raw = _encode_string(datatype, value)
        if raw is None:
            log_warn(f"debug_write({arr}, {elem}, {datatype}): cannot encode {value!r}")
            return False
    else:
        try:
            encoded = ctype(value)
        except (TypeError, ValueError) as e:
            log_warn(f"debug_write({arr}, {elem}, {datatype}): cannot encode {value!r}: {e}")
            return False
        raw = bytes(encoded)
    buf = (ctypes.c_uint8 * len(raw))(*raw)
    try:
        status = args.debug_write(
            ctypes.c_uint8(arr),
            ctypes.c_uint16(elem),
            ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)),
            ctypes.c_uint16(len(raw)),
        )
    except Exception as e:
        log_error(f"debug_write({arr}, {elem}) raised: {e}")
        return False
    return status == STATUS_OK


def debug_force_value(args: Any, arr: int, elem: int, datatype: str, value: Any) -> bool:
    """Force-write a value (debug_set with forcing=True). Pins the
    variable until explicitly unforced. Distinct from debug_write
    which is a soft write the program can overwrite next cycle.
    Exposed for any plugin feature that wants debugger-style pinning.
    """
    ctype = _ctype_for(datatype)
    if ctype is None and not _is_string(datatype):
        return False

    # Strings force through the same wire form as a write. Read and write each
    # grew a string path and force did not, so forcing a STRING answered False
    # with no reason given -- the same silence that hid the read bug, kept
    # alive in the one operation nobody calls yet.
    if _is_string(datatype):
        encoded_str = _encode_string(datatype, value)
        if encoded_str is None:
            log_warn(f"debug_force({arr}, {elem}, {datatype}): cannot encode {value!r}")
            return False
        raw = encoded_str
    else:
        try:
            encoded = ctype(value)
        except (TypeError, ValueError) as e:
            log_warn(f"debug_force({arr}, {elem}, {datatype}): cannot encode {value!r}: {e}")
            return False
        raw = bytes(encoded)
    buf = (ctypes.c_uint8 * len(raw))(*raw)
    try:
        status = args.debug_set(
            ctypes.c_uint8(arr),
            ctypes.c_uint16(elem),
            ctypes.c_bool(True),
            ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)),
            ctypes.c_uint16(len(raw)),
        )
    except Exception as e:
        log_error(f"debug_set({arr}, {elem}) raised: {e}")
        return False
    return status == STATUS_OK


def debug_unforce(args: Any, arr: int, elem: int) -> bool:
    """Release a force on a variable (debug_set with forcing=False).
    The bytes/len arguments are ignored by the runtime's unforce path
    but the C signature still requires them — pass an empty buffer."""
    buf = (ctypes.c_uint8 * 1)()
    try:
        status = args.debug_set(
            ctypes.c_uint8(arr),
            ctypes.c_uint16(elem),
            ctypes.c_bool(False),
            ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)),
            ctypes.c_uint16(0),
        )
    except Exception as e:
        log_error(f"debug_set/unforce({arr}, {elem}) raised: {e}")
        return False
    return status == STATUS_OK


def initialize_variable_cache(
    args: Any,
    addrs: Iterable[Tuple[int, int]],
    datatypes: Dict[Tuple[int, int], str],
) -> Dict[Tuple[int, int], VariableMetadata]:
    """Build a (arr, elem) → VariableMetadata cache. Sizes come from
    the runtime's args.debug_size; types come from the configured
    datatype (already known per-variable from opcua_config.json, no
    need to query the runtime for them).

    Returns an empty dict if no program is loaded yet — callers should
    check for that and retry on the next cycle. Once populated, the
    cache replaces per-call args.debug_size lookups in the hot loops.
    """
    cache: Dict[Tuple[int, int], VariableMetadata] = {}
    for arr, elem in addrs:
        try:
            size = args.debug_size(ctypes.c_uint8(arr), ctypes.c_uint16(elem))
        except Exception as e:
            log_warn(f"debug_size({arr}, {elem}) raised: {e}")
            continue
        if size == 0:
            # Out of bounds, or no program loaded. NOT a string: strucpp's
            # handle_size reports 127 / 253 for STRING / WSTRING, so those
            # cache normally.
            continue
        datatype = datatypes.get((arr, elem), "UNKNOWN")
        cache[(arr, elem)] = VariableMetadata(
            arr=arr,
            elem=elem,
            size=size,
            inferred_type=datatype,
        )
    if cache:
        log_debug(f"Cached size+type metadata for {len(cache)} variables")
    return cache


def time_to_timespec(value_ns: int) -> Tuple[int, int]:
    """Split an int64 nanosecond value into (tv_sec, tv_nsec) for
    callers that want to expose TIME-family values as their CODESYS
    components rather than raw nanoseconds."""
    if value_ns < 0:
        # Mimic CODESYS: negative duration → (-N, 0..-N). Use floor.
        sec, nsec = divmod(value_ns, 1_000_000_000)
    else:
        sec = value_ns // 1_000_000_000
        nsec = value_ns % 1_000_000_000
    return int(sec), int(nsec)


def timespec_to_time(tv_sec: int, tv_nsec: int) -> int:
    """Compose (tv_sec, tv_nsec) back into an int64 nanosecond value."""
    return int(tv_sec) * 1_000_000_000 + int(tv_nsec)
