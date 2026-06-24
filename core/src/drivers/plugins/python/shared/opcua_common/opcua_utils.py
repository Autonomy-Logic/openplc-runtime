"""OPC-UA value/type conversion utilities (shared server/client)."""

import ctypes
import struct
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from asyncua import ua

# Import logging (handle both package and direct loading)
try:
    from .opcua_logging import log_warn
except ImportError:
    from opcua_logging import log_warn


# TIME-family datatypes. strucpp stores each as a single int64, but the unit
# differs: TIME and TOD are nanoseconds (duration / since midnight), DT is
# nanoseconds since the 1970 epoch, and DATE is *days* since the 1970 epoch.
TIME_DATATYPES = frozenset(["TIME", "DATE", "TOD", "DT"])

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    """Normalize an OPC-UA DateTime to an aware UTC datetime (asyncua may hand
    back naive UTC datetimes). Returns None if value is not a datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def map_plc_to_opcua_type(plc_type: str) -> ua.VariantType:
    """Map plc datatype to OPC-UA VariantType."""
    type_mapping = {
        "BOOL": ua.VariantType.Boolean,
        # 8-bit types
        "SINT": ua.VariantType.SByte,  # Signed 8-bit integer
        "USINT": ua.VariantType.Byte,  # Unsigned 8-bit integer
        "BYTE": ua.VariantType.Byte,  # Unsigned 8-bit (alias for USINT)
        # 16-bit types
        "INT": ua.VariantType.Int16,  # Signed 16-bit integer
        "UINT": ua.VariantType.UInt16,  # Unsigned 16-bit integer
        "WORD": ua.VariantType.UInt16,  # Unsigned 16-bit (bit string)
        # 32-bit types
        "DINT": ua.VariantType.Int32,  # Signed 32-bit integer
        "INT32": ua.VariantType.Int32,  # Alias for DINT
        "UDINT": ua.VariantType.UInt32,  # Unsigned 32-bit integer
        "DWORD": ua.VariantType.UInt32,  # Unsigned 32-bit (bit string)
        # 64-bit types
        "LINT": ua.VariantType.Int64,  # Signed 64-bit integer
        "ULINT": ua.VariantType.UInt64,  # Unsigned 64-bit integer
        "LWORD": ua.VariantType.UInt64,  # Unsigned 64-bit (bit string)
        # Floating point types
        "FLOAT": ua.VariantType.Float,
        "REAL": ua.VariantType.Float,  # IEC 61131-3 REAL = 32-bit float
        "LREAL": ua.VariantType.Double,  # IEC 61131-3 LREAL = 64-bit float
        # String type
        "STRING": ua.VariantType.String,
        "WSTRING": ua.VariantType.String,
        # TIME-related types
        "TIME": ua.VariantType.Int64,  # Duration in milliseconds
        "TOD": ua.VariantType.DateTime,  # Time of day as DateTime (current date + time)
        "DATE": ua.VariantType.DateTime,  # Date as DateTime (date only, time set to 00:00:00)
        "DT": ua.VariantType.DateTime,  # Date and Time as OPC-UA DateTime
    }
    mapped_type = type_mapping.get(plc_type.upper(), ua.VariantType.Variant)
    return mapped_type


def timespec_to_milliseconds(tv_sec: int, tv_nsec: int) -> int:
    """
    Convert IEC_TIMESPEC (tv_sec, tv_nsec) to milliseconds.

    Args:
        tv_sec: Seconds component
        tv_nsec: Nanoseconds component

    Returns:
        Total time in milliseconds
    """
    return (tv_sec * 1000) + (tv_nsec // 1_000_000)


def milliseconds_to_timespec(ms: int) -> tuple[int, int]:
    """
    Convert milliseconds to IEC_TIMESPEC format (tv_sec, tv_nsec).

    Args:
        ms: Time in milliseconds

    Returns:
        Tuple of (tv_sec, tv_nsec)
    """
    tv_sec = ms // 1000
    tv_nsec = (ms % 1000) * 1_000_000
    return (tv_sec, tv_nsec)


def convert_value_for_opcua(datatype: str, value: Any) -> Any:
    """Convert PLC debug variable value to OPC-UA compatible format."""
    # The debug utils return raw integer values based on variable size
    # Convert to appropriate OPC-UA types based on config datatype
    try:
        if datatype.upper() == "BOOL":
            # Ensure BOOL values are proper Python booleans
            if isinstance(value, bool):
                return value
            elif isinstance(value, (int, float)):
                return bool(value != 0)
            else:
                return bool(value)

        elif datatype.upper() == "SINT":
            # Ensure proper int8 type for OPC-UA compatibility (signed 8-bit)
            clamped_value = max(-128, min(127, int(value)))
            return ctypes.c_int8(clamped_value).value

        elif datatype.upper() in ["BYTE", "USINT"]:
            # Ensure proper uint8 type for OPC-UA compatibility
            return ctypes.c_uint8(max(0, min(255, int(value)))).value

        elif datatype.upper() in ["UINT", "WORD"]:
            # Ensure proper uint16 type for OPC-UA compatibility
            clamped_value = max(0, min(65535, int(value)))
            return ctypes.c_uint16(clamped_value).value

        elif datatype.upper() == "INT":
            # Ensure proper int16 type - critical for OPC-UA compatibility
            clamped_value = max(-32768, min(32767, int(value)))
            return ctypes.c_int16(clamped_value).value

        elif datatype.upper() in ["DINT", "INT32"]:
            # Ensure proper int32 type for OPC-UA compatibility
            clamped_value = max(-2147483648, min(2147483647, int(value)))
            return ctypes.c_int32(clamped_value).value

        elif datatype.upper() in ["UDINT", "DWORD"]:
            # Ensure proper uint32 type for OPC-UA compatibility
            clamped_value = max(0, min(4294967295, int(value)))
            return ctypes.c_uint32(clamped_value).value

        elif datatype.upper() == "LINT":
            return int(value)  # int64

        elif datatype.upper() in ["ULINT", "LWORD"]:
            # Ensure proper uint64 type for OPC-UA compatibility
            clamped_value = max(0, min(18446744073709551615, int(value)))
            return ctypes.c_uint64(clamped_value).value

        elif datatype.upper() in ["FLOAT", "REAL"]:
            # Float/Real values are stored as integers in debug variables
            # Convert back to float if it's an integer representation
            if isinstance(value, int):
                try:
                    return struct.unpack("f", struct.pack("I", value))[0]
                except struct.error:
                    return float(value)
            return float(value)

        elif datatype.upper() == "LREAL":
            # LREAL (64-bit float) values are stored as integers in debug variables
            # Convert back to double if it's an integer representation
            if isinstance(value, int):
                try:
                    return struct.unpack("d", struct.pack("Q", value))[0]
                except struct.error:
                    return float(value)
            return float(value)

        elif datatype.upper() in ["STRING", "WSTRING"]:
            return str(value)

        elif datatype.upper() == "TIME":
            # strucpp TIME = int64 nanoseconds (duration). Exposed as Int64 ns.
            return int(value)

        elif datatype.upper() == "TOD":
            # strucpp TOD = int64 nanoseconds since midnight. Expose as a
            # DateTime anchored on today's (UTC) date.
            ns = int(value)
            today = datetime.now(timezone.utc).date()
            base = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
            return base + timedelta(microseconds=ns // 1000)

        elif datatype.upper() == "DATE":
            # strucpp DATE = int64 DAYS since the 1970 epoch.
            return _EPOCH + timedelta(days=int(value))

        elif datatype.upper() == "DT":
            # strucpp DT = int64 nanoseconds since the 1970 epoch.
            sec, ns_rem = divmod(int(value), 1_000_000_000)
            return _EPOCH + timedelta(seconds=sec, microseconds=ns_rem // 1000)

        else:
            return value

    except (ValueError, TypeError, OverflowError) as e:
        # If conversion fails, return a safe default
        log_warn(f"Failed to convert value {value} to OPC-UA format for {datatype}: {e}")
        if datatype.upper() == "BOOL":
            return False
        elif datatype.upper() in ["FLOAT", "REAL"]:
            return 0.0
        elif datatype.upper() in ["STRING", "WSTRING"]:
            return ""
        elif datatype.upper() == "TIME":
            return 0
        elif datatype.upper() in TIME_DATATYPES:
            return _EPOCH
        else:
            return 0


def convert_value_for_plc(datatype: str, value: Any) -> Any:
    """Convert OPC-UA value to PLC debug variable format."""
    # Handle different OPC-UA value types more robustly
    try:
        if datatype.upper() == "BOOL":
            # Convert any value to boolean, then to int (0/1)
            if isinstance(value, bool):
                return int(value)
            elif isinstance(value, (int, float)):
                return 1 if value != 0 else 0
            elif isinstance(value, str):
                return 1 if value.lower() in ["true", "1", "yes", "on"] else 0
            else:
                return int(bool(value))

        elif datatype.upper() == "SINT":
            # Ensure proper int8 type for PLC compatibility (signed 8-bit)
            clamped_value = max(-128, min(127, int(value)))
            return ctypes.c_int8(clamped_value).value

        elif datatype.upper() in ["BYTE", "USINT"]:
            # Ensure proper uint8 type for PLC compatibility
            return ctypes.c_uint8(max(0, min(255, int(value)))).value

        elif datatype.upper() == "INT":
            # Ensure proper int16 type for PLC compatibility
            clamped_value = max(-32768, min(32767, int(value)))
            return ctypes.c_int16(clamped_value).value

        elif datatype.upper() in ["UINT", "WORD"]:
            # Ensure proper uint16 type for PLC compatibility
            clamped_value = max(0, min(65535, int(value)))
            return ctypes.c_uint16(clamped_value).value

        elif datatype.upper() in ["DINT", "INT32"]:
            # Ensure proper int32 type for PLC compatibility
            clamped_value = max(-2147483648, min(2147483647, int(value)))
            return ctypes.c_int32(clamped_value).value

        elif datatype.upper() in ["UDINT", "DWORD"]:
            # Ensure proper uint32 type for PLC compatibility
            clamped_value = max(0, min(4294967295, int(value)))
            return ctypes.c_uint32(clamped_value).value

        elif datatype.upper() == "LINT":
            return int(value)  # int64

        elif datatype.upper() in ["ULINT", "LWORD"]:
            # Ensure proper uint64 type for PLC compatibility
            clamped_value = max(0, min(18446744073709551615, int(value)))
            return ctypes.c_uint64(clamped_value).value

        elif datatype.upper() in ["FLOAT", "REAL", "LREAL"]:
            # Return the actual float value. The STruC++ debug-write surface
            # (debug_write_value) encodes it via ctypes c_float / c_double, so
            # it expects the real value here, NOT the integer IEEE-754 bit
            # pattern. The old MatIEC raw-bytes surface needed the bit-packed
            # int; double-packing it here corrupted REAL/LREAL writes
            # (e.g. 280.0 was written as ~1.13e9).
            return float(value)

        elif datatype.upper() in ["STRING", "WSTRING"]:
            return str(value)

        elif datatype.upper() == "TIME":
            # OPC-UA Int64 ns -> strucpp int64 ns (identity duration).
            return int(value)

        elif datatype.upper() == "TOD":
            # DateTime -> strucpp int64 nanoseconds since midnight.
            dt = _as_utc(value)
            if dt is None:
                return int(value) if isinstance(value, (int, float)) else 0
            return (
                (dt.hour * 3600 + dt.minute * 60 + dt.second) * 1_000_000_000
                + dt.microsecond * 1000
            )

        elif datatype.upper() == "DATE":
            # DateTime -> strucpp int64 DAYS since the 1970 epoch.
            dt = _as_utc(value)
            if dt is None:
                return int(value) if isinstance(value, (int, float)) else 0
            return (dt - _EPOCH).days

        elif datatype.upper() == "DT":
            # DateTime -> strucpp int64 nanoseconds since the 1970 epoch.
            dt = _as_utc(value)
            if dt is None:
                return int(value) if isinstance(value, (int, float)) else 0
            delta = dt - _EPOCH
            total_us = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
            return total_us * 1000

        else:
            # For unknown types, try to preserve the value
            return value

    except (ValueError, TypeError, OverflowError) as e:
        # If conversion fails, log and return a safe default
        log_warn(f"Failed to convert value {value} to {datatype}, using default: {e}")
        if datatype.upper() == "BOOL":
            return 0
        elif datatype.upper() in ["FLOAT", "REAL"]:
            return 0
        elif datatype.upper() in ["STRING", "WSTRING"]:
            return ""
        elif datatype.upper() in TIME_DATATYPES:
            return 0
        else:
            return 0


def infer_var_type(size: int) -> str:
    """
    Infer variable type from size.

    Args:
        size: Size of the variable in bytes

    Returns:
        String indicating the inferred type or type category
    """
    if size == 1:
        return "BOOL_OR_SINT"
    elif size == 2:
        return "UINT16"
    elif size == 4:
        return "UINT32_OR_TIME"
    elif size == 8:
        return "UINT64_OR_TIME"
    elif size == 127:
        # IEC_STRING: 1 byte len + 126 bytes body = 127 bytes
        return "STRING"
    else:
        return "UNKNOWN"
