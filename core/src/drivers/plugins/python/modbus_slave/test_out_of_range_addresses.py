import threading

import pytest
import simple_modbus
from pymodbus.constants import ExcCodes

MAX_BITS = 8


class FakeBufferAccess:
    """SafeBufferAccess stand-in with separate output and memory regions."""

    def __init__(self, nbytes=64, nregs=64):
        self.is_valid = True
        self.error_msg = ""
        self.bits_per_buffer = MAX_BITS
        self.buffer_size = nbytes
        self.bool_output = [0] * (nbytes * MAX_BITS)
        self.bool_memory = [0] * (nbytes * MAX_BITS)
        self.int_output = [0] * nregs
        self.int_memory = [0] * nregs
        self.dint_memory = [0] * nregs
        self.lint_memory = [0] * nregs
        self._lock = threading.RLock()

    def acquire_mutex(self):
        self._lock.acquire()

    def release_mutex(self):
        try:
            self._lock.release()
        except RuntimeError:
            pass

    def validate_pointers(self):
        return True, ""

    @staticmethod
    def _flat(buffer_idx, bit_idx):
        return buffer_idx * MAX_BITS + bit_idx

    def _read_bit(self, store, buffer_idx, bit_idx):
        flat = self._flat(buffer_idx, bit_idx)
        if flat >= len(store):
            return 0, "range error"
        return store[flat], "Success"

    def _write_bit(self, store, buffer_idx, bit_idx, value):
        flat = self._flat(buffer_idx, bit_idx)
        if flat >= len(store):
            return False, "range error"
        store[flat] = int(bool(value))
        return True, "Success"

    def read_bool_output(self, buffer_idx, bit_idx, thread_safe=True):
        return self._read_bit(self.bool_output, buffer_idx, bit_idx)

    def write_bool_output(self, buffer_idx, bit_idx, value, thread_safe=True):
        return self._write_bit(self.bool_output, buffer_idx, bit_idx, value)

    def read_bool_memory(self, buffer_idx, bit_idx, thread_safe=True):
        return self._read_bit(self.bool_memory, buffer_idx, bit_idx)

    def write_bool_memory(self, buffer_idx, bit_idx, value, thread_safe=True):
        return self._write_bit(self.bool_memory, buffer_idx, bit_idx, value)

    def read_bool_input(self, buffer_idx, bit_idx, thread_safe=True):
        return 0, "Success"

    def _read_reg(self, store, idx):
        if idx >= len(store):
            return 0, "range error"
        return store[idx], "Success"

    def _write_reg(self, store, idx, value):
        if idx >= len(store):
            return False, "range error"
        store[idx] = value
        return True, "Success"

    def read_int_output(self, idx, thread_safe=True):
        return self._read_reg(self.int_output, idx)

    def write_int_output(self, idx, value, thread_safe=True):
        return self._write_reg(self.int_output, idx, value)

    def read_int_memory(self, idx, thread_safe=True):
        return self._read_reg(self.int_memory, idx)

    def write_int_memory(self, idx, value, thread_safe=True):
        return self._write_reg(self.int_memory, idx, value)

    def read_dint_memory(self, idx, thread_safe=True):
        return self._read_reg(self.dint_memory, idx)

    def write_dint_memory(self, idx, value, thread_safe=True):
        return self._write_reg(self.dint_memory, idx, value)

    def read_lint_memory(self, idx, thread_safe=True):
        return self._read_reg(self.lint_memory, idx)

    def write_lint_memory(self, idx, value, thread_safe=True):
        return self._write_reg(self.lint_memory, idx, value)

    def read_int_input(self, idx, thread_safe=True):
        return 0, "Success"

    read_uint16_input = read_int_input
    read_uint16_output = read_int_output
    write_uint16_output = write_int_output


@pytest.fixture
def buffers(monkeypatch):
    fake = FakeBufferAccess()
    monkeypatch.setattr(simple_modbus, "SafeBufferAccess", lambda args: fake)
    return fake


def coils(qx_bits=16, mx_bits=0):
    return simple_modbus.OpenPLCSegmentedCoilsDataBlock(None, qx_bits=qx_bits, mx_bits=mx_bits)


def holding(qw=4, mw=4, md=0, ml=0):
    return simple_modbus.OpenPLCSegmentedHoldingRegistersDataBlock(
        None, qw_count=qw, mw_count=mw, md_count=md, ml_count=ml
    )


class TestSegmentedCoilsOutOfRange:
    def test_write_to_mx_is_refused_when_mx_is_disabled(self, buffers):
        block = coils(qx_bits=16, mx_bits=0)

        result = block.setValues(17, [True])

        assert result == ExcCodes.ILLEGAL_ADDRESS
        assert buffers.bool_memory[0] == 0

    def test_read_of_mx_is_refused_when_mx_is_disabled(self, buffers):
        block = coils(qx_bits=16, mx_bits=0)

        assert block.getValues(17, 1) == ExcCodes.ILLEGAL_ADDRESS

    def test_write_reaches_bool_memory_when_mx_is_enabled(self, buffers):
        block = coils(qx_bits=16, mx_bits=8)

        assert block.setValues(17, [True]) is None
        assert buffers.bool_memory[0] == 1
        assert block.getValues(17, 1) == [1]

    def test_qx_write_is_unaffected(self, buffers):
        block = coils(qx_bits=16, mx_bits=0)

        assert block.setValues(1, [True]) is None
        assert buffers.bool_output[0] == 1
        assert block.getValues(1, 1) == [1]

    def test_request_straddling_the_boundary_fails_as_a_whole(self, buffers):
        block = coils(qx_bits=16, mx_bits=0)

        assert block.getValues(16, 2) == ExcCodes.ILLEGAL_ADDRESS
        assert block.setValues(16, [True, True]) == ExcCodes.ILLEGAL_ADDRESS
        assert buffers.bool_output[15] == 0

    def test_negative_address_is_refused(self, buffers):
        block = coils(qx_bits=16, mx_bits=0)

        assert block.getValues(0, 1) == ExcCodes.ILLEGAL_ADDRESS


class TestSegmentedHoldingRegistersOutOfRange:
    def test_read_past_the_last_segment_is_refused(self, buffers):
        block = holding(qw=4, mw=4)

        assert block.getValues(9, 1) == ExcCodes.ILLEGAL_ADDRESS

    def test_write_past_the_last_segment_is_refused(self, buffers):
        block = holding(qw=4, mw=4)

        assert block.setValues(9, [1234]) == ExcCodes.ILLEGAL_ADDRESS
        assert buffers.int_memory[4:] == [0] * (len(buffers.int_memory) - 4)

    def test_write_inside_mw_still_works(self, buffers):
        block = holding(qw=4, mw=4)

        assert block.setValues(5, [1234]) is None
        assert buffers.int_memory[0] == 1234


class TestEchoCacheHonoursRefusedWrites:
    def test_refused_write_is_not_echoed_as_success(self, buffers):
        device = simple_modbus.OpenPLCDeviceContext(co=coils(qx_bits=16, mx_bits=0))

        result = device.setValues(5, 16, [True])

        assert result == ExcCodes.ILLEGAL_ADDRESS
        assert device._pending_writes == {}

    def test_accepted_write_is_still_echoed(self, buffers):
        device = simple_modbus.OpenPLCDeviceContext(co=coils(qx_bits=16, mx_bits=8))

        assert device.setValues(5, 16, [True]) is None
        assert device.getValues(5, 16, 1) == [True]
