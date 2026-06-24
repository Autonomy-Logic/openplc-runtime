"""
OPC-UA plugin memory access — STruC++ debugger surface.

Variable I/O goes through the runtime's strucpp_debug_* C function
pointers exposed on plugin_runtime_args_t:

    args.debug_read(arr, elem, dest)         -> bytes written
    args.debug_write(arr, elem, bytes, len)  -> status (soft write)
    args.debug_set(arr, elem, force, ...)    -> status (force/unforce)
    args.debug_size(arr, elem)               -> bytes for that leaf

On the threaded runtime, debug_write/debug_set ENQUEUE through the
runtime's debug-write journal (runtime_external_write) and are applied
race-free at the no-task window. The status byte reflects the enqueue:
0x7E queued OK, 0x82 queue momentarily full (treat as retry next cycle),
0x81 no program loaded.

The plugin is variable-position-agnostic: the editor resolves user-
selected variables to (arr, elem) tuples and the plugin forwards them
through these calls. No flat-index lookups, no debug-map.json reads,
no direct ctypes pointer arithmetic.

OPC-UA Writes use debug_write — soft writes that the next scan cycle
can overwrite. Use debug_set(forcing=True) only to expose a "force"
capability (not the default).
"""

import ctypes
from typing import Any, Dict, Iterable, Optional, Tuple

# Import shared modules (handle both package and direct loading)
try:
    from .opcua_logging import log_debug, log_error, log_warn
    from .types import VariableMetadata
except ImportError:
    from types import VariableMetadata  # type: ignore

    from opcua_logging import log_debug, log_error, log_warn


# TIME-related datatypes are stored on disk as IEC_TIMESPEC: two signed
# 32-bit fields (tv_sec, tv_nsec), 8 bytes total. This is NOT an int64 of
# nanoseconds. tv_sec holds whole seconds (since epoch for DATE/DT, since
# midnight for TOD, or a raw duration for TIME) and tv_nsec the sub-second
# remainder. They cross the debug surface as a (tv_sec, tv_nsec) tuple,
# which is exactly what opcua_utils.convert_value_for_opcua/_for_plc expect.
TIME_DATATYPES = frozenset(["TIME", "DATE", "TOD", "DT"])


class _IecTimespec(ctypes.Structure):
    """On-disk layout of strucpp's IEC_TIMESPEC (TIME/DATE/TOD/DT)."""

    _fields_ = [("tv_sec", ctypes.c_int32), ("tv_nsec", ctypes.c_int32)]


# Maximum byte size for read buffers. Generous bound so a STRING
# (126 bytes + 1 length byte = 127) plus future variable-length
# encodings fit without reallocating per read.
_READ_BUFFER_SIZE = 256

# Status code from debug_dispatch.hpp
STATUS_OK = 0x7E


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
        # 8-byte leaf, but decoded/encoded as an IEC_TIMESPEC tuple by the
        # read/write helpers below (not as this scalar). c_int64 is returned
        # only so the "is this type supported?" gate (ctype is None) passes.
        return ctypes.c_int64
    if t in ("STRING", "WSTRING"):
        return None  # variable-length, not yet supported by debug surface
    return None


def _pack_timespec(value: Any) -> Optional[bytes]:
    """Pack a (tv_sec, tv_nsec) tuple — as produced by
    convert_value_for_plc for TIME/DATE/TOD/DT — into the 8-byte
    IEC_TIMESPEC layout. Returns None if value is not a usable timespec."""
    if isinstance(value, tuple) and len(value) == 2:
        tv_sec, tv_nsec = value
    elif isinstance(value, int):
        tv_sec, tv_nsec = value, 0
    else:
        return None
    try:
        ts = _IecTimespec(
            ctypes.c_int32(int(tv_sec)).value,
            ctypes.c_int32(int(tv_nsec)).value,
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return bytes(ts)


def debug_read_value(args: Any, arr: int, elem: int, datatype: str) -> Optional[Any]:
    """Read a single PLC variable through args.debug_read and decode
    it into a Python value matching the IEC datatype.

    Returns None on read failure (program not loaded, address out-of-
    bounds, type stub not implemented). Callers should treat None as
    "skip this variable for the current cycle".
    """
    ctype = _ctype_for(datatype)
    if ctype is None:
        # STRING/WSTRING — not supported yet (Phase 4a in
        # debug_dispatch.hpp explicitly stubs string reads).
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
        # Out-of-bounds, no program, or string-stub — skip.
        return None

    if (datatype or "").upper() in TIME_DATATYPES:
        # Decode the 8-byte leaf as IEC_TIMESPEC -> (tv_sec, tv_nsec).
        ts = ctypes.cast(buf, ctypes.POINTER(_IecTimespec)).contents
        return (int(ts.tv_sec), int(ts.tv_nsec))

    # Reinterpret the leading bytes as the typed scalar.
    typed = ctypes.cast(buf, ctypes.POINTER(ctype)).contents
    return typed.value


def debug_write_value(args: Any, arr: int, elem: int, datatype: str, value: Any) -> bool:
    """Soft-write a Python value to a PLC variable through
    args.debug_write. Encodes the value per the IEC datatype, then
    forwards to the runtime which enqueues it on the debug-write journal
    (applied race-free at the no-task window; ignored if the variable is
    currently forced — matches the editor's debugger contract).

    Returns True on STATUS_OK (queued), False otherwise (e.g. 0x82 queue
    full — callers should retry on the next cycle, not treat as fatal).
    """
    ctype = _ctype_for(datatype)
    if ctype is None:
        return False

    if (datatype or "").upper() in TIME_DATATYPES:
        raw = _pack_timespec(value)
        if raw is None:
            log_warn(f"debug_write({arr}, {elem}, {datatype}): bad timespec {value!r}")
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
    if ctype is None:
        return False

    if (datatype or "").upper() in TIME_DATATYPES:
        raw = _pack_timespec(value)
        if raw is None:
            log_warn(f"debug_force({arr}, {elem}, {datatype}): bad timespec {value!r}")
            return False
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
    """Build a (arr, elem) -> VariableMetadata cache. Sizes come from
    the runtime's args.debug_size; types come from the configured
    datatype (already known per-variable from the plugin config, no
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
            # Out-of-bounds or string-stub — skip.
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
        # Mimic CODESYS: negative duration -> (-N, 0..-N). Use floor.
        sec, nsec = divmod(value_ns, 1_000_000_000)
    else:
        sec = value_ns // 1_000_000_000
        nsec = value_ns % 1_000_000_000
    return int(sec), int(nsec)


def timespec_to_time(tv_sec: int, tv_nsec: int) -> int:
    """Compose (tv_sec, tv_nsec) back into an int64 nanosecond value."""
    return int(tv_sec) * 1_000_000_000 + int(tv_nsec)
