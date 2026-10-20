#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Plugin Structure Validator

This module provides validation and debugging tools for the PluginRuntimeArgs structure.
"""

import ctypes
import sys

# Import the PluginRuntimeArgs class
from .plugin_runtime_args import PluginRuntimeArgs

class PluginStructureValidator:
    """Validates structure alignment and provides debugging tools"""

    @staticmethod
    def validate_structure_alignment():
        """
        Validates that the Python ctypes structure has the expected size and alignment
        Returns: (bool, str, dict) - (is_valid, message, debug_info)
        """
        try:
            # Calculate expected structure size
            # This is platform-dependent but we can do basic checks
            struct_size = ctypes.sizeof(PluginRuntimeArgs)

            debug_info = {
                'structure_size': struct_size,
                'pointer_size': ctypes.sizeof(ctypes.c_void_p),
                'int_size': ctypes.sizeof(ctypes.c_int),
                'platform': sys.platform,
                'architecture': sys.maxsize > 2**32 and '64-bit' or '32-bit'
            }

            # Basic sanity checks
            expected_min_size = (
                13 * ctypes.sizeof(ctypes.c_void_p) +  # 13 buffer pointers
                2 * ctypes.sizeof(ctypes.c_void_p) +   # 2 function pointers
                1 * ctypes.sizeof(ctypes.c_void_p) +   # 1 mutex pointer
                2 * ctypes.sizeof(ctypes.c_int)        # 2 integers
            )

            if struct_size < expected_min_size:
                return False, f"Structure too small: {struct_size} < {expected_min_size}", debug_info

            # Check field offsets make sense
            buffer_size_offset = PluginRuntimeArgs.buffer_size.offset
            bits_per_buffer_offset = PluginRuntimeArgs.bits_per_buffer.offset

            if bits_per_buffer_offset <= buffer_size_offset:
                return False, "Field offsets are incorrect", debug_info

            debug_info['buffer_size_offset'] = buffer_size_offset
            debug_info['bits_per_buffer_offset'] = bits_per_buffer_offset

            return True, "Structure validation passed", debug_info

        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, f"Exception during validation: {e}", {}

    @staticmethod
    def print_structure_info():
        """Print detailed structure information for debugging"""
        is_valid, msg, debug_info = PluginStructureValidator.validate_structure_alignment()

        print("=== Plugin Structure Validation ===")
        print(f"Status: {'VALID' if is_valid else 'INVALID'}")
        print(f"Message: {msg}")
        print("\nStructure Details:")
        for key, value in debug_info.items():
            print(f"  {key}: {value}")

        print(f"\nField Offsets:")
        try:
            for field_name, field_type in PluginRuntimeArgs._fields_:
                offset = getattr(PluginRuntimeArgs, field_name).offset
                print(f"  {field_name}: offset {offset}")
        except (AttributeError, TypeError) as e:
            print(f"  Error getting field offsets: {e}")
