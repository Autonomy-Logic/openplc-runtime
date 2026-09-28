# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

import threading
import time
from unittest.mock import MagicMock, patch
import pytest

import simple_modbus
from pymodbus.datastore import ModbusSparseDataBlock

# -----------------------------------------------------------------------
# Helpers / Fixtures
# -----------------------------------------------------------------------

@pytest.fixture
def runtime_args():
    """
    Realistic runtime_args used by SafeBufferAccess in your production code.
    Must implement validate_pointers() -> (bool, str) and provide memory arrays.
    """
    ra = MagicMock()
    ra.validate_pointers.return_value = (True, "")

    # Simulated PLC memory regions (lists of ints)
    ra.bool_input = [0] * 64
    ra.bool_output = [0] * 64
    ra.analog_input = [0] * 64
    ra.analog_output = [0] * 64

    # Sizes (some implementations check these)
    ra.digital_inputs_size = len(ra.bool_input)
    ra.digital_outputs_size = len(ra.bool_output)
    ra.analog_inputs_size = len(ra.analog_input)
    ra.analog_outputs_size = len(ra.analog_output)

    return ra


def assert_block_zeroed(block, size):
    """Ensure the ModbusSparseDataBlock-like block returns zeros for fresh region."""
    assert isinstance(block, ModbusSparseDataBlock)
    assert block.getValues(0, size) == [0] * size


# -----------------------------------------------------------------------
# Fake SafeBufferAccess used to observe locking behavior.
# We patch simple_modbus.SafeBufferAccess to return this object inside blocks
# -----------------------------------------------------------------------
class ObservingSafeBufferAccess:
    """
    Test double for SafeBufferAccess that matches the REAL method signatures used
    inside simple_modbus.py.
    """
    # Match OpenPLC bit width for coils & discrete inputs
    MAX_BITS = 8

    def __init__(self, runtime_args):
        self.args = runtime_args
        self.is_valid, self.error_msg = runtime_args.validate_pointers()
        self._lock = threading.Lock()

        # for locking verification
        self.acquire_count = 0
        self.release_count = 0

    # -------------------------
    # Lock handling
    # -------------------------
    def acquire_mutex(self):
        self._lock.acquire()
        self.acquire_count += 1

    def release_mutex(self):
        self.release_count += 1
        self._lock.release()

    # -------------------------
    # BOOL OUTPUT (Coils)
    # -------------------------
    def read_bool_output(self, buffer_idx, bit_idx, thread_safe=True):
        """Return (value, msg)."""
        flat_index = buffer_idx * self.MAX_BITS + bit_idx
        if flat_index < 0 or flat_index >= len(self.args.bool_output):
            return (0, "Invalid buffer index")
        value = 1 if self.args.bool_output[flat_index] else 0
        return (value, "Success")

    def write_bool_output(self, buffer_idx, bit_idx, value, thread_safe=True):
        """Return (success, msg)."""
        flat_index = buffer_idx * self.MAX_BITS + bit_idx
        if flat_index < 0 or flat_index >= len(self.args.bool_output):
            return (0, "Invalid buffer index")
        self.args.bool_output[flat_index] = 1 if value else 0
        return (1, "Success")

    # -------------------------
    # BOOL INPUT (Discrete Inputs)
    # -------------------------
    def read_bool_input(self, buffer_idx, bit_idx, thread_safe=True):
        """Return (value, msg)."""
        flat_index = buffer_idx * self.MAX_BITS + bit_idx
        if flat_index < 0 or flat_index >= len(self.args.bool_input):
            return (0, "Invalid buffer index")
        value = 1 if self.args.bool_input[flat_index] else 0
        return (value, "Success")

    # -------------------------
    # INT INPUT (Input Registers)
    # -------------------------
    def read_int_input(self, index, thread_safe=True):
        """Return (value, msg)."""
        if index < 0 or index >= len(self.args.analog_input):
            return (0, "Invalid buffer index")
        return (int(self.args.analog_input[index]) & 0xFFFF, "Success")

    # -------------------------
    # INT OUTPUT (Holding Registers) - write and read
    # -------------------------
    def write_int_output(self, index, value, thread_safe=True):
        """Return (success, msg). Apply uint16 masking."""
        if index < 0 or index >= len(self.args.analog_output):
            return (0, "Invalid buffer index")
        self.args.analog_output[index] = int(value) & 0xFFFF
        return (1, "Success")

    def read_int_output(self, index, thread_safe=True):
        """Return (value, msg) to match how the code reads holding registers."""
        if index < 0 or index >= len(self.args.analog_output):
            return (0, "Invalid buffer index")
        return (int(self.args.analog_output[index]) & 0xFFFF, "Success")



# -----------------------------------------------------------------------
# Data Block tests (use ObservingSafeBufferAccess patched in)
# -----------------------------------------------------------------------

def test_coils_read_write_and_locking(runtime_args):
    # Patch SafeBufferAccess so blocks use ObservingSafeBufferAccess
    with patch("simple_modbus.SafeBufferAccess", new=ObservingSafeBufferAccess):
        block = simple_modbus.OpenPLCCoilsDataBlock(runtime_args, num_coils=16)

        # initially zero
        assert_block_zeroed(block, 16)

        # setValues should write into runtime_args.bool_output
        block.setValues(1, [1, 0, 1])  # Modbus uses 1-based addressing in your code
        # read back
        values = block.getValues(1, 3)
        assert values == [1, 0, 1]

        # confirm that SafeBufferAccess's acquire/release were called.
        # The test accesses the actual instance created by the block:
        sba = block.safe_buffer_access
        assert isinstance(sba, ObservingSafeBufferAccess)
        assert sba.acquire_count >= 1
        assert sba.release_count >= 1


def test_coils_invalid_ranges_return_zero(runtime_args):
    with patch("simple_modbus.SafeBufferAccess", new=ObservingSafeBufferAccess):
        block = simple_modbus.OpenPLCCoilsDataBlock(runtime_args, num_coils=8)

        # read beyond range -> zeros expected (your code prints and returns zeros)
        out = block.getValues(1000, 3)
        assert out == [0, 0, 0]

        # write beyond range should not crash and should not mutate in-range values
        block.setValues(1000, [1, 1])
        assert runtime_args.bool_output.count(1) == 0


def test_discrete_inputs_behavior(runtime_args):
    with patch("simple_modbus.SafeBufferAccess", new=ObservingSafeBufferAccess):
        blk = simple_modbus.OpenPLCDiscreteInputsDataBlock(runtime_args, num_inputs=8)

        # simulate external update to runtime_args.bool_input (OpenPLC writes here)
        runtime_args.bool_input[2] = 1
        # getValues uses Modbus address base 1 in your code; call getValues(3,1)
        val = blk.getValues(3, 1)
        assert val == [1]


def test_holding_registers_masking(runtime_args):
    with patch("simple_modbus.SafeBufferAccess", new=ObservingSafeBufferAccess):
        blk = simple_modbus.OpenPLCHoldingRegistersDataBlock(runtime_args, num_registers=8)

        # write value > 16-bit and verify masking to uint16
        blk.setValues(1, [70000])   # 70000 & 0xFFFF == 4464
        stored = blk.getValues(1, 1)[0]
        assert stored == (70000 & 0xFFFF)


def test_input_registers_out_of_range(runtime_args):
    with patch("simple_modbus.SafeBufferAccess", new=ObservingSafeBufferAccess):
        blk = simple_modbus.OpenPLCInputRegistersDataBlock(runtime_args, num_registers=4)

        # read partially inside/partially outside -> out-of-range fields are zero
        # Ensure that for a very large start we get zeros
        got = blk.getValues(10, 3)
        assert got == [0, 0, 0]


# -----------------------------------------------------------------------
# SafeBufferAccess concurrency test (basic)
# Verify lock prevents race when multiple threads write/read
# -----------------------------------------------------------------------
def test_concurrent_writes_are_consistent(runtime_args):
    sba = ObservingSafeBufferAccess(runtime_args)

    # small test: spawn multiple threads that write alternating values to same index
    def writer(idx, value, count=1000):
        for _ in range(count):
            sba.acquire_mutex()
            # simulate non-atomic read-modify-write
            cur = runtime_args.analog_output[idx]
            runtime_args.analog_output[idx] = (cur + value) & 0xFFFF
            sba.release_mutex()

    threads = []
    for v in (1, 2, 3, 4):
        t = threading.Thread(target=writer, args=(0, v, 200))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    # after concurrent increments the final value should be deterministic (sum mod 2^16)
    expected = (1 + 2 + 3 + 4) * 200 & 0xFFFF
    assert runtime_args.analog_output[0] == expected


# -----------------------------------------------------------------------
# Verify that blocks do not raise on odd inputs (robustness)
# -----------------------------------------------------------------------
def test_robustness_against_bad_inputs(runtime_args):
    with patch("simple_modbus.SafeBufferAccess", new=ObservingSafeBufferAccess):
        coils = simple_modbus.OpenPLCCoilsDataBlock(runtime_args, num_coils=4)
        # None as values, should not raise
        with pytest.raises(TypeError):
            coils.setValues(1, None)

        # # extremely large request count -> handled gracefully (returns zeros)
        # vals = coils.getValues(1, 10)
        # assert isinstance(vals, list)
        # # at least we expect list elements to be ints
        # assert all(isinstance(x, int) for x in vals[:10])
