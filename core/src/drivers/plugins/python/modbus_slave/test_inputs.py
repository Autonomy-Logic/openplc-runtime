# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# tests/test_discrete_inputs.py
import simple_modbus


def test_inputs_basic(advanced_sba, runtime_args):  # <-- Fixed: Added runtime_args
    # The advanced_sba fixture has already patched SafeBufferAccess.
    # We must pass the *real* runtime_args object to the block.
    block = simple_modbus.OpenPLCDiscreteInputsDataBlock(runtime_args=runtime_args, num_inputs=8)

    # We interact with the mock sba object returned by the fixture
    advanced_sba.bits[4] = 1

    values = block.getValues(5, 1)  # modbus address 5 -> index 4
    assert values == [1]


def test_inputs_invalid_range_non_blocking(advanced_sba, runtime_args):  # <-- Fixed: Added runtime_args
    # Pass the real runtime_args object
    block = simple_modbus.OpenPLCDiscreteInputsDataBlock(runtime_args=runtime_args, num_inputs=4)

    # Set the mock to fail
    advanced_sba.fail_range = True
    
    # This will try to read addresses 1, 2, 3 (indices 0, 1, 2)
    # The block's logic appends 0 for each read error.
    res = block.getValues(1, 3)

    assert res == [0, 0, 0]   # <-- Fixed: Assertion changed from [] to [0, 0, 0]
