#!/usr/bin/env python3
"""
Shared type definitions for OpenPLC Python plugins
This module provides correct ctypes mappings for the plugin_runtime_args_t structure
"""

import ctypes
from ctypes import *
import json
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
        ("plugin_specific_config_file_path", ctypes.c_char * 256),
        
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
            
        except (AttributeError, TypeError) as e:
            return False, f"Structure access error during validation: {e}"
        except (ValueError, OverflowError) as e:
            return False, f"Value validation error: {e}"
        except OSError as e:
            return False, f"System error during validation: {e}"
    
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
            
        except (AttributeError, TypeError) as e:
            return -1, f"Structure access error: {e}"
        except (ValueError, OverflowError) as e:
            return -1, f"Value validation error: {e}"
        except OSError as e:
            return -1, f"System error accessing buffer_size: {e}"
    
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
    
    @staticmethod
    def _handle_buffer_exception(exception, operation_name):
        """
        Centralized exception handling for buffer operations
        Args:
            exception: The caught exception
            operation_name: Name of the operation that failed
        Returns:
            str: Formatted error message
        """
        if isinstance(exception, (AttributeError, TypeError)):
            return f"Structure access error during {operation_name}: {exception}"
        elif isinstance(exception, (ValueError, OverflowError)):
            return f"Value validation error during {operation_name}: {exception}"
        elif isinstance(exception, OSError):
            return f"System error during {operation_name}: {exception}"
        elif isinstance(exception, MemoryError):
            return f"Memory error during {operation_name}: {exception}"
        else:
            return f"Unexpected error during {operation_name}: {exception}"
    
    def read_bool_input(self, buffer_idx, bit_idx, thread_safe=True):
        """
        Safely read a boolean input with optional mutex handling
        Args:
            buffer_idx: Buffer index
            bit_idx: Bit index within buffer
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (bool, str) - (value, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return False, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate indices
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return False, f"Invalid buffer index: {buffer_idx}"
                if bit_idx < 0 or bit_idx >= self.args.bits_per_buffer:
                    return False, f"Invalid bit index: {bit_idx}"
                
                # Access the value - read from the actual value, not the pointer
                value = bool(self.args.bool_input[buffer_idx][bit_idx].contents.value)
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_buffer_exception(e, "buffer access")

    def read_bool_output(self, buffer_idx, bit_idx, thread_safe=True):
        """
        Safely read a boolean output with optional mutex handling
        Args:
            buffer_idx: Buffer index
            bit_idx: Bit index within buffer
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (bool, str) - (value, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return False, "Failed to acquire mutex"
                mutex_acquired = True
            
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
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_buffer_exception(e, "buffer access")
    
    def write_bool_output(self, buffer_idx, bit_idx, value, thread_safe=True):
        """
        Safely write a boolean output with optional mutex handling
        Args:
            buffer_idx: Buffer index
            bit_idx: Bit index within buffer
            value: Boolean value to write
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return False, "Failed to acquire mutex"
                mutex_acquired = True
            
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
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_buffer_exception(e, "buffer access")
    
    # Byte buffer access functions
    def read_byte_input(self, buffer_idx, thread_safe=True):
        """
        Safely read a byte input with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.byte_input[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    def write_byte_output(self, buffer_idx, value, thread_safe=True):
        """
        Safely write a byte output with optional mutex handling
        Args:
            buffer_idx: Buffer index
            value: Byte value to write (0-255)
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Validate value range
            if not (0 <= value <= 255):
                return False, f"Invalid byte value: {value} (must be 0-255)"
            
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return False, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return False, f"Invalid buffer index: {buffer_idx}"
                
                # Set the value
                self.args.byte_output[buffer_idx].contents.value = value
                return True, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_buffer_exception(e, "buffer access")
    
    def read_byte_output(self, buffer_idx, thread_safe=True):
        """
        Safely read a byte output with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.byte_output[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    # Int buffer access functions (IEC_UINT - 16-bit)
    def read_int_input(self, buffer_idx, thread_safe=True):
        """
        Safely read an int input with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.int_input[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    def write_int_output(self, buffer_idx, value, thread_safe=True):
        """
        Safely write an int output with optional mutex handling
        Args:
            buffer_idx: Buffer index
            value: Int value to write (0-65535)
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Validate value range
            if not (0 <= value <= 65535):
                return False, f"Invalid int value: {value} (must be 0-65535)"
            
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return False, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return False, f"Invalid buffer index: {buffer_idx}"
                
                # Set the value
                self.args.int_output[buffer_idx].contents.value = value
                return True, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_buffer_exception(e, "buffer access")
    
    def read_int_output(self, buffer_idx, thread_safe=True):
        """
        Safely read an int output with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.int_output[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    # Dint buffer access functions (IEC_UDINT - 32-bit)
    def read_dint_input(self, buffer_idx, thread_safe=True):
        """
        Safely read a dint input with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.dint_input[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    def write_dint_output(self, buffer_idx, value, thread_safe=True):
        """
        Safely write a dint output with optional mutex handling
        Args:
            buffer_idx: Buffer index
            value: Dint value to write (0-4294967295)
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Validate value range
            if not (0 <= value <= 4294967295):
                return False, f"Invalid dint value: {value} (must be 0-4294967295)"
            
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return False, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return False, f"Invalid buffer index: {buffer_idx}"
                
                # Set the value
                self.args.dint_output[buffer_idx].contents.value = value
                return True, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_buffer_exception(e, "buffer access")
    
    def read_dint_output(self, buffer_idx, thread_safe=True):
        """
        Safely read a dint output with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.dint_output[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    # Lint buffer access functions (IEC_ULINT - 64-bit)
    def read_lint_input(self, buffer_idx, thread_safe=True):
        """
        Safely read a lint input with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.lint_input[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    def write_lint_output(self, buffer_idx, value, thread_safe=True):
        """
        Safely write a lint output with optional mutex handling
        Args:
            buffer_idx: Buffer index
            value: Lint value to write (0-18446744073709551615)
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Validate value range
            if not (0 <= value <= 18446744073709551615):
                return False, f"Invalid lint value: {value} (must be 0-18446744073709551615)"
            
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return False, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return False, f"Invalid buffer index: {buffer_idx}"
                
                # Set the value
                self.args.lint_output[buffer_idx].contents.value = value
                return True, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_buffer_exception(e, "buffer access")
    
    def read_lint_output(self, buffer_idx, thread_safe=True):
        """
        Safely read a lint output with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.lint_output[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    # Memory buffer access functions (IEC_UINT - 16-bit)
    def read_int_memory(self, buffer_idx, thread_safe=True):
        """
        Safely read an int memory with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.int_memory[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    def write_int_memory(self, buffer_idx, value, thread_safe=True):
        """
        Safely write an int memory with optional mutex handling
        Args:
            buffer_idx: Buffer index
            value: Int value to write (0-65535)
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Validate value range
            if not (0 <= value <= 65535):
                return False, f"Invalid int value: {value} (must be 0-65535)"
            
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return False, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return False, f"Invalid buffer index: {buffer_idx}"
                
                # Set the value
                self.args.int_memory[buffer_idx].contents.value = value
                return True, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_buffer_exception(e, "buffer access")
    
    # Memory buffer access functions (IEC_UDINT - 32-bit)
    def read_dint_memory(self, buffer_idx, thread_safe=True):
        """
        Safely read a dint memory with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.dint_memory[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    def write_dint_memory(self, buffer_idx, value, thread_safe=True):
        """
        Safely write a dint memory with optional mutex handling
        Args:
            buffer_idx: Buffer index
            value: Dint value to write (0-4294967295)
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Validate value range
            if not (0 <= value <= 4294967295):
                return False, f"Invalid dint value: {value} (must be 0-4294967295)"
            
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return False, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return False, f"Invalid buffer index: {buffer_idx}"
                
                # Set the value
                self.args.dint_memory[buffer_idx].contents.value = value
                return True, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_buffer_exception(e, "buffer access")
    
    # Memory buffer access functions (IEC_ULINT - 64-bit)
    def read_lint_memory(self, buffer_idx, thread_safe=True):
        """
        Safely read a lint memory with optional mutex handling
        Args:
            buffer_idx: Buffer index
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (int, str) - (value, error_message)
        """
        if not self.is_valid:
            return 0, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return 0, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return 0, f"Invalid buffer index: {buffer_idx}"
                
                # Access the value
                value = self.args.lint_memory[buffer_idx].contents.value
                return value, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return 0, self._handle_buffer_exception(e, "buffer access")
    
    def write_lint_memory(self, buffer_idx, value, thread_safe=True):
        """
        Safely write a lint memory with optional mutex handling
        Args:
            buffer_idx: Buffer index
            value: Lint value to write (0-18446744073709551615)
            thread_safe: If True, uses mutex for thread-safe access
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            # Validate value range
            if not (0 <= value <= 18446744073709551615):
                return False, f"Invalid lint value: {value} (must be 0-18446744073709551615)"
            
            # Take mutex only if thread_safe is True
            mutex_acquired = False
            if thread_safe:
                if self.args.mutex_take(self.args.buffer_mutex) != 0:
                    return False, "Failed to acquire mutex"
                mutex_acquired = True
            
            try:
                # Validate index
                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                    return False, f"Invalid buffer index: {buffer_idx}"
                
                # Set the value
                self.args.lint_memory[buffer_idx].contents.value = value
                return True, "Success"
                
            finally:
                # Release mutex only if it was acquired
                if mutex_acquired:
                    self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_buffer_exception(e, "buffer access")
    
    # Mutex API functions for manual control
    def acquire_mutex(self):
        """
        Manually acquire the buffer mutex
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            if self.args.mutex_take(self.args.buffer_mutex) != 0:
                return False, "Failed to acquire mutex"
            return True, "Mutex acquired successfully"
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, f"Exception during mutex acquisition: {e}"
    
    def release_mutex(self):
        """
        Manually release the buffer mutex
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid runtime args: {self.error_msg}"
        
        try:
            if self.args.mutex_give(self.args.buffer_mutex) != 0:
                return False, "Failed to release mutex"
            return True, "Mutex released successfully"
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, f"Exception during mutex release: {e}"
    
    # Batch operations for optimized mutex usage
    def batch_read_values(self, operations):
        """
        Perform multiple read operations with a single mutex acquisition
        Args:
            operations: List of tuples describing read operations
                       Format: [('buffer_type', buffer_idx, bit_idx), ...]
                       buffer_type can be: 'bool_input', 'bool_output', 'byte_input', 'byte_output',
                                         'int_input', 'int_output', 'dint_input', 'dint_output',
                                         'lint_input', 'lint_output', 'int_memory', 'dint_memory', 'lint_memory'
                       bit_idx is only required for bool operations
        Returns: (list, str) - (results, error_message)
                results format: [(success, value, error_msg), ...]
        """
        if not self.is_valid:
            return [], f"Invalid runtime args: {self.error_msg}"
        
        if not operations:
            return [], "No operations provided"
        
        results = []
        
        try:
            # Acquire mutex once for all operations
            if self.args.mutex_take(self.args.buffer_mutex) != 0:
                return [], "Failed to acquire mutex"
            
            try:
                for operation in operations:
                    try:
                        if len(operation) < 2:
                            results.append((False, None, "Invalid operation format"))
                            continue
                        
                        buffer_type = operation[0]
                        buffer_idx = operation[1]
                        
                        # Handle boolean operations (require bit_idx)
                        if buffer_type in ['bool_input', 'bool_output']:
                            if len(operation) < 3:
                                results.append((False, None, "Boolean operations require bit_idx"))
                                continue
                            bit_idx = operation[2]
                            
                            # Validate indices
                            if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                                results.append((False, None, f"Invalid buffer index: {buffer_idx}"))
                                continue
                            if bit_idx < 0 or bit_idx >= self.args.bits_per_buffer:
                                results.append((False, None, f"Invalid bit index: {bit_idx}"))
                                continue
                            
                            if buffer_type == 'bool_input':
                                value = bool(self.args.bool_input[buffer_idx][bit_idx].contents.value)
                            else:  # bool_output
                                value = bool(self.args.bool_output[buffer_idx][bit_idx].contents.value)
                            
                            results.append((True, value, "Success"))
                        
                        # Handle other buffer types
                        else:
                            # Validate buffer index
                            if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                                results.append((False, None, f"Invalid buffer index: {buffer_idx}"))
                                continue
                            
                            if buffer_type == 'byte_input':
                                value = self.args.byte_input[buffer_idx].contents.value
                            elif buffer_type == 'byte_output':
                                value = self.args.byte_output[buffer_idx].contents.value
                            elif buffer_type == 'int_input':
                                value = self.args.int_input[buffer_idx].contents.value
                            elif buffer_type == 'int_output':
                                value = self.args.int_output[buffer_idx].contents.value
                            elif buffer_type == 'dint_input':
                                value = self.args.dint_input[buffer_idx].contents.value
                            elif buffer_type == 'dint_output':
                                value = self.args.dint_output[buffer_idx].contents.value
                            elif buffer_type == 'lint_input':
                                value = self.args.lint_input[buffer_idx].contents.value
                            elif buffer_type == 'lint_output':
                                value = self.args.lint_output[buffer_idx].contents.value
                            elif buffer_type == 'int_memory':
                                value = self.args.int_memory[buffer_idx].contents.value
                            elif buffer_type == 'dint_memory':
                                value = self.args.dint_memory[buffer_idx].contents.value
                            elif buffer_type == 'lint_memory':
                                value = self.args.lint_memory[buffer_idx].contents.value
                            else:
                                results.append((False, None, f"Unknown buffer type: {buffer_type}"))
                                continue
                            
                            results.append((True, value, "Success"))
                    
                    except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
                        results.append((False, None, f"Exception during operation: {e}"))
                
                return results, "Batch read completed"
                
            finally:
                # Always release mutex
                self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return [], f"Exception during batch read: {e}"
    
    def batch_write_values(self, operations):
        """
        Perform multiple write operations with a single mutex acquisition
        Args:
            operations: List of tuples describing write operations
                       Format: [('buffer_type', buffer_idx, value, bit_idx), ...]
                       buffer_type can be: 'bool_output', 'byte_output', 'int_output', 'dint_output',
                                         'lint_output', 'int_memory', 'dint_memory', 'lint_memory'
                       bit_idx is only required for bool operations (last parameter)
        Returns: (list, str) - (results, error_message)
                results format: [(success, error_msg), ...]
        """
        if not self.is_valid:
            return [], f"Invalid runtime args: {self.error_msg}"
        
        if not operations:
            return [], "No operations provided"
        
        results = []
        
        try:
            # Acquire mutex once for all operations
            if self.args.mutex_take(self.args.buffer_mutex) != 0:
                return [], "Failed to acquire mutex"
            
            try:
                for operation in operations:
                    try:
                        if len(operation) < 3:
                            results.append((False, "Invalid operation format"))
                            continue
                        
                        buffer_type = operation[0]
                        buffer_idx = operation[1]
                        value = operation[2]
                        
                        # Handle boolean operations (require bit_idx)
                        if buffer_type == 'bool_output':
                            if len(operation) < 4:
                                results.append((False, "Boolean operations require bit_idx"))
                                continue
                            bit_idx = operation[3]
                            
                            # Validate indices
                            if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                                results.append((False, f"Invalid buffer index: {buffer_idx}"))
                                continue
                            if bit_idx < 0 or bit_idx >= self.args.bits_per_buffer:
                                results.append((False, f"Invalid bit index: {bit_idx}"))
                                continue
                            
                            self.args.bool_output[buffer_idx][bit_idx].contents.value = 1 if value else 0
                            results.append((True, "Success"))
                        
                        # Handle other buffer types
                        else:
                            # Validate buffer index
                            if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                                results.append((False, f"Invalid buffer index: {buffer_idx}"))
                                continue
                            
                            # Validate value ranges and write
                            if buffer_type == 'byte_output':
                                if not (0 <= value <= 255):
                                    results.append((False, f"Invalid byte value: {value} (must be 0-255)"))
                                    continue
                                self.args.byte_output[buffer_idx].contents.value = value
                            elif buffer_type == 'int_output':
                                if not (0 <= value <= 65535):
                                    results.append((False, f"Invalid int value: {value} (must be 0-65535)"))
                                    continue
                                self.args.int_output[buffer_idx].contents.value = value
                            elif buffer_type == 'dint_output':
                                if not (0 <= value <= 4294967295):
                                    results.append((False, f"Invalid dint value: {value} (must be 0-4294967295)"))
                                    continue
                                self.args.dint_output[buffer_idx].contents.value = value
                            elif buffer_type == 'lint_output':
                                if not (0 <= value <= 18446744073709551615):
                                    results.append((False, f"Invalid lint value: {value} (must be 0-18446744073709551615)"))
                                    continue
                                self.args.lint_output[buffer_idx].contents.value = value
                            elif buffer_type == 'int_memory':
                                if not (0 <= value <= 65535):
                                    results.append((False, f"Invalid int value: {value} (must be 0-65535)"))
                                    continue
                                self.args.int_memory[buffer_idx].contents.value = value
                            elif buffer_type == 'dint_memory':
                                if not (0 <= value <= 4294967295):
                                    results.append((False, f"Invalid dint value: {value} (must be 0-4294967295)"))
                                    continue
                                self.args.dint_memory[buffer_idx].contents.value = value
                            elif buffer_type == 'lint_memory':
                                if not (0 <= value <= 18446744073709551615):
                                    results.append((False, f"Invalid lint value: {value} (must be 0-18446744073709551615)"))
                                    continue
                                self.args.lint_memory[buffer_idx].contents.value = value
                            else:
                                results.append((False, f"Unknown buffer type: {buffer_type}"))
                                continue
                            
                            results.append((True, "Success"))
                    
                    except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
                        results.append((False, f"Exception during operation: {e}"))
                
                return results, "Batch write completed"
                
            finally:
                # Always release mutex
                self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return [], f"Exception during batch write: {e}"
    
    def batch_mixed_operations(self, read_operations, write_operations):
        """
        Perform mixed read and write operations with a single mutex acquisition
        Args:
            read_operations: List of read operation tuples (same format as batch_read_values)
            write_operations: List of write operation tuples (same format as batch_write_values)
        Returns: (dict, str) - (results, error_message)
                results format: {'reads': [(success, value, error_msg), ...], 'writes': [(success, error_msg), ...]}
        """
        if not self.is_valid:
            return {}, f"Invalid runtime args: {self.error_msg}"
        
        if not read_operations and not write_operations:
            return {}, "No operations provided"
        
        read_results = []
        write_results = []
        
        try:
            # Acquire mutex once for all operations
            if self.args.mutex_take(self.args.buffer_mutex) != 0:
                return {}, "Failed to acquire mutex"
            
            try:
                # Perform read operations first (typically safer)
                if read_operations:
                    for operation in read_operations:
                        try:
                            if len(operation) < 2:
                                read_results.append((False, None, "Invalid operation format"))
                                continue
                            
                            buffer_type = operation[0]
                            buffer_idx = operation[1]
                            
                            # Handle boolean operations (require bit_idx)
                            if buffer_type in ['bool_input', 'bool_output']:
                                if len(operation) < 3:
                                    read_results.append((False, None, "Boolean operations require bit_idx"))
                                    continue
                                bit_idx = operation[2]
                                
                                # Validate indices
                                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                                    read_results.append((False, None, f"Invalid buffer index: {buffer_idx}"))
                                    continue
                                if bit_idx < 0 or bit_idx >= self.args.bits_per_buffer:
                                    read_results.append((False, None, f"Invalid bit index: {bit_idx}"))
                                    continue
                                
                                if buffer_type == 'bool_input':
                                    value = bool(self.args.bool_input[buffer_idx][bit_idx].contents.value)
                                else:  # bool_output
                                    value = bool(self.args.bool_output[buffer_idx][bit_idx].contents.value)
                                
                                read_results.append((True, value, "Success"))
                            
                            # Handle other buffer types
                            else:
                                # Validate buffer index
                                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                                    read_results.append((False, None, f"Invalid buffer index: {buffer_idx}"))
                                    continue
                                
                                if buffer_type == 'byte_input':
                                    value = self.args.byte_input[buffer_idx].contents.value
                                elif buffer_type == 'byte_output':
                                    value = self.args.byte_output[buffer_idx].contents.value
                                elif buffer_type == 'int_input':
                                    value = self.args.int_input[buffer_idx].contents.value
                                elif buffer_type == 'int_output':
                                    value = self.args.int_output[buffer_idx].contents.value
                                elif buffer_type == 'dint_input':
                                    value = self.args.dint_input[buffer_idx].contents.value
                                elif buffer_type == 'dint_output':
                                    value = self.args.dint_output[buffer_idx].contents.value
                                elif buffer_type == 'lint_input':
                                    value = self.args.lint_input[buffer_idx].contents.value
                                elif buffer_type == 'lint_output':
                                    value = self.args.lint_output[buffer_idx].contents.value
                                elif buffer_type == 'int_memory':
                                    value = self.args.int_memory[buffer_idx].contents.value
                                elif buffer_type == 'dint_memory':
                                    value = self.args.dint_memory[buffer_idx].contents.value
                                elif buffer_type == 'lint_memory':
                                    value = self.args.lint_memory[buffer_idx].contents.value
                                else:
                                    read_results.append((False, None, f"Unknown buffer type: {buffer_type}"))
                                    continue
                                
                                read_results.append((True, value, "Success"))
                        
                        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
                            read_results.append((False, None, f"Exception during read operation: {e}"))
                
                # Perform write operations
                if write_operations:
                    for operation in write_operations:
                        try:
                            if len(operation) < 3:
                                write_results.append((False, "Invalid operation format"))
                                continue
                            
                            buffer_type = operation[0]
                            buffer_idx = operation[1]
                            value = operation[2]
                            
                            # Handle boolean operations (require bit_idx)
                            if buffer_type == 'bool_output':
                                if len(operation) < 4:
                                    write_results.append((False, "Boolean operations require bit_idx"))
                                    continue
                                bit_idx = operation[3]
                                
                                # Validate indices
                                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                                    write_results.append((False, f"Invalid buffer index: {buffer_idx}"))
                                    continue
                                if bit_idx < 0 or bit_idx >= self.args.bits_per_buffer:
                                    write_results.append((False, f"Invalid bit index: {bit_idx}"))
                                    continue
                                
                                self.args.bool_output[buffer_idx][bit_idx].contents.value = 1 if value else 0
                                write_results.append((True, "Success"))
                            
                            # Handle other buffer types
                            else:
                                # Validate buffer index
                                if buffer_idx < 0 or buffer_idx >= self.args.buffer_size:
                                    write_results.append((False, f"Invalid buffer index: {buffer_idx}"))
                                    continue
                                
                                # Validate value ranges and write
                                if buffer_type == 'byte_output':
                                    if not (0 <= value <= 255):
                                        write_results.append((False, f"Invalid byte value: {value} (must be 0-255)"))
                                        continue
                                    self.args.byte_output[buffer_idx].contents.value = value
                                elif buffer_type == 'int_output':
                                    if not (0 <= value <= 65535):
                                        write_results.append((False, f"Invalid int value: {value} (must be 0-65535)"))
                                        continue
                                    self.args.int_output[buffer_idx].contents.value = value
                                elif buffer_type == 'dint_output':
                                    if not (0 <= value <= 4294967295):
                                        write_results.append((False, f"Invalid dint value: {value} (must be 0-4294967295)"))
                                        continue
                                    self.args.dint_output[buffer_idx].contents.value = value
                                elif buffer_type == 'lint_output':
                                    if not (0 <= value <= 18446744073709551615):
                                        write_results.append((False, f"Invalid lint value: {value} (must be 0-18446744073709551615)"))
                                        continue
                                    self.args.lint_output[buffer_idx].contents.value = value
                                elif buffer_type == 'int_memory':
                                    if not (0 <= value <= 65535):
                                        write_results.append((False, f"Invalid int value: {value} (must be 0-65535)"))
                                        continue
                                    self.args.int_memory[buffer_idx].contents.value = value
                                elif buffer_type == 'dint_memory':
                                    if not (0 <= value <= 4294967295):
                                        write_results.append((False, f"Invalid dint value: {value} (must be 0-4294967295)"))
                                        continue
                                    self.args.dint_memory[buffer_idx].contents.value = value
                                elif buffer_type == 'lint_memory':
                                    if not (0 <= value <= 18446744073709551615):
                                        write_results.append((False, f"Invalid lint value: {value} (must be 0-18446744073709551615)"))
                                        continue
                                    self.args.lint_memory[buffer_idx].contents.value = value
                                else:
                                    write_results.append((False, f"Unknown buffer type: {buffer_type}"))
                                    continue
                                
                                write_results.append((True, "Success"))
                        
                        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
                            write_results.append((False, f"Exception during write operation: {e}"))
                
                return {'reads': read_results, 'writes': write_results}, "Batch mixed operations completed"
                
            finally:
                # Always release mutex
                self.args.mutex_give(self.args.buffer_mutex)
                
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return {}, f"Exception during batch mixed operations: {e}"
    
    def get_config_path(self):
        """
        Retrieve the plugin-specific configuration file path
        Returns: (str, str) - (config_path, error_message)
        """
        if not self.is_valid:
            return "", f"Invalid runtime args: {self.error_msg}"
        
        try:
            config_path_bytes = self.args.plugin_specific_config_file_path
            
            # Handle different types of C char arrays
            if isinstance(config_path_bytes, (bytes, bytearray)):
                config_path = config_path_bytes.decode('utf-8').rstrip('\x00')
            elif hasattr(config_path_bytes, 'value'):
                config_path = config_path_bytes.value.decode('utf-8').rstrip('\x00')
            elif hasattr(config_path_bytes, 'raw'):
                config_path = config_path_bytes.raw.decode('utf-8').rstrip('\x00')
            else:
                # Try to convert to bytes first
                config_path = bytes(config_path_bytes).decode('utf-8').rstrip('\x00')
            
            # Clean up the path - remove all whitespace and control characters
            config_path = config_path.strip()
            
            return config_path, "Success"
        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return "", f"Exception retrieving config path: {e}"
        
    def get_config_file_args_as_map(self):
        """
        Parse the plugin-specific configuration file as a key-value map
        Supports JSON format for flexibility
        Returns: (dict, str) - (config_map, error_message)
        """
        import os
        
        config_path, err_msg = self.get_config_path()
        if not config_path:
            return {}, f"Failed to get config path: {err_msg}"
        
        # Debug information
        debug_info = f"Original path: '{config_path}', CWD: '{os.getcwd()}'"
        
        try:
            with open(config_path, 'r') as config_file:
                config_data = json.load(config_file)
                if not isinstance(config_data, dict):
                    return {}, "Configuration file must contain a JSON object at the top level"
                return config_data, "Success"
        except FileNotFoundError:
            return {}, f"Configuration file not found: {config_path}"
        except json.JSONDecodeError as e:
            return {}, f"JSON parsing error in config file {config_path}: {e}"
        except (OSError, MemoryError) as e:
            return {}, f"Exception reading config file {config_path}: {e}"
    

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

if __name__ == "__main__":
    # Self-test when run directly
    print("OpenPLC Python Plugin Types - Self Test")
    print("=" * 50)
    
    # Test structure validation
    # PluginStructureValidator.print_structure_info()
    
    print(f"\nIEC Type Sizes:")
    print(f"  IEC_BOOL: {ctypes.sizeof(IEC_BOOL)} bytes")
    print(f"  IEC_BYTE: {ctypes.sizeof(IEC_BYTE)} bytes") 
    print(f"  IEC_UINT: {ctypes.sizeof(IEC_UINT)} bytes")
    print(f"  IEC_UDINT: {ctypes.sizeof(IEC_UDINT)} bytes")
    print(f"  IEC_ULINT: {ctypes.sizeof(IEC_ULINT)} bytes")
