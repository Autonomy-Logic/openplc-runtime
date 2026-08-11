"""
Round-trip tests for the OPC-UA write path.

A client write travels

    client value
      -> convert_value_for_plc()      (opcua_utils.py)
      -> debug_write_value()          (opcua_memory.py, encodes with ctypes)
      -> PLC memory

and comes back through debug_read_value() -> convert_value_for_opcua().
The two ends were tested in isolation but never as a pair, which is how a
float could be packed into its integer bit pattern on the way in and read
back as that pattern: a client writing 42 to a REAL stored 1109917696.0
(forum thread "Error changing values via OPC UA").

These tests drive the real functions against a fake `args` whose debug_read /
debug_write keep the bytes in a dict, exactly like the runtime keeps them in
PLC memory.
"""

import ctypes
import sys
from pathlib import Path

import pytest

# Add plugin path for imports
_plugin_dir = (
    Path(__file__).parent.parent.parent.parent.parent
    / "core"
    / "src"
    / "drivers"
    / "plugins"
    / "python"
)
sys.path.insert(0, str(_plugin_dir / "opcua"))

from opcua_memory import STATUS_OK, debug_read_value, debug_write_value
from opcua_utils import convert_value_for_opcua, convert_value_for_plc


class FakePlcMemory:
    """Stands in for the runtime's debug surface.

    Stores whatever bytes debug_write hands over, keyed by (arr, elem), and
    replays them on debug_read -- the same contract plc_main implements.
    """

    def __init__(self):
        self.cells = {}

    def debug_write(self, arr, elem, buf, size):
        key = (
            int(arr.value if hasattr(arr, "value") else arr),
            int(elem.value if hasattr(elem, "value") else elem),
        )
        length = int(size.value if hasattr(size, "value") else size)
        self.cells[key] = bytes(buf[i] for i in range(length))
        return STATUS_OK

    def debug_read(self, arr, elem, buf):
        key = (
            int(arr.value if hasattr(arr, "value") else arr),
            int(elem.value if hasattr(elem, "value") else elem),
        )
        raw = self.cells.get(key)
        if raw is None:
            return 0
        for i, byte in enumerate(raw):
            buf[i] = byte
        return len(raw)


@pytest.fixture
def plc():
    return FakePlcMemory()


def write_then_read(plc, datatype, client_value):
    """Full client-write -> PLC -> client-read cycle for one variable."""
    plc_value = convert_value_for_plc(datatype, client_value)
    assert debug_write_value(plc, 0, 0, datatype, plc_value) is True
    stored = debug_read_value(plc, 0, 0, datatype)
    return convert_value_for_opcua(datatype, stored)


class TestFloatingPointWrites:
    """Regression: forum thread "Error changing values via OPC UA"."""

    def test_real_write_of_42_round_trips(self, plc):
        assert write_then_read(plc, "REAL", 42.0) == 42.0

    def test_lreal_write_of_42_round_trips(self, plc):
        assert write_then_read(plc, "LREAL", 42.0) == 42.0

    def test_real_does_not_store_the_bit_pattern(self, plc):
        """The exact failure mode: 42.0 stored as 1109917696.0."""
        assert write_then_read(plc, "REAL", 42.0) != 1109917696.0

    def test_lreal_does_not_store_the_bit_pattern(self, plc):
        assert write_then_read(plc, "LREAL", 42.0) != 4.6311077918204232e18

    @pytest.mark.parametrize("value", [0.0, 1.5, -273.15, 1000000.5])
    def test_lreal_keeps_full_double_precision(self, plc, value):
        assert write_then_read(plc, "LREAL", value) == value

    @pytest.mark.parametrize("value", [0.0, 1.5, -273.0, 42.0])
    def test_real_keeps_float32_representable_values(self, plc, value):
        # Values chosen to be exact in float32, so the comparison is exact.
        assert write_then_read(plc, "REAL", value) == value

    def test_real_narrows_to_float32(self, plc):
        """A REAL is 32-bit: the stored value is the float32 rounding of the
        client's double, not the double itself."""
        result = write_then_read(plc, "REAL", 3.14159)
        assert result == pytest.approx(3.14159, abs=1e-6)

    def test_int_written_to_a_float_node_is_the_number(self, plc):
        """A client may send an integer variant to a Float node."""
        assert write_then_read(plc, "REAL", 42) == 42.0


class TestIntegerWritesStillWork:
    """INT was never affected; guard against fixing floats by breaking ints."""

    @pytest.mark.parametrize(
        "datatype,value",
        [
            ("INT", 42),
            ("INT", -32768),
            ("INT", 32767),
            ("DINT", -2147483648),
            ("UINT", 65535),
            ("LINT", 9223372036854775807),
            ("BOOL", True),
            ("BOOL", False),
        ],
    )
    def test_integer_round_trip(self, plc, datatype, value):
        result = write_then_read(plc, datatype, value)
        expected = bool(value) if datatype == "BOOL" else value
        assert result == expected


class TestEncoding:
    """The bytes that reach PLC memory are the IEC-typed encoding."""

    def test_real_writes_four_bytes_of_float32(self, plc):
        debug_write_value(plc, 0, 0, "REAL", convert_value_for_plc("REAL", 42.0))
        assert plc.cells[(0, 0)] == bytes(ctypes.c_float(42.0))

    def test_lreal_writes_eight_bytes_of_float64(self, plc):
        debug_write_value(plc, 0, 0, "LREAL", convert_value_for_plc("LREAL", 42.0))
        assert plc.cells[(0, 0)] == bytes(ctypes.c_double(42.0))
