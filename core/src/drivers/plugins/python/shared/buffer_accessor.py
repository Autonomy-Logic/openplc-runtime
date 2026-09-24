# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# pylint: disable=R0913,R0917
# R0913: Too many arguments (required for generic buffer operations)
# R0917: Too many positional arguments (required for generic buffer operations)
"""
Generic Buffer Accessor for OpenPLC Python Plugin System

This module provides generic buffer access operations that work with any buffer type.
It encapsulates the low-level ctypes operations and provides a clean interface
for reading and writing buffer values.
"""

import ctypes
from typing import Any, Optional, Tuple

try:
    # Try relative imports first (when used as package)
    from .buffer_types import get_buffer_types
    from .buffer_validator import BufferValidator
    from .component_interfaces import IBufferAccessor
    from .mutex_manager import MutexManager
except ImportError:
    # Fall back to absolute imports (when testing standalone)
    from buffer_types import get_buffer_types
    from buffer_validator import BufferValidator
    from component_interfaces import IBufferAccessor
    from mutex_manager import MutexManager


class GenericBufferAccessor(IBufferAccessor):
    """
    Generic buffer accessor that handles all buffer types uniformly.

    This class encapsulates the complex ctypes buffer access logic and provides
    a clean, type-agnostic interface for buffer operations. It eliminates the
    massive code duplication that existed in the original SafeBufferAccess class.

    Write operations use the journal buffer system for race-condition-free writes.
    Read operations access buffers directly (reads are always safe).
    """

    # Journal buffer type mapping (matches journal_buffer_type_t enum)
    JOURNAL_TYPE_MAP = {
        "bool_input": 0,   # JOURNAL_BOOL_INPUT
        "bool_output": 1,  # JOURNAL_BOOL_OUTPUT
        "bool_memory": 2,  # JOURNAL_BOOL_MEMORY
        "byte_input": 3,   # JOURNAL_BYTE_INPUT
        "byte_output": 4,  # JOURNAL_BYTE_OUTPUT
        "int_input": 5,    # JOURNAL_INT_INPUT
        "int_output": 6,   # JOURNAL_INT_OUTPUT
        "int_memory": 7,   # JOURNAL_INT_MEMORY
        "dint_input": 8,   # JOURNAL_DINT_INPUT
        "dint_output": 9,  # JOURNAL_DINT_OUTPUT
        "dint_memory": 10, # JOURNAL_DINT_MEMORY
        "lint_input": 11,  # JOURNAL_LINT_INPUT
        "lint_output": 12, # JOURNAL_LINT_OUTPUT
        "lint_memory": 13, # JOURNAL_LINT_MEMORY
    }

    def __init__(self, runtime_args, validator: BufferValidator, mutex_manager: MutexManager):
        """
        Initialize the generic buffer accessor.

        Args:
            runtime_args: PluginRuntimeArgs instance
            validator: BufferValidator instance
            mutex_manager: MutexManager instance
        """
        self.args = runtime_args
        self.validator = validator
        self.mutex = mutex_manager
        self.buffer_types = get_buffer_types()

    def read_buffer(
        self,
        buffer_type: str,
        buffer_idx: int,
        bit_idx: Optional[int] = None,
        thread_safe: bool = True,
    ) -> Tuple[Any, str]:
        """
        Generic buffer read operation.

        Args:
            buffer_type: Buffer type name (e.g., 'bool_input', 'int_output')
            buffer_idx: Buffer index
            bit_idx: Bit index (required for boolean operations)
            thread_safe: Whether to use mutex protection

        Returns:
            Tuple[Any, str]: (value, error_message)
        """
        # Validate parameters
        is_valid, msg = self.validator.validate_operation_params(buffer_type, buffer_idx, bit_idx)
        if not is_valid:
            return None, msg

        # Get buffer type info
        buffer_type_obj, direction = self.buffer_types.get_buffer_info(buffer_type)

        # Define the read operation
        def do_read():
            return self._perform_read(buffer_type, buffer_type_obj, direction, buffer_idx, bit_idx)

        # Execute with or without mutex
        if thread_safe:
            return self.mutex.with_mutex(do_read)
        else:
            return do_read()

    def write_buffer(
        self,
        buffer_type: str,
        buffer_idx: int,
        value: Any,
        bit_idx: Optional[int] = None,
        thread_safe: bool = True,
    ) -> Tuple[bool, str]:
        """
        Generic buffer write operation.

        Writes go through the journal buffer system for race-condition-free operation.
        The journal is internally thread-safe, so the thread_safe parameter is kept
        for backward compatibility but writes no longer require explicit mutex.

        Args:
            buffer_type: Buffer type name (e.g., 'bool_output', 'int_output')
            buffer_idx: Buffer index
            value: Value to write
            bit_idx: Bit index (required for boolean operations)
            thread_safe: Kept for backward compatibility (journal is always thread-safe)

        Returns:
            Tuple[bool, str]: (success, error_message)
        """
        # Validate parameters
        is_valid, msg = self.validator.validate_operation_params(
            buffer_type, buffer_idx, bit_idx, value
        )
        if not is_valid:
            return False, msg

        # Get buffer type info
        buffer_type_obj, direction = self.buffer_types.get_buffer_info(buffer_type)

        # Journal writes are thread-safe internally, no mutex needed
        # The thread_safe parameter is ignored but kept for backward compatibility
        return self._perform_write(
            buffer_type, buffer_type_obj, direction, buffer_idx, value, bit_idx
        )

    def get_buffer_pointer(self, buffer_type: str) -> Optional[ctypes.POINTER]:
        """
        Get the buffer pointer for a given type.

        Args:
            buffer_type: Buffer type name

        Returns:
            Optional[ctypes.POINTER]: Buffer pointer or None if invalid
        """
        try:
            buffer_type_obj, direction = self.buffer_types.get_buffer_info(buffer_type)

            # Map buffer type to runtime_args field
            field_map = {
                ("bool", "input"): "bool_input",
                ("bool", "output"): "bool_output",
                ("bool", "memory"): "bool_memory",
                ("byte", "input"): "byte_input",
                ("byte", "output"): "byte_output",
                ("int", "input"): "int_input",
                ("int", "output"): "int_output",
                ("int", "memory"): "int_memory",
                ("dint", "input"): "dint_input",
                ("dint", "output"): "dint_output",
                ("dint", "memory"): "dint_memory",
                ("lint", "input"): "lint_input",
                ("lint", "output"): "lint_output",
                ("lint", "memory"): "lint_memory",
            }

            field_name = field_map.get((buffer_type_obj.name, direction))
            if field_name:
                return getattr(self.args, field_name, None)

            return None

        except (AttributeError, TypeError, ValueError):
            return None

    def _perform_read(
        self,
        buffer_type: str,
        buffer_type_obj,
        direction: str,
        buffer_idx: int,
        bit_idx: Optional[int],
    ) -> Tuple[Any, str]:
        """
        Internal method to perform the actual buffer read operation.
        """
        try:
            # Get the appropriate buffer pointer
            buffer_ptr = self.get_buffer_pointer(buffer_type)
            if buffer_ptr is None or buffer_ptr.contents is None:
                return None, f"Buffer pointer not available for {buffer_type}"

            # Handle boolean operations (require bit indexing)
            if buffer_type_obj.name == "bool":
                if bit_idx is None:
                    return None, "Bit index required for boolean operations"

                # Access the specific bit within the buffer
                value = bool(buffer_ptr[buffer_idx][bit_idx].contents.value)
                return value, "Success"

            # Handle other buffer types (direct value access)
            else:
                value = buffer_ptr[buffer_idx].contents.value
                return value, "Success"

        except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
            return None, f"Buffer read error: {e}"

    def _perform_write(
        self,
        buffer_type: str,
        buffer_type_obj,
        direction: str,
        buffer_idx: int,
        value: Any,
        bit_idx: Optional[int],
    ) -> Tuple[bool, str]:
        """
        Internal method to perform the actual buffer write operation.

        Uses the journal buffer system for race-condition-free writes.
        All writes go through the journal and are applied atomically at the
        start of the next PLC scan cycle.
        """
        try:
            # Get journal buffer type
            journal_type = self.JOURNAL_TYPE_MAP.get(buffer_type)
            if journal_type is None:
                return False, f"Unknown buffer type: {buffer_type}"

            # Handle boolean operations (require bit indexing)
            if buffer_type_obj.name == "bool":
                if bit_idx is None:
                    return False, "Bit index required for boolean operations"

                # Write through journal
                result = self.args.journal_write_bool(
                    journal_type, buffer_idx, bit_idx, 1 if value else 0
                )
                if result != 0:
                    return False, f"Journal write failed with code {result}"
                return True, "Success"

            # Handle byte operations
            elif buffer_type_obj.name == "byte":
                result = self.args.journal_write_byte(journal_type, buffer_idx, int(value) & 0xFF)
                if result != 0:
                    return False, f"Journal write failed with code {result}"
                return True, "Success"

            # Handle int operations (16-bit)
            elif buffer_type_obj.name == "int":
                result = self.args.journal_write_int(journal_type, buffer_idx, int(value) & 0xFFFF)
                if result != 0:
                    return False, f"Journal write failed with code {result}"
                return True, "Success"

            # Handle dint operations (32-bit)
            elif buffer_type_obj.name == "dint":
                result = self.args.journal_write_dint(journal_type, buffer_idx, int(value) & 0xFFFFFFFF)
                if result != 0:
                    return False, f"Journal write failed with code {result}"
                return True, "Success"

            # Handle lint operations (64-bit)
            elif buffer_type_obj.name == "lint":
                result = self.args.journal_write_lint(journal_type, buffer_idx, int(value))
                if result != 0:
                    return False, f"Journal write failed with code {result}"
                return True, "Success"

            else:
                return False, f"Unsupported buffer type: {buffer_type_obj.name}"

        except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
            return False, f"Buffer write error: {e}"

    def _handle_buffer_exception(self, exception, operation_name: str) -> str:
        """
        Centralized exception handling for buffer operations.

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
