#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Capsule Extraction Utilities

This module provides safe extraction of PluginRuntimeArgs from PyCapsules.
"""

import ctypes

# Import the PluginRuntimeArgs class
from .plugin_runtime_args import PluginRuntimeArgs

def safe_extract_runtime_args_from_capsule(capsule):
    """
    Enhanced capsule extraction with comprehensive validation
    Args:
        capsule: PyCapsule containing plugin_runtime_args_t structure
    Returns:
        (PluginRuntimeArgs, str) - (runtime_args, error_message)
    """
    try:
        # Validate capsule type
        if not hasattr(capsule, '__class__') or capsule.__class__.__name__ != 'PyCapsule':
            return None, f"Expected PyCapsule object, got {type(capsule)}"

        # Set up the Python API function signatures
        ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
        ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p

        # Get the pointer from the capsule
        ptr = ctypes.pythonapi.PyCapsule_GetPointer(capsule, b"openplc_runtime_args")
        if not ptr:
            return None, "Failed to extract pointer from capsule - invalid capsule name or corrupted data"

        # Cast the pointer to our structure type
        args_ptr = ctypes.cast(ptr, ctypes.POINTER(PluginRuntimeArgs))
        if not args_ptr:
            return None, "Failed to cast pointer to PluginRuntimeArgs structure"

        runtime_args = args_ptr.contents

        # Validate the extracted structure
        is_valid, validation_msg = runtime_args.validate_pointers()
        if not is_valid:
            return None, f"Structure validation failed: {validation_msg}"

        return runtime_args, "Success"

    except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
        return None, f"Exception during capsule extraction: {e}"
