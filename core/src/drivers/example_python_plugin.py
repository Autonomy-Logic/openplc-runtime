#!/usr/bin/env python3
"""
Example plugin for testing the updated python_plugin_get_symbols function
This demonstrates the expected functions that should be present in a Python plugin
"""

import time
import ctypes
from ctypes import *

# Global variable to track initialization
_initialized = False
_runtime_args = None

# Define the runtime arguments structure matching the C structure
class PluginRuntimeArgs(ctypes.Structure):
    """Python ctypes structure matching plugin_runtime_args_t"""
    _fields_ = [
        # Buffer arrays (using POINTER type for arrays)
        ("bool_input", POINTER(POINTER(ctypes.c_bool * 8))),   # bool_input[BUFFER_SIZE][8] 
        ("bool_output", POINTER(POINTER(ctypes.c_bool * 8))),  # bool_output[BUFFER_SIZE][8]
        ("byte_input", POINTER(POINTER(ctypes.c_ubyte))),      # byte_input[BUFFER_SIZE]
        ("byte_output", POINTER(POINTER(ctypes.c_ubyte))),     # byte_output[BUFFER_SIZE]
        ("int_input", POINTER(POINTER(ctypes.c_uint16))),      # int_input[BUFFER_SIZE]
        ("int_output", POINTER(POINTER(ctypes.c_uint16))),     # int_output[BUFFER_SIZE]
        ("dint_input", POINTER(POINTER(ctypes.c_uint32))),     # dint_input[BUFFER_SIZE]
        ("dint_output", POINTER(POINTER(ctypes.c_uint32))),    # dint_output[BUFFER_SIZE]
        ("lint_input", POINTER(POINTER(ctypes.c_uint64))),     # lint_input[BUFFER_SIZE]
        ("lint_output", POINTER(POINTER(ctypes.c_uint64))),    # lint_output[BUFFER_SIZE]
        ("int_memory", POINTER(POINTER(ctypes.c_uint16))),     # int_memory[BUFFER_SIZE]
        ("dint_memory", POINTER(POINTER(ctypes.c_uint32))),    # dint_memory[BUFFER_SIZE]
        ("lint_memory", POINTER(POINTER(ctypes.c_uint64))),    # lint_memory[BUFFER_SIZE]
        
        # Mutex function pointers
        ("mutex_take", CFUNCTYPE(c_int, c_void_p)),           # int (*mutex_take)(pthread_mutex_t*)
        ("mutex_give", CFUNCTYPE(c_int, c_void_p)),           # int (*mutex_give)(pthread_mutex_t*)
        ("buffer_mutex", c_void_p),                           # pthread_mutex_t *buffer_mutex
        
        # Buffer size information
        ("buffer_size", c_int),                               # int buffer_size
        ("bits_per_buffer", c_int),                           # int bits_per_buffer
    ]

def extract_runtime_args_from_capsule(capsule):
    """Extract runtime arguments from PyCapsule"""
    if not hasattr(capsule, '__class__') or capsule.__class__.__name__ != 'PyCapsule':
        raise TypeError("Expected PyCapsule object")
    
    # Get the pointer from the capsule
    ptr = ctypes.pythonapi.PyCapsule_GetPointer(capsule, b"openplc_runtime_args")
    if not ptr:
        raise ValueError("Failed to extract pointer from capsule")
    
    # Cast the pointer to our structure type
    args_ptr = ctypes.cast(ptr, POINTER(PluginRuntimeArgs))
    return args_ptr.contents

def init(runtime_args_capsule):
    """
    Plugin initialization function
    Called once when the plugin is loaded
    
    Args:
        runtime_args_capsule: PyCapsule containing plugin_runtime_args_t structure
    """
    global _initialized, _runtime_args
    
    print("Python plugin 'example_plugin' initializing...")
    
    try:
        # Extract runtime args from capsule
        # runtime_args = extract_runtime_args_from_capsule(runtime_args_capsule)
        # print(f"✓ Runtime arguments extracted successfully")
        # print(f"  Buffer size: {runtime_args.buffer_size}")
        # print(f"  Bits per buffer: {runtime_args.bits_per_buffer}")
        
        # # Store runtime args for later use
        # _runtime_args = runtime_args
        # _initialized = True
        
        print("✓ Plugin initialized successfully")
        return True
        
    except Exception as e:
        print(f"✗ Plugin initialization failed: {e}")
        return False

def start_loop():
    """
    Called when the plugin loop should start
    Optional function - not all plugins need this
    """
    print("Plugin start_loop called")
    pass

def stop_loop():
    """
    Called when the plugin loop should stop
    Optional function - not all plugins need this
    """
    print("Plugin stop_loop called")
    pass

def run_cycle():
    """
    Main plugin cycle function
    Called periodically by the plugin system
    Optional function - some plugins may only need init
    """
    global _initialized, _runtime_args
    
    if not _initialized or not _runtime_args:
        return
    
    # Example: Toggle a digital output every cycle
    try:
        if _runtime_args.mutex_take(_runtime_args.buffer_mutex) == 0:
            # Toggle bool_output[0][0] 
            current_value = _runtime_args.bool_output[0][0]
            _runtime_args.bool_output[0][0] = not current_value
            print(f"Toggled output 0.0 to {not current_value}")
    except Exception as e:
        print(f"Error in run_cycle: {e}")
    finally:
        if _runtime_args.buffer_mutex:
            _runtime_args.mutex_give(_runtime_args.buffer_mutex)

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
