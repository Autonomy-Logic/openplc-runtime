#!/usr/bin/env python3
"""
Example plugin for testing the updated python_plugin_get_symbols function
This demonstrates the expected functions that should be present in a Python plugin
"""

import time
import ctypes
from ctypes import *

# Import the correct type definitions
from python_plugin_types import (
    PluginRuntimeArgs, 
    safe_extract_runtime_args_from_capsule,
    SafeBufferAccess,
    PluginStructureValidator
)

# Global variable to track initialization
_initialized = False
_runtime_args = None
_safe_buffer_access = None
_runtime_args_capsule = None

def init(runtime_args_capsule):
    """
    Plugin initialization function
    Called once when the plugin is loaded
    
    Args:
        runtime_args_capsule: PyCapsule containing plugin_runtime_args_t structure
    """
    global _initialized, _runtime_args, _safe_buffer_access, _runtime_args_capsule
    _runtime_args_capsule = runtime_args_capsule
    
    print("Python plugin 'example_plugin' initializing...")
    
    try:
        # Print structure validation info for debugging
        print("Validating plugin structure alignment...")
        PluginStructureValidator.print_structure_info()
        
        # Extract runtime args from capsule using safe method
        runtime_args, error_msg = safe_extract_runtime_args_from_capsule(runtime_args_capsule)
        if runtime_args is None:
            print(f"✗ Failed to extract runtime args: {error_msg}")
            return False
        
        print(f"✓ Runtime arguments extracted successfully")
        
        # Safely access buffer size using validation
        buffer_size, size_error = runtime_args.safe_access_buffer_size()
        if buffer_size == -1:
            print(f"✗ Failed to access buffer size: {size_error}")
            return False
        
        print(f"  Buffer size: {buffer_size}")
        print(f"  Bits per buffer: {runtime_args.bits_per_buffer}")
        print(f"  Structure details: {runtime_args}")
        
        # Create safe buffer access wrapper
        _safe_buffer_access = SafeBufferAccess(runtime_args)
        if not _safe_buffer_access.is_valid:
            print(f"✗ Failed to create safe buffer access: {_safe_buffer_access.error_msg}")
            return False
        
        # Store runtime args for later use
        _runtime_args = runtime_args
        
        print("✓ Plugin initialized successfully")
        return True
        
    except Exception as e:
        print(f"✗ Plugin initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def start_loop():
    """
    Called when the plugin loop should start
    Optional function - not all plugins need this
    """

    current_args, error_msg = safe_extract_runtime_args_from_capsule(_runtime_args_capsule)

    global _runtime_args
    print("Plugin start_loop called")
    while True:
        time.sleep(0.1)
        addr = ctypes.addressof(current_args.bool_output[0][0])
        internal_safe_buffer_access = SafeBufferAccess(current_args)
        value, msg = internal_safe_buffer_access.safe_read_bool_output(0,0)
        print(f"Value at address 0x{addr:x}: {value} ({msg})")
    pass

def stop_loop():
    """
    Called when the plugin loop should stop
    Optional function - not all plugins need this
    """
    print("Plugin stop_loop called")
    pass

def cleanup():
    """
    Plugin cleanup function
    Called when the plugin is being unloaded
    Optional function - use for cleanup tasks
    """
    global _initialized, _runtime_args
    
    print("Plugin cleanup called")
    
    _initialized = False
    _runtime_args = None
    
    print("✓ Plugin cleaned up successfully")

if __name__ == "__main__":
    print("This is an example Python plugin for OpenPLC Runtime")
    print("Expected functions:")
    print("  - init(runtime_args_capsule) -> bool")
    print("  - start_loop() -> None (optional)")  
    print("  - stop_loop() -> None (optional)")
    print("  - run_cycle() -> None (optional)")
    print("  - cleanup() -> None (optional)")
    print()
    print("This file should be loaded by the OpenPLC plugin system")
