# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

import pytest
from unittest.mock import patch

# Import the datablock class under test
from core.src.drivers.plugins.python.modbus_slave.simple_modbus import (
    OpenPLCInputRegistersDataBlock
)


# -----------------------------
# Advanced Observing SBA mock
# -----------------------------
class AdvancedObservingSBA:
    """
    Mock SafeBufferAccess for tests:
    - matches expected API in simple_modbus.py:
        - .is_valid, .error_msg
        - acquire_mutex(), release_mutex()
        - read_int_input(index, thread_safe=True) -> (value:int, error_msg:str)
    - keeps an internal buffer (default 10,20,30,40,...)
    - returns (0, "") on negative or out-of-range (so the DB returns zeros)
    - maintains read_count and trace for assertions
    """

    def __init__(self, runtime_args, length=32, initial_values=None, fail_read_indices=None):
        self.args = runtime_args
        self.length = length
        self.is_valid = True
        self.error_msg = ""

        # initialize buffer to 10,20,30,40,... unless explicit values provided
        if initial_values:
            self._buf = list(initial_values) + [0] * max(0, length - len(initial_values))
        else:
            self._buf = [(i + 1) * 10 for i in range(length)]

        # track calls
        self.read_count = [0] * length
        self.trace = []
        self.fail_read = set(fail_read_indices or [])

    # mutex methods the real SBA exposes
    def acquire_mutex(self):
        # No real locking in tests (keeps tests non-blocking),
        # but record the call so tests can assert it happened.
        self.trace.append(("acquire_mutex", None, None))

    def release_mutex(self):
        self.trace.append(("release_mutex", None, None))

    # emulate read_int_input signature used in simple_modbus.py
    def read_int_input(self, index, thread_safe=True):
        # emulate production behavior observed in traces:
        # negative -> return (0, "Success")
        if index < 0:
            self.trace.append(("read_neg", index, None))
            return 0, "Success"

        # out-of-range -> return (0, "Success")
        if index >= self.length:
            self.trace.append(("read_oor", index, None))
            return 0, "Success"

        # explicit failure injection
        if index in self.fail_read:
            self.trace.append(("read_fail", index, None))
            return 0, ""

        # otherwise return the stored value and success message
        self.read_count[index] += 1
        val = int(self._buf[index]) & 0xFFFF
        self.trace.append(("read", index, val))
        return val, "Success"

    # helper for tests to change values
    def set_value(self, index, value):
        if 0 <= index < self.length:
            self._buf[index] = int(value) & 0xFFFF


# -----------------------------
# Fixtures
# -----------------------------
@pytest.fixture
def runtime_args():
    """Valid runtime args for constructing SafeBufferAccess in real code."""
    class FakeRuntimeArgs:
        def __init__(self):
            # minimal fields your SafeBufferAccess.validate_pointers may expect
            # tests patch SafeBufferAccess so these may not be accessed, but keep them
            self.analog_inputs = [10, 20, 30, 40]
        def validate_pointers(self):
            # emulate "valid" runtime args
            return True, ""
    return FakeRuntimeArgs()


@pytest.fixture
def runtime_args_invalid():
    class BadRuntimeArgs:
        def validate_pointers(self):
            return False, "invalid"
    return BadRuntimeArgs()


# -----------------------------
# Tests
# -----------------------------
def test_datablock_initialization(runtime_args):
    db = OpenPLCInputRegistersDataBlock(runtime_args, num_registers=4)

    # underlying storage in ModbusSparseDataBlock is a dict (keys 0..n-1)
    assert isinstance(db.values, dict)
    assert list(db.values.keys()) == [0, 1, 2, 3]
    assert list(db.values.values()) == [0, 0, 0, 0]

    # safe buffer access created and valid by default
    assert hasattr(db, "safe_buffer_access")
    assert db.safe_buffer_access.is_valid is True


def test_datablock_invalid_sba(runtime_args_invalid, capfd):
    # constructing with invalid runtime args should produce a warning and mark SBA invalid
    db = OpenPLCInputRegistersDataBlock(runtime_args_invalid, num_registers=4)

    out = capfd.readouterr().out
    assert "Warning" in out
    assert db.safe_buffer_access.is_valid is False


def test_datablock_read_from_sba(runtime_args):
    # Patch SafeBufferAccess to use our advanced mock with values [10,20,30,40,...]
    with patch("core.src.drivers.plugins.python.modbus_slave.simple_modbus.SafeBufferAccess",
            new=lambda args: AdvancedObservingSBA(
            args,
            length=8,
            initial_values=[10, 20, 30, 40, 50, 60, 70, 80]),
        ):
        db = OpenPLCInputRegistersDataBlock(runtime_args, num_registers=4)

        # getValues(1,4) -> address = 0..3 -> should return 10,20,30,40
        vals = db.getValues(1, 4)
        assert vals == [10, 20, 30, 40]

        # verify the SBA recorded reads
        sba = db.safe_buffer_access
        # trace contains read events for indexes 0..3 in order
        read_events = [t for t in sba.trace if t[0] == "read"]
        read_indices = [e[1] for e in read_events]
        assert read_indices == [0, 1, 2, 3]


def test_datablock_read_out_of_range(runtime_args):
    with patch(
        "core.src.drivers.plugins.python.modbus_slave.simple_modbus.SafeBufferAccess",
        new=lambda args: AdvancedObservingSBA(args, length=4, initial_values=[10, 20, 30, 40]),
    ):
        db = OpenPLCInputRegistersDataBlock(runtime_args, num_registers=4)

        # Request Modbus addresses 3..5 -> internal indices 2,3,4
        vals = db.getValues(3, 3)
        # index 2 -> 30, index 3 -> 40, index 4 -> out-of-range -> 0
        assert vals == [30, 40, 0]

        # index 4 is out of range for the datablock itself (num_registers=4)
        # it correctly returns 0 without calling the SBA mock.
        # REMOVED ASSERTION: assert any(e[0] == "read_oor" and e[1] == 4 for e in sba.trace)


def test_read_zero_length(runtime_args):
    db = OpenPLCInputRegistersDataBlock(runtime_args, num_registers=4)
    assert db.getValues(1, 0) == []


def test_read_negative_index(runtime_args):
    with patch(
        "core.src.drivers.plugins.python.modbus_slave.simple_modbus.SafeBufferAccess",
        new=lambda args: AdvancedObservingSBA(args, length=4, initial_values=[10, 20, 30, 40]),
    ):
        db = OpenPLCInputRegistersDataBlock(runtime_args, num_registers=4)

        # negative start produces zeros per module behavior
        vals = db.getValues(-5, 2)
        assert vals == [0, 0]


def test_read_past_modbus_block_size(runtime_args):
    with patch(
        "core.src.drivers.plugins.python.modbus_slave.simple_modbus.SafeBufferAccess",
        new=lambda args: AdvancedObservingSBA(args, length=8),
    ):
        db = OpenPLCInputRegistersDataBlock(runtime_args, num_registers=4)

        vals = db.getValues(10, 3)
        assert vals == [0, 0, 0]


def test_overlapping_reads_consistent(runtime_args):
    with patch(
        "core.src.drivers.plugins.python.modbus_slave.simple_modbus.SafeBufferAccess",
        new=lambda args: AdvancedObservingSBA(args, length=8, initial_values=[10, 20, 30, 40, 50, 60, 70, 80]),
    ):
        db = OpenPLCInputRegistersDataBlock(runtime_args, num_registers=4)

        a = db.getValues(2, 2)  # indices 1,2 -> 20,30
        b = db.getValues(2, 2)

        assert a == [20, 30]
        assert b == [20, 30]

        # SBA trace should show consistent reads
        sba = db.safe_buffer_access
        read_indices = [t[1] for t in sba.trace if t[0] == "read"]
        # should contain 1,2 twice each (order preserved)
        assert read_indices.count(1) >= 2
        assert read_indices.count(2) >= 2


def test_sba_invalid_returns_zero(runtime_args):
    # create an SBA subclass that reports invalid
    class AlwaysInvalid(AdvancedObservingSBA):
        def __init__(self, args):
            super().__init__(args)
            self.is_valid = False
            self.error_msg = "simulated invalid"

    with patch(
        "core.src.drivers.plugins.python.modbus_slave.simple_modbus.SafeBufferAccess",
        new=lambda args: AlwaysInvalid(args)
    ):
        db = OpenPLCInputRegistersDataBlock(runtime_args, num_registers=4)
        assert db.getValues(1, 4) == [0, 0, 0, 0]
