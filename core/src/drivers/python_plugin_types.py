#!/usr/bin/env python3
"""
Shared type definitions for OpenPLC Python plugins
This module provides correct ctypes mappings for the plugin_runtime_args_t structure
"""

import ctypes
from ctypes import *
import sys

# IEC type mappings based on iec_types.h
# These must match exactly with the C definitions
IEC_BOOL = ctypes.c_uint8    # typedef uint8_t IEC_BOOL;
IEC_BYTE = ctypes.c_uint8    # typedef uint8_t IEC_BYTE;
IEC_UINT = ctypes.c_uint16   # typedef uint16_t IEC_UINT;
IEC_UDINT = ctypes.c_uint32  # typedef uint32_t IEC_UDINT;
IEC_ULINT = ctypes.c_uint64  # typedef uint64_t IEC_ULINT;

class PluginRuntimeArgs(ctypes.Structure):
    """
    Python ctypes structure matching plugin_runtime_args_t from plugin_driver.h
    
    CRITICAL: This structure must match the C definition exactly to prevent
    segmentation faults and memory corruption.
    """
    _fields_ = [
        # Buffer arrays - these are pointers to arrays of pointers
        # C: IEC_BOOL *(*bool_input)[8] means pointer to array of 8 pointers
        ("bool_input", ctypes.POINTER(ctypes.POINTER(IEC_BOOL) * 8)),
        ("bool_output", ctypes.POINTER(ctypes.POINTER(IEC_BOOL) * 8)),
        ("byte_input", ctypes.POINTER(ctypes.POINTER(IEC_BYTE))),
        ("byte_output", ctypes.POINTER(ctypes.POINTER(IEC_BYTE))),
        ("int_input", ctypes.POINTER(ctypes.POINTER(IEC_UINT))),
        ("int_output", ctypes.POINTER(ctypes.POINTER(IEC_UINT))),
        ("dint_input", ctypes.POINTER(ctypes.POINTER(IEC_UDINT))),
        ("dint_output", ctypes.POINTER(ctypes.POINTER(IEC_UDINT))),
        ("lint_input", ctypes.POINTER(ctypes.POINTER(IEC_ULINT))),
        ("lint_output", ctypes.POINTER(ctypes.POINTER(IEC_ULINT))),
        ("int_memory", ctypes.POINTER(ctypes.POINTER(IEC_UINT))),
        ("dint_memory", ctypes.POINTER(ctypes.POINTER(IEC_UDINT))),
        ("lint_memory", ctypes.POINTER(ctypes.POINTER(IEC_ULINT))),
        
        # Mutex function pointers
        ("mutex_take", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)),
        ("mutex_give", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)),
        ("buffer_mutex", ctypes.c_void_p),
        
        # Buffer size information
        ("buffer_size", ctypes.c_int),
        ("bits_per_buffer", ctypes.c_int),
    ]
    
    def validate_pointers(self):
        """
        Validate that critical pointers are not NULL
        Returns: (bool, str) - (is_valid, error_message)
        """
        try:
            # Check buffer mutex
            if not self.buffer_mutex:
                return False, "buffer_mutex is NULL"
            
            # Check mutex functions
            if not self.mutex_take:
                return False, "mutex_take function pointer is NULL"
            if not self.mutex_give:
                return False, "mutex_give function pointer is NULL"
            
            # Check buffer size is reasonable
            if self.buffer_size <= 0 or self.buffer_size > 10000:
                return False, f"buffer_size is invalid: {self.buffer_size}"
            
            if self.bits_per_buffer <= 0 or self.bits_per_buffer > 64:
                return False, f"bits_per_buffer is invalid: {self.bits_per_buffer}"
            
            return True, "All pointers valid"
            
        except Exception as e:
            return False, f"Exception during validation: {e}"
    
    def safe_access_buffer_size(self):
        """
        Safely access buffer_size with validation
        Returns: (int, str) - (buffer_size, error_message)
        """
        try:
            is_valid, msg = self.validate_pointers()
            if not is_valid:
                return -1, f"Validation failed: {msg}"
            
            size = self.buffer_size
            if size <= 0 or size > 10000:
                return -1, f"Invalid buffer size: {size}"
            
            return size, "Success"
            
        except Exception as e:
            return -1, f"Exception accessing buffer_size: {e}"
    
    def __str__(self):
        """Debug representation of the structure"""
        try:
            return (f"PluginRuntimeArgs(\n"
                    f"  bool_input=0x{ctypes.addressof(self.bool_input.contents) if self.bool_input else 0:x},\n"
                    f"  bool_output=0x{ctypes.addressof(self.bool_output.contents) if self.bool_output else 0:x},\n"
                    f"  byte_input=0x{ctypes.addressof(self.byte_input.contents) if self.byte_input else 0:x},\n"
                    f"  byte_output=0x{ctypes.addressof(self.byte_output.contents) if self.byte_output else 0:x},\n"
                    f"  int_input=0x{ctypes.addressof(self.int_input.contents) if self.int_input else 0:x},\n"
                    f"  int_output=0x{ctypes.addressof(self.int_output.contents) if self.int_output else 0:x},\n"
                    f"  dint_input=0x{ctypes.addressof(self.dint_input.contents) if self.dint_input else 0:x},\n"
                    f"  dint_output=0x{ctypes.addressof(self.dint_output.contents) if self.dint_output else 0:x},\n"
                    f"  lint_input=0x{ctypes.addressof(self.lint_input.contents) if self.lint_input else 0:x},\n"
                    f"  lint_output=0x{ctypes.addressof(self.lint_output.contents) if self.lint_output else 0:x},\n"
                    f"  int_memory=0x{ctypes.addressof(self.int_memory.contents) if self.int_memory else 0:x},\n"
                    f"  buffer_size={self.buffer_size},\n"
                    f"  bits_per_buffer={self.bits_per_buffer},\n"
                    f"  buffer_mutex=0x{self.buffer_mutex or 0:x},\n"
                    f"  mutex_take={'valid' if self.mutex_take else 'NULL'},\n"
                    f"  mutex_give={'valid' if self.mutex_give else 'NULL'}\n"
                    f")")
        except:
            return "PluginRuntimeArgs(corrupted or invalid)"

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
            
        except Exception as e:
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
        except Exception as e:
            print(f"  Error getting field offsets: {e}")

class SafeBufferAccess:
    """Wrapper class for safe buffer operations with mutex handling"""
    
    def __init__(self, runtime_args):
        """
        Initialize with validated runtime args
        Args:
            runtime_args: PluginRuntimeArgs instance
        """
        self.args = runtime_args
        self.is_valid, self.error_msg = runtime_args.validate_pointers()
    
    def safe_read_bool_output(self, buffer_idx, bit_idx):
        """
        Safely read a boolean output with proper mutex handling
        Returns: (bool, str) - (value, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex
            if self.args.mutex_take(self.args.buffer_mutex) != 0:
                return False, "Failed to acquire mutex"
            
            try:
                # Validate indices
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return False, f"Invalid buffer index: {buffer_idx}"
                if bit_idx < 0 or bit_idx >= self.args.bits_per_buffer:
                    return False, f"Invalid bit index: {bit_idx}"
                
                # Access the value - read from the actual value, not the pointer
                value = bool(self.args.bool_output[buffer_idx][bit_idx].contents.value)
                return value, "Success"
                
            finally:
                # Always release mutex
                self.args.mutex_give(self.args.buffer_mutex)
                
        except Exception as e:
            return False, f"Exception during buffer access: {e}"
    
    def safe_write_bool_output(self, buffer_idx, bit_idx, value):
        """
        Safely write a boolean output with proper mutex handling
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex
            if self.args.mutex_take(self.args.buffer_mutex) != 0:
                return False, "Failed to acquire mutex"
            
            try:
                # Validate indices
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return False, f"Invalid buffer index: {buffer_idx}"
                if bit_idx < 0 or bit_idx >= self.args.bits_per_buffer:
                    return False, f"Invalid bit index: {bit_idx}"
                
                # Set the value - access the actual value, not the pointer
                self.args.bool_output[buffer_idx][bit_idx].contents.value = 1 if value else 0
                return True, "Success"
                
            finally:
                # Always release mutex
                self.args.mutex_give(self.args.buffer_mutex)
                
        except Exception as e:
            return False, f"Exception during buffer access: {e}"

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
        
    except Exception as e:
        return None, f"Exception during capsule extraction: {e}"

if __name__ == "__main__":
    # Self-test when run directly
    print("OpenPLC Python Plugin Types - Self Test")
    print("=" * 50)
    
    # Test structure validation
    PluginStructureValidator.print_structure_info()
    
    print(f"\nIEC Type Sizes:")
    print(f"  IEC_BOOL: {ctypes.sizeof(IEC_BOOL)} bytes")
    print(f"  IEC_BYTE: {ctypes.sizeof(IEC_BYTE)} bytes") 
    print(f"  IEC_UINT: {ctypes.sizeof(IEC_UINT)} bytes")
    print(f"  IEC_UDINT: {ctypes.sizeof(IEC_UDINT)} bytes")
    print(f"  IEC_ULINT: {ctypes.sizeof(IEC_ULINT)} bytes")
