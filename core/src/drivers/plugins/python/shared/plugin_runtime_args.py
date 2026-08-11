#!/usr/bin/env python3
"""
Plugin Runtime Arguments Structure

This module provides the PluginRuntimeArgs ctypes structure that matches
the plugin_runtime_args_t structure from plugin_driver.h.
"""

import ctypes

# Import IEC type definitions
from .iec_types import IEC_BOOL, IEC_BYTE, IEC_UDINT, IEC_UINT, IEC_ULINT


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
        ("bool_memory", ctypes.POINTER(ctypes.POINTER(IEC_BOOL) * 8)),
        # Flush-on-lock image read API. image_lock() takes the image mutex and
        # drains the journal so the holder reads freshly committed values;
        # image_unlock() releases it. Writes use journal_write_* (no lock).
        # Both are void (*)(void).
        ("image_lock", ctypes.CFUNCTYPE(None)),
        ("image_unlock", ctypes.CFUNCTYPE(None)),
        # STruC++ debugger variable-access surface. Replaces the
        # MatIEC-era flat-index API (get_var_list/get_var_size/
        # get_var_count). Variables are addressed by (arr, elem); the
        # editor resolves user-selected variables against debug-map.json
        # and writes the tuples into each plugin's per-plugin config.
        # debug_set toggles forcing; debug_write does a soft write that
        # respects existing forces (the next scan cycle can overwrite).
        ("debug_array_count", ctypes.CFUNCTYPE(ctypes.c_uint8)),
        ("debug_elem_count",  ctypes.CFUNCTYPE(ctypes.c_uint16, ctypes.c_uint8)),
        ("debug_size",        ctypes.CFUNCTYPE(ctypes.c_uint16, ctypes.c_uint8, ctypes.c_uint16)),
        ("debug_read",        ctypes.CFUNCTYPE(ctypes.c_uint16,
                                               ctypes.c_uint8, ctypes.c_uint16,
                                               ctypes.POINTER(ctypes.c_uint8))),
        ("debug_set",         ctypes.CFUNCTYPE(ctypes.c_uint8,
                                               ctypes.c_uint8, ctypes.c_uint16,
                                               ctypes.c_bool,
                                               ctypes.POINTER(ctypes.c_uint8),
                                               ctypes.c_uint16)),
        ("debug_write",       ctypes.CFUNCTYPE(ctypes.c_uint8,
                                               ctypes.c_uint8, ctypes.c_uint16,
                                               ctypes.POINTER(ctypes.c_uint8),
                                               ctypes.c_uint16)),
        ("plugin_specific_config_file_path", ctypes.c_char * 256),
        # Buffer size information
        ("buffer_size", ctypes.c_int),
        ("bits_per_buffer", ctypes.c_int),
        # Logging function pointers
        ("log_info", ctypes.CFUNCTYPE(None, ctypes.c_char_p)),
        ("log_debug", ctypes.CFUNCTYPE(None, ctypes.c_char_p)),
        ("log_warn", ctypes.CFUNCTYPE(None, ctypes.c_char_p)),
        ("log_error", ctypes.CFUNCTYPE(None, ctypes.c_char_p)),
        # Journal write function pointers for race-condition-free buffer writes
        # int (*func)(int type, int index, int bit/value, int value)
        ("journal_write_bool", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)),
        ("journal_write_byte", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)),
        ("journal_write_int", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)),
        ("journal_write_dint", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint)),
        ("journal_write_lint", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulonglong)),
        # Async request to stop the whole PLC: void (*)(const char *reason).
        #
        # This entry was missing while the C struct had the field, so every
        # field after it was shifted by one pointer -- `base_tick_ns` was
        # actually reading the request_plc_stop pointer. Keep this list in
        # lockstep with plugin_types.h; the offsets are load-bearing.
        ("request_plc_stop", ctypes.CFUNCTYPE(None, ctypes.c_char_p)),
        # PLC base tick time in nanoseconds (mirrors C-side base_tick_ns).
        ("base_tick_ns", ctypes.c_ulonglong),
        # --- Run/stop control (appended in C, so appended here too) ---------
        # Async request to run: void (*)(const char *reason). Refused while the
        # mode switch reads STOP.
        ("request_plc_start", ctypes.CFUNCTYPE(None, ctypes.c_char_p)),
        # Report the mode-switch position: void (*)(int position),
        # 0 = STOP, 1 = RUN. Store the position BEFORE requesting a transition.
        ("set_switch_position", ctypes.CFUNCTYPE(None, ctypes.c_int)),
        # Current PLC state: int (*)(void), 0 = STOPPED, 1 = RUNNING, 2 = ERROR.
        ("get_plc_state", ctypes.CFUNCTYPE(ctypes.c_int)),
    ]

    def validate_pointers(self):
        """
        Validate that critical pointers are not NULL
        Returns: (bool, str) - (is_valid, error_message)
        """
        try:
            # Check image lock functions
            if not self.image_lock:
                return False, "image_lock function pointer is NULL"
            if not self.image_unlock:
                return False, "image_unlock function pointer is NULL"

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

            def addr(ptr):
                return ctypes.addressof(ptr.contents) if ptr else 0

            return (
                f"PluginRuntimeArgs(\n"
                f"  bool_input=0x{addr(self.bool_input):x},\n"
                f"  bool_output=0x{addr(self.bool_output):x},\n"
                f"  byte_input=0x{addr(self.byte_input):x},\n"
                f"  byte_output=0x{addr(self.byte_output):x},\n"
                f"  int_input=0x{addr(self.int_input):x},\n"
                f"  int_output=0x{addr(self.int_output):x},\n"
                f"  dint_input=0x{addr(self.dint_input):x},\n"
                f"  dint_output=0x{addr(self.dint_output):x},\n"
                f"  lint_input=0x{addr(self.lint_input):x},\n"
                f"  lint_output=0x{addr(self.lint_output):x},\n"
                f"  int_memory=0x{addr(self.int_memory):x},\n"
                f"  buffer_size={self.buffer_size},\n"
                f"  bits_per_buffer={self.bits_per_buffer},\n"
                f"  image_lock={'valid' if self.image_lock else 'NULL'},\n"
                f"  image_unlock={'valid' if self.image_unlock else 'NULL'}\n"
                f")"
            )
        except Exception:
            return "PluginRuntimeArgs(corrupted or invalid)"
