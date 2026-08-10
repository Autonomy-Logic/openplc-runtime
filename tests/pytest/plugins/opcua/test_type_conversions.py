"""
Unit tests for OPC-UA type conversion functions.

Tests the functions in opcua_utils.py:
- map_plc_to_opcua_type()
- convert_value_for_opcua()
- convert_value_for_plc()
- infer_var_type()
"""

import pytest
import sys
from pathlib import Path

# Add plugin path for imports
_plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "core" / "src" / "drivers" / "plugins" / "python"
sys.path.insert(0, str(_plugin_dir / "opcua"))

from opcua_utils import (
    map_plc_to_opcua_type,
    convert_value_for_opcua,
    convert_value_for_plc,
    infer_var_type,
    timespec_to_milliseconds,
    milliseconds_to_timespec,
    TIME_DATATYPES,
)
from asyncua import ua


class TestMapPlcToOpcuaType:
    """Tests for map_plc_to_opcua_type function."""

    def test_bool_mapping(self):
        """BOOL should map to Boolean."""
        assert map_plc_to_opcua_type("BOOL") == ua.VariantType.Boolean
        assert map_plc_to_opcua_type("bool") == ua.VariantType.Boolean
        assert map_plc_to_opcua_type("Bool") == ua.VariantType.Boolean

    def test_sint_mapping(self):
        """SINT should map to SByte (signed 8-bit integer)."""
        assert map_plc_to_opcua_type("SINT") == ua.VariantType.SByte
        assert map_plc_to_opcua_type("sint") == ua.VariantType.SByte

    def test_usint_mapping(self):
        """USINT should map to Byte (unsigned 8-bit integer)."""
        assert map_plc_to_opcua_type("USINT") == ua.VariantType.Byte
        assert map_plc_to_opcua_type("usint") == ua.VariantType.Byte

    def test_byte_mapping(self):
        """BYTE should map to Byte."""
        assert map_plc_to_opcua_type("BYTE") == ua.VariantType.Byte
        assert map_plc_to_opcua_type("byte") == ua.VariantType.Byte

    def test_uint_mapping(self):
        """UINT should map to UInt16 (unsigned 16-bit integer)."""
        assert map_plc_to_opcua_type("UINT") == ua.VariantType.UInt16
        assert map_plc_to_opcua_type("uint") == ua.VariantType.UInt16

    def test_word_mapping(self):
        """WORD should map to UInt16."""
        assert map_plc_to_opcua_type("WORD") == ua.VariantType.UInt16
        assert map_plc_to_opcua_type("word") == ua.VariantType.UInt16

    def test_int_mapping(self):
        """INT should map to Int16."""
        assert map_plc_to_opcua_type("INT") == ua.VariantType.Int16
        assert map_plc_to_opcua_type("int") == ua.VariantType.Int16

    def test_dint_mapping(self):
        """DINT should map to Int32."""
        assert map_plc_to_opcua_type("DINT") == ua.VariantType.Int32
        assert map_plc_to_opcua_type("dint") == ua.VariantType.Int32

    def test_int32_mapping(self):
        """INT32 should map to Int32."""
        assert map_plc_to_opcua_type("INT32") == ua.VariantType.Int32
        assert map_plc_to_opcua_type("int32") == ua.VariantType.Int32

    def test_udint_mapping(self):
        """UDINT should map to UInt32 (unsigned 32-bit integer)."""
        assert map_plc_to_opcua_type("UDINT") == ua.VariantType.UInt32
        assert map_plc_to_opcua_type("udint") == ua.VariantType.UInt32

    def test_dword_mapping(self):
        """DWORD should map to UInt32."""
        assert map_plc_to_opcua_type("DWORD") == ua.VariantType.UInt32
        assert map_plc_to_opcua_type("dword") == ua.VariantType.UInt32

    def test_lint_mapping(self):
        """LINT should map to Int64."""
        assert map_plc_to_opcua_type("LINT") == ua.VariantType.Int64
        assert map_plc_to_opcua_type("lint") == ua.VariantType.Int64

    def test_ulint_mapping(self):
        """ULINT should map to UInt64 (unsigned 64-bit integer)."""
        assert map_plc_to_opcua_type("ULINT") == ua.VariantType.UInt64
        assert map_plc_to_opcua_type("ulint") == ua.VariantType.UInt64

    def test_lword_mapping(self):
        """LWORD should map to UInt64."""
        assert map_plc_to_opcua_type("LWORD") == ua.VariantType.UInt64
        assert map_plc_to_opcua_type("lword") == ua.VariantType.UInt64

    def test_float_mapping(self):
        """FLOAT should map to Float."""
        assert map_plc_to_opcua_type("FLOAT") == ua.VariantType.Float
        assert map_plc_to_opcua_type("float") == ua.VariantType.Float

    def test_real_mapping(self):
        """REAL should map to Float (IEC 61131-3 REAL = 32-bit float)."""
        assert map_plc_to_opcua_type("REAL") == ua.VariantType.Float
        assert map_plc_to_opcua_type("real") == ua.VariantType.Float

    def test_lreal_mapping(self):
        """LREAL should map to Double (IEC 61131-3 LREAL = 64-bit float)."""
        assert map_plc_to_opcua_type("LREAL") == ua.VariantType.Double
        assert map_plc_to_opcua_type("lreal") == ua.VariantType.Double

    def test_string_mapping(self):
        """STRING should map to String."""
        assert map_plc_to_opcua_type("STRING") == ua.VariantType.String
        assert map_plc_to_opcua_type("string") == ua.VariantType.String

    def test_unknown_type_mapping(self):
        """Unknown types should map to Variant."""
        assert map_plc_to_opcua_type("UNKNOWN") == ua.VariantType.Variant
        assert map_plc_to_opcua_type("CUSTOM") == ua.VariantType.Variant

    # TIME type mappings
    def test_time_mapping(self):
        """TIME should map to Int64 (milliseconds)."""
        assert map_plc_to_opcua_type("TIME") == ua.VariantType.Int64
        assert map_plc_to_opcua_type("time") == ua.VariantType.Int64
        assert map_plc_to_opcua_type("Time") == ua.VariantType.Int64

    def test_tod_mapping(self):
        """TOD (Time of Day) should map to DateTime (current date + time)."""
        assert map_plc_to_opcua_type("TOD") == ua.VariantType.DateTime
        assert map_plc_to_opcua_type("tod") == ua.VariantType.DateTime

    def test_date_mapping(self):
        """DATE should map to DateTime."""
        assert map_plc_to_opcua_type("DATE") == ua.VariantType.DateTime
        assert map_plc_to_opcua_type("date") == ua.VariantType.DateTime

    def test_dt_mapping(self):
        """DT (Date and Time) should map to DateTime."""
        assert map_plc_to_opcua_type("DT") == ua.VariantType.DateTime
        assert map_plc_to_opcua_type("dt") == ua.VariantType.DateTime


class TestConvertValueForOpcua:
    """Tests for convert_value_for_opcua function."""

    # BOOL conversions
    def test_bool_from_true(self):
        """True values should convert to True."""
        assert convert_value_for_opcua("BOOL", True) is True
        assert convert_value_for_opcua("BOOL", 1) is True
        assert convert_value_for_opcua("BOOL", 100) is True

    def test_bool_from_false(self):
        """False/zero values should convert to False."""
        assert convert_value_for_opcua("BOOL", False) is False
        assert convert_value_for_opcua("BOOL", 0) is False

    # SINT conversions (signed 8-bit integer)
    def test_sint_normal_values(self):
        """Normal SINT values should pass through."""
        assert convert_value_for_opcua("SINT", 0) == 0
        assert convert_value_for_opcua("SINT", 50) == 50
        assert convert_value_for_opcua("SINT", -50) == -50

    def test_sint_boundary_values(self):
        """SINT boundary values should be preserved."""
        assert convert_value_for_opcua("SINT", 127) == 127
        assert convert_value_for_opcua("SINT", -128) == -128

    def test_sint_clamping(self):
        """SINT values outside range should be clamped."""
        assert convert_value_for_opcua("SINT", 200) == 127
        assert convert_value_for_opcua("SINT", -200) == -128

    # BYTE conversions
    def test_byte_normal_values(self):
        """Normal byte values should pass through."""
        assert convert_value_for_opcua("BYTE", 0) == 0
        assert convert_value_for_opcua("BYTE", 128) == 128
        assert convert_value_for_opcua("BYTE", 255) == 255

    def test_byte_clamping(self):
        """Byte values should be clamped to 0-255."""
        assert convert_value_for_opcua("BYTE", -1) == 0
        assert convert_value_for_opcua("BYTE", 256) == 255
        assert convert_value_for_opcua("BYTE", 1000) == 255

    # INT conversions
    def test_int_normal_values(self):
        """Normal INT values should pass through."""
        assert convert_value_for_opcua("INT", 0) == 0
        assert convert_value_for_opcua("INT", 1000) == 1000
        assert convert_value_for_opcua("INT", -1000) == -1000

    def test_int_boundary_values(self):
        """INT boundary values should be preserved."""
        assert convert_value_for_opcua("INT", 32767) == 32767
        assert convert_value_for_opcua("INT", -32768) == -32768

    def test_int_clamping(self):
        """INT values outside range should be clamped."""
        assert convert_value_for_opcua("INT", 40000) == 32767
        assert convert_value_for_opcua("INT", -40000) == -32768

    # DINT conversions
    def test_dint_normal_values(self):
        """Normal DINT values should pass through."""
        assert convert_value_for_opcua("DINT", 0) == 0
        assert convert_value_for_opcua("DINT", 100000) == 100000
        assert convert_value_for_opcua("DINT", -100000) == -100000

    def test_dint_boundary_values(self):
        """DINT boundary values should be preserved."""
        assert convert_value_for_opcua("DINT", 2147483647) == 2147483647
        assert convert_value_for_opcua("DINT", -2147483648) == -2147483648

    def test_int32_alias(self):
        """INT32 should behave same as DINT."""
        assert convert_value_for_opcua("INT32", 100000) == 100000
        assert convert_value_for_opcua("Int32", -100000) == -100000

    # LINT conversions
    def test_lint_normal_values(self):
        """Normal LINT values should pass through."""
        assert convert_value_for_opcua("LINT", 0) == 0
        assert convert_value_for_opcua("LINT", 1000000000) == 1000000000
        assert convert_value_for_opcua("LINT", -1000000000) == -1000000000

    def test_lint_large_values(self):
        """Large LINT values should be preserved."""
        assert convert_value_for_opcua("LINT", 9223372036854775807) == 9223372036854775807

    # FLOAT/REAL conversions
    def test_float_from_float(self):
        """Float values should pass through."""
        result = convert_value_for_opcua("FLOAT", 3.14159)
        assert abs(result - 3.14159) < 0.0001

    def test_float_from_int_is_the_number_not_a_bit_pattern(self):
        """An int reaching a FLOAT node is that number, widened.

        debug_read_value hands over a real float (c_float / c_double), so
        there is no bit pattern to decode. The MatIEC-era code reinterpreted
        ints through struct.unpack, which turned a REAL of 1 into 1.4e-45.
        """
        assert convert_value_for_opcua("FLOAT", 1) == 1.0
        assert convert_value_for_opcua("FLOAT", -7) == -7.0
        assert convert_value_for_opcua("FLOAT", 1109917696) == 1109917696.0

    def test_float_zero(self):
        """Zero float should work correctly."""
        assert convert_value_for_opcua("FLOAT", 0.0) == 0.0
        assert convert_value_for_opcua("FLOAT", 0) == 0.0

    def test_float_negative(self):
        """Negative floats should work correctly."""
        result = convert_value_for_opcua("FLOAT", -273.15)
        assert abs(result - (-273.15)) < 0.01

    # REAL conversions (IEC 61131-3 REAL = 32-bit float)
    def test_real_from_float(self):
        """REAL values should pass through as float."""
        result = convert_value_for_opcua("REAL", 3.14159)
        assert abs(result - 3.14159) < 0.0001

    def test_real_from_int_is_the_number_not_a_bit_pattern(self):
        """Same contract as FLOAT, for both REAL and LREAL."""
        assert convert_value_for_opcua("REAL", 1) == 1.0
        assert convert_value_for_opcua("LREAL", 42) == 42.0

    # LREAL conversions (IEC 61131-3 LREAL = 64-bit float)
    def test_lreal_from_float(self):
        """LREAL values should pass through as float."""
        result = convert_value_for_opcua("LREAL", -273.15)
        assert abs(result - (-273.15)) < 0.0001

    # STRING conversions
    def test_string_normal(self):
        """String values should pass through."""
        assert convert_value_for_opcua("STRING", "Hello") == "Hello"
        assert convert_value_for_opcua("STRING", "") == ""

    def test_string_from_other_types(self):
        """Non-string values should be converted to string."""
        assert convert_value_for_opcua("STRING", 123) == "123"

    # TIME conversions
    def test_time_from_tuple(self):
        """TIME from tuple (tv_sec, tv_nsec) should convert to milliseconds."""
        # 1.5 seconds = 1500 ms
        assert convert_value_for_opcua("TIME", (1, 500_000_000)) == 1500
        # 0 seconds
        assert convert_value_for_opcua("TIME", (0, 0)) == 0
        # 10.25 seconds = 10250 ms
        assert convert_value_for_opcua("TIME", (10, 250_000_000)) == 10250

    def test_time_from_int(self):
        """TIME from int should be treated as already milliseconds."""
        assert convert_value_for_opcua("TIME", 1500) == 1500
        assert convert_value_for_opcua("TIME", 0) == 0

    def test_tod_from_tuple(self):
        """TOD from tuple should convert to DateTime with current date + time."""
        from datetime import datetime, timezone

        # Capture the date before conversion to avoid flakiness across midnight
        today_before = datetime.now(timezone.utc).date()

        # 1 hour = 3600 seconds since midnight -> 01:00:00
        result = convert_value_for_opcua("TOD", (3600, 0))
        assert isinstance(result, datetime)
        assert result.hour == 1
        assert result.minute == 0
        assert result.second == 0
        # Date should correspond to the current date at the time of conversion,
        # allowing for the possibility that midnight passes during the test.
        today_after = datetime.now(timezone.utc).date()
        assert today_before <= result.date() <= today_after

        # 1 hour + 30 minutes + 45 seconds = 5445 seconds
        result2 = convert_value_for_opcua("TOD", (5445, 500_000_000))
        assert result2.hour == 1
        assert result2.minute == 30
        assert result2.second == 45
        assert result2.microsecond == 500000  # 500ms = 500000 microseconds

    def test_time_large_values(self):
        """TIME should handle large values (hours/days)."""
        # 24 hours = 86400 seconds = 86400000 ms
        assert convert_value_for_opcua("TIME", (86400, 0)) == 86400000
        # 1 day + 1 hour + 1 minute + 1.5 seconds
        tv_sec = 86400 + 3600 + 60 + 1
        assert convert_value_for_opcua("TIME", (tv_sec, 500_000_000)) == (tv_sec * 1000 + 500)


class TestConvertValueForPlc:
    """Tests for convert_value_for_plc function."""

    # BOOL conversions
    def test_bool_from_python_bool(self):
        """Python bool should convert to int 0/1."""
        assert convert_value_for_plc("BOOL", True) == 1
        assert convert_value_for_plc("BOOL", False) == 0

    def test_bool_from_int(self):
        """Integer should convert to 0/1."""
        assert convert_value_for_plc("BOOL", 1) == 1
        assert convert_value_for_plc("BOOL", 0) == 0
        assert convert_value_for_plc("BOOL", 100) == 1

    def test_bool_from_string(self):
        """String bool representations should convert."""
        assert convert_value_for_plc("BOOL", "true") == 1
        assert convert_value_for_plc("BOOL", "false") == 0
        assert convert_value_for_plc("BOOL", "1") == 1
        assert convert_value_for_plc("BOOL", "0") == 0

    # BYTE conversions
    def test_byte_normal_values(self):
        """Normal byte values should pass through."""
        assert convert_value_for_plc("BYTE", 0) == 0
        assert convert_value_for_plc("BYTE", 128) == 128
        assert convert_value_for_plc("BYTE", 255) == 255

    def test_byte_clamping(self):
        """Byte values should be clamped to 0-255."""
        assert convert_value_for_plc("BYTE", -1) == 0
        assert convert_value_for_plc("BYTE", 256) == 255

    # INT conversions
    def test_int_normal_values(self):
        """Normal INT values should pass through."""
        assert convert_value_for_plc("INT", 0) == 0
        assert convert_value_for_plc("INT", 1000) == 1000
        assert convert_value_for_plc("INT", -1000) == -1000

    def test_int_clamping(self):
        """INT values outside range should be clamped."""
        assert convert_value_for_plc("INT", 40000) == 32767
        assert convert_value_for_plc("INT", -40000) == -32768

    # DINT conversions
    def test_dint_normal_values(self):
        """Normal DINT values should pass through."""
        assert convert_value_for_plc("DINT", 0) == 0
        assert convert_value_for_plc("DINT", 100000) == 100000
        assert convert_value_for_plc("DINT", -100000) == -100000

    # LINT conversions
    def test_lint_normal_values(self):
        """Normal LINT values should pass through."""
        assert convert_value_for_plc("LINT", 0) == 0
        assert convert_value_for_plc("LINT", 1000000000) == 1000000000

    # FLOAT conversions
    def test_float_stays_a_float(self):
        """Floats are handed over as numbers, not as their bit pattern.

        debug_write_value encodes with c_float / c_double, so packing the bit
        pattern here would store the pattern as the value.
        """
        assert convert_value_for_plc("FLOAT", 3.5) == 3.5
        assert convert_value_for_plc("FLOAT", -273.15) == -273.15

    def test_float_zero(self):
        """Zero float should stay zero."""
        assert convert_value_for_plc("FLOAT", 0.0) == 0.0

    def test_float_from_int_is_widened_not_reinterpreted(self):
        """A client sending an int to a Float node means that number."""
        assert convert_value_for_plc("FLOAT", 3) == 3.0

    # REAL conversions (IEC 61131-3 REAL = 32-bit float)
    def test_real_stays_a_float(self):
        """REAL and LREAL follow the same contract as FLOAT."""
        assert convert_value_for_plc("REAL", 3.5) == 3.5
        assert convert_value_for_plc("LREAL", -273.15) == -273.15

    def test_real_write_of_42_is_not_a_bit_pattern(self):
        """Regression: forum thread "Error changing values via OPC UA".

        A client writing 42 saw 1.109918e+09 on a REAL and
        4.6311077918204232e+18 on an LREAL -- the IEEE-754 bit patterns of
        42.0 stored as the value itself.
        """
        assert convert_value_for_plc("REAL", 42.0) == 42.0
        assert convert_value_for_plc("REAL", 42.0) != 1109917696
        assert convert_value_for_plc("LREAL", 42.0) == 42.0
        assert convert_value_for_plc("LREAL", 42.0) != 4631107791820423168

    # STRING conversions
    def test_string_normal(self):
        """String values should pass through."""
        assert convert_value_for_plc("STRING", "Hello") == "Hello"
        assert convert_value_for_plc("STRING", "") == ""

    # TIME conversions (OPC-UA milliseconds -> PLC timespec tuple)
    def test_time_to_tuple(self):
        """TIME milliseconds should convert to (tv_sec, tv_nsec) tuple."""
        # 1500 ms = 1.5 seconds
        assert convert_value_for_plc("TIME", 1500) == (1, 500_000_000)
        # 0 ms
        assert convert_value_for_plc("TIME", 0) == (0, 0)
        # 10250 ms = 10.25 seconds
        assert convert_value_for_plc("TIME", 10250) == (10, 250_000_000)

    def test_tod_to_tuple(self):
        """TOD DateTime should convert to (tv_sec, tv_nsec) tuple (seconds since midnight)."""
        from datetime import datetime, timezone

        # 01:00:00 = 3600 seconds since midnight
        dt1 = datetime(2025, 6, 15, 1, 0, 0, tzinfo=timezone.utc)
        assert convert_value_for_plc("TOD", dt1) == (3600, 0)

        # 01:30:45.500000 = 5445 seconds + 500000 microseconds
        dt2 = datetime(2025, 6, 15, 1, 30, 45, 500000, tzinfo=timezone.utc)
        result = convert_value_for_plc("TOD", dt2)
        assert result[0] == 5445  # seconds since midnight
        assert result[1] == 500_000_000  # nanoseconds

        # Midnight = 0 seconds
        dt3 = datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        assert convert_value_for_plc("TOD", dt3) == (0, 0)

    def test_time_large_values_to_tuple(self):
        """TIME should handle large milliseconds values."""
        # 86400000 ms = 24 hours
        assert convert_value_for_plc("TIME", 86400000) == (86400, 0)


class TestInferVarType:
    """Tests for infer_var_type function."""

    def test_size_1_byte(self):
        """1-byte variables could be BOOL or SINT."""
        assert infer_var_type(1) == "BOOL_OR_SINT"

    def test_size_2_bytes(self):
        """2-byte variables are likely UINT16/INT."""
        assert infer_var_type(2) == "UINT16"

    def test_size_4_bytes(self):
        """4-byte variables could be UINT32, DINT, or TIME."""
        assert infer_var_type(4) == "UINT32_OR_TIME"

    def test_size_8_bytes(self):
        """8-byte variables could be UINT64, LINT, or TIME."""
        assert infer_var_type(8) == "UINT64_OR_TIME"

    def test_size_127_bytes(self):
        """127-byte variables are IEC_STRING (1 byte len + 126 bytes body)."""
        assert infer_var_type(127) == "STRING"

    def test_unknown_size(self):
        """Unknown sizes should return UNKNOWN."""
        assert infer_var_type(3) == "UNKNOWN"
        assert infer_var_type(16) == "UNKNOWN"
        assert infer_var_type(0) == "UNKNOWN"


class TestRoundTripConversions:
    """
    Tests that verify values can be converted from PLC -> OPC-UA -> PLC
    without loss of data (within type constraints).
    """

    def test_bool_roundtrip(self):
        """BOOL values should survive round-trip conversion."""
        for val in [True, False]:
            opcua_val = convert_value_for_opcua("BOOL", int(val))
            plc_val = convert_value_for_plc("BOOL", opcua_val)
            assert plc_val == int(val)

    def test_sint_roundtrip(self):
        """SINT values should survive round-trip conversion."""
        for val in [0, 1, -1, 50, -50, 127, -128]:
            opcua_val = convert_value_for_opcua("SINT", val)
            plc_val = convert_value_for_plc("SINT", opcua_val)
            assert plc_val == val

    def test_byte_roundtrip(self):
        """BYTE values should survive round-trip conversion."""
        for val in [0, 1, 127, 128, 255]:
            opcua_val = convert_value_for_opcua("BYTE", val)
            plc_val = convert_value_for_plc("BYTE", opcua_val)
            assert plc_val == val

    def test_int_roundtrip(self):
        """INT values should survive round-trip conversion."""
        for val in [0, 1, -1, 1000, -1000, 32767, -32768]:
            opcua_val = convert_value_for_opcua("INT", val)
            plc_val = convert_value_for_plc("INT", opcua_val)
            assert plc_val == val

    def test_dint_roundtrip(self):
        """DINT values should survive round-trip conversion."""
        for val in [0, 100000, -100000, 2147483647, -2147483648]:
            opcua_val = convert_value_for_opcua("DINT", val)
            plc_val = convert_value_for_plc("DINT", opcua_val)
            assert plc_val == val

    def test_lint_roundtrip(self):
        """LINT values should survive round-trip conversion."""
        for val in [0, 1000000000, -1000000000]:
            opcua_val = convert_value_for_opcua("LINT", val)
            plc_val = convert_value_for_plc("LINT", opcua_val)
            assert plc_val == val

    def test_float_roundtrip(self):
        """FLOAT values should survive round-trip conversion."""
        for val in [0.0, 3.14159, -273.15, 1000000.5, 42.0]:
            opcua_val = convert_value_for_opcua("FLOAT", val)
            plc_val = convert_value_for_plc("FLOAT", opcua_val)
            assert abs(plc_val - val) < 0.0001

    def test_real_roundtrip(self):
        """REAL and LREAL values should survive round-trip conversion."""
        for datatype in ("REAL", "LREAL"):
            for val in [0.0, 3.14159, -273.15, 1000000.5, 42.0]:
                opcua_val = convert_value_for_opcua(datatype, val)
                plc_val = convert_value_for_plc(datatype, opcua_val)
                assert abs(plc_val - val) < 0.0001

    def test_string_roundtrip(self):
        """STRING values should survive round-trip conversion."""
        for val in ["", "Hello", "Test!@#$%", "OpenPLC Runtime"]:
            opcua_val = convert_value_for_opcua("STRING", val)
            plc_val = convert_value_for_plc("STRING", opcua_val)
            assert plc_val == val

    def test_time_roundtrip(self):
        """TIME values should survive round-trip conversion (PLC tuple -> OPC-UA ms -> PLC tuple)."""
        test_values = [
            (0, 0),           # Zero
            (1, 0),           # 1 second
            (1, 500_000_000), # 1.5 seconds
            (10, 250_000_000), # 10.25 seconds
            (3600, 0),        # 1 hour
            (86400, 0),       # 24 hours
        ]
        for tv_sec, tv_nsec in test_values:
            # Convert PLC tuple to OPC-UA milliseconds
            opcua_val = convert_value_for_opcua("TIME", (tv_sec, tv_nsec))
            # Convert back to PLC tuple
            plc_val = convert_value_for_plc("TIME", opcua_val)
            # Compare (note: nanosecond precision is truncated to milliseconds)
            expected_sec = tv_sec
            expected_nsec = (tv_nsec // 1_000_000) * 1_000_000  # Truncate to ms precision
            assert plc_val == (expected_sec, expected_nsec)

    def test_tod_roundtrip(self):
        """TOD values should survive round-trip conversion."""
        test_values = [
            (0, 0),           # Midnight
            (3600, 0),        # 1:00 AM
            (43200, 0),       # Noon
            (43200, 500_000_000), # Noon + 500ms
        ]
        for tv_sec, tv_nsec in test_values:
            opcua_val = convert_value_for_opcua("TOD", (tv_sec, tv_nsec))
            plc_val = convert_value_for_plc("TOD", opcua_val)
            expected_sec = tv_sec
            expected_nsec = (tv_nsec // 1_000_000) * 1_000_000
            assert plc_val == (expected_sec, expected_nsec)


class TestTimespecConversionHelpers:
    """Tests for TIME conversion helper functions."""

    def test_timespec_to_milliseconds_basic(self):
        """Basic conversion: 1 second = 1000 ms."""
        assert timespec_to_milliseconds(1, 0) == 1000
        assert timespec_to_milliseconds(0, 0) == 0
        assert timespec_to_milliseconds(10, 0) == 10000

    def test_timespec_to_milliseconds_with_nanoseconds(self):
        """Conversion with nanoseconds: 1.5 sec = 1500 ms."""
        assert timespec_to_milliseconds(1, 500_000_000) == 1500
        assert timespec_to_milliseconds(0, 100_000_000) == 100
        assert timespec_to_milliseconds(2, 750_000_000) == 2750

    def test_timespec_to_milliseconds_truncates_submillisecond(self):
        """Sub-millisecond nanoseconds should be truncated."""
        # 999999 ns = 0.999999 ms, should truncate to 0 ms
        assert timespec_to_milliseconds(0, 999_999) == 0
        # 1000000 ns = 1 ms
        assert timespec_to_milliseconds(0, 1_000_000) == 1

    def test_milliseconds_to_timespec_basic(self):
        """Basic reverse conversion."""
        assert milliseconds_to_timespec(1000) == (1, 0)
        assert milliseconds_to_timespec(0) == (0, 0)
        assert milliseconds_to_timespec(10000) == (10, 0)

    def test_milliseconds_to_timespec_with_remainder(self):
        """Conversion with fractional seconds."""
        assert milliseconds_to_timespec(1500) == (1, 500_000_000)
        assert milliseconds_to_timespec(100) == (0, 100_000_000)
        assert milliseconds_to_timespec(2750) == (2, 750_000_000)

    def test_roundtrip_conversion(self):
        """Roundtrip conversion should preserve millisecond precision."""
        for ms in [0, 1, 100, 999, 1000, 1500, 10000, 86400000]:
            tv_sec, tv_nsec = milliseconds_to_timespec(ms)
            result = timespec_to_milliseconds(tv_sec, tv_nsec)
            assert result == ms

    def test_large_time_values(self):
        """Large values should work correctly."""
        # 24 hours in seconds = 86400
        assert timespec_to_milliseconds(86400, 0) == 86_400_000
        assert milliseconds_to_timespec(86_400_000) == (86400, 0)

        # 1 week in milliseconds
        week_ms = 7 * 24 * 60 * 60 * 1000
        tv_sec, tv_nsec = milliseconds_to_timespec(week_ms)
        assert tv_sec == 7 * 24 * 60 * 60
        assert tv_nsec == 0


class TestTimeDatatypesConstant:
    """Tests for TIME_DATATYPES constant."""

    def test_time_datatypes_contains_all_time_types(self):
        """TIME_DATATYPES should contain all time-related types."""
        assert "TIME" in TIME_DATATYPES
        assert "DATE" in TIME_DATATYPES
        assert "TOD" in TIME_DATATYPES
        assert "DT" in TIME_DATATYPES

    def test_time_datatypes_is_frozen(self):
        """TIME_DATATYPES should be immutable."""
        assert isinstance(TIME_DATATYPES, frozenset)
