# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

import pytest
import threading
from core.src.drivers.plugins.python.modbus_slave import simple_modbus

MAX_BITS = 8   # matches OpenPLC bit grouping
MAX_REGS = 1   # word-aligned registers


class AdvancedObservingSBA:
    """
    Fully functional SafeBufferAccess mock:
    - Supports coils / discrete inputs (bool)
    - Supports holding / input registers (uint16)
    - Records lock/unlock calls
    - Supports initial values
    - Supports failure injection
    """

    def __init__(self, runtime_args, length=64, initial_values=None):
        self.length = length
        self.lock_count = 0
        self.unlock_count = 0
        self.fail_range = False

        # FIX 1: Added attributes required by datablock __init__
        self.is_valid = True
        self.error_msg = ""

        # REQUIRED by DataBlock
        self.bits_per_buffer = MAX_BITS
        self.buffer_size = length

        # bit storage (coils / discrete inputs)
        self.bits = [0] * (length * MAX_BITS)

        # register storage
        self.regs = [0] * length

        if initial_values:
            for i, v in enumerate(initial_values):
                if i < len(self.regs):
                    self.regs[i] = v

        self._lock = threading.Lock()

    # locking
    def lock(self):
        self.lock_count += 1
        self._lock.acquire()

    def unlock(self):
        self.unlock_count += 1
        try:
            self._lock.release()
        except RuntimeError:
            pass

    # FIX 2: Added public-facing mutex methods
    def acquire_mutex(self):
        self.lock()

    def release_mutex(self):
        self.unlock()

    def _check_range(self, idx):
        if self.fail_range:
            return False
        return 0 <= idx < len(self.bits)

    def _check_reg_range(self, idx):
        if self.fail_range:
            return False
        return 0 <= idx < len(self.regs)

    def validate_pointers(self):
        return True, ""

    # coils / discrete inputs
    def read_bool_output(self, buffer_idx, bit_idx, thread_safe=True):
        flat = buffer_idx * MAX_BITS + bit_idx
        if not self._check_range(flat):
            return 0, "range error"
        return self.bits[flat], "Success"  # <-- FIX 3: Return "Success"

    def write_bool_output(self, buffer_idx, bit_idx, value, thread_safe=True):
        flat = buffer_idx * MAX_BITS + bit_idx
        if not self._check_range(flat):
            return False, "range error"
        self.bits[flat] = int(bool(value))
        return True, "Success"  # <-- FIX 3: Return "Success"

    def read_bool_input(self, buffer_idx, bit_idx, thread_safe=True):
        flat = buffer_idx * MAX_BITS + bit_idx
        if not self._check_range(flat):
            return 0, "range error"
        return self.bits[flat], "Success"  # <-- FIX 3: Return "Success"

    # registers
    def read_uint16_input(self, idx, thread_safe=True):
        if not self._check_reg_range(idx):
            return 0, "range"
        return self.regs[idx], "Success"  # <-- FIX 3: Return "Success"

    def read_uint16_output(self, idx, thread_safe=True):
        if not self._check_reg_range(idx):
            return 0, "range"
        return self.regs[idx], "Success"  # <-- FIX 3: Return "Success"

    def write_uint16_output(self, idx, value, thread_safe=True):
        if not self._check_reg_range(idx):
            return False, "range"
        self.regs[idx] = value & 0xFFFF
        return True, "Success"  # <-- FIX 3: Return "Success"

    read_int_input = read_uint16_input
    write_int_output = write_uint16_output



# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def advanced_sba(runtime_args, monkeypatch): # <-- Added monkeypatch
    """
    Fixture used by test_inputs.py.
    Provides AdvancedObservingSBA and patches SafeBufferAccess to use it.
    """
    sba = AdvancedObservingSBA(runtime_args, length=32)
    runtime_args.sba = sba
    
    # Patch simple_modbus.SafeBufferAccess to return our mock sba
    monkeypatch.setattr(simple_modbus, "SafeBufferAccess", lambda args: sba)
    
    return sba


@pytest.fixture
def runtime_args():
    """Fake runtime args with a mock PLC buffer and proper lock."""
    class FakeRuntime:
        def __init__(self):
            self.analog_inputs = [100, 200, 300, 400]
            self.analog_outputs = [0, 0, 0, 0]
            self.bool_inputs = [0] * 64
            self.bool_outputs = [0] * 64
            # Add proper threading lock
            import threading
            self.lock = threading.RLock()
    return FakeRuntime()
