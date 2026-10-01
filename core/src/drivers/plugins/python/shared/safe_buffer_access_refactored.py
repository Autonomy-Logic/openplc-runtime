# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Refactored SafeBufferAccess - Modular Architecture

This module provides the refactored SafeBufferAccess class that maintains
100% API compatibility while using a modular component architecture internally.
"""

# pylint: disable=too-many-instance-attributes,too-many-public-methods

from typing import Any, Dict, List, Tuple

try:
    # Try relative imports first (when used as package)
    from .batch_processor import BatchProcessor
    from .buffer_accessor import GenericBufferAccessor
    from .buffer_types import get_buffer_types
    from .buffer_validator import BufferValidator
    from .component_interfaces import ISafeBufferAccess
    from .config_handler import ConfigHandler
    from .mutex_manager import MutexManager
except ImportError:
    # Fall back to absolute imports (when testing standalone)
    from batch_processor import BatchProcessor
    from buffer_accessor import GenericBufferAccessor
    from buffer_types import get_buffer_types
    from buffer_validator import BufferValidator
    from component_interfaces import ISafeBufferAccess
    from config_handler import ConfigHandler
    from mutex_manager import MutexManager


class SafeBufferAccess(ISafeBufferAccess):
    """
    Refactored SafeBufferAccess with modular architecture.

    This class maintains 100% API compatibility with the original SafeBufferAccess
    while internally using a clean, modular component architecture. All existing
    code and tests will continue to work without modification.

    The modular architecture eliminates code duplication and improves maintainability:
    - Buffer type definitions are centralized and extensible
    - Validation logic is consolidated
    - Mutex management is abstracted
    - Buffer access is generic and type-agnostic
    - Batch operations are optimized
    - Debug utilities are separated
    - Configuration handling is isolated
    """

    def __init__(self, runtime_args):
        """
        Initialize SafeBufferAccess with modular components.

        Args:
            runtime_args: PluginRuntimeArgs instance
        """
        # Expose so plugins (e.g. OPC-UA SyncManager) can reach the
        # debug_read / debug_write / debug_size function pointers
        # without going through the buffer-access wrapper.
        self.runtime_args = runtime_args
        # Initialize all components
        self.buffer_types = get_buffer_types()
        self.mutex_manager = MutexManager(runtime_args)
        self.validator = BufferValidator(runtime_args)
        self.buffer_accessor = GenericBufferAccessor(
            runtime_args, self.validator, self.mutex_manager
        )
        self.batch_processor = BatchProcessor(self.buffer_accessor, self.mutex_manager)
        self.config_handler = ConfigHandler(runtime_args)

        # Validate initialization (maintains original behavior)
        self._is_valid, self._error_msg = runtime_args.validate_pointers()

    @property
    def is_valid(self) -> bool:
        """Whether the instance is properly initialized."""
        return self._is_valid

    @property
    def error_msg(self) -> str:
        """Error message if initialization failed."""
        return self._error_msg

    # ============================================================================
    # Mutex Management Methods
    # ============================================================================

    def acquire_mutex(self) -> Tuple[bool, str]:
        """Acquire the buffer mutex."""
        success = self.mutex_manager.acquire()
        return success, "Success" if success else "Failed to acquire mutex"

    def release_mutex(self) -> Tuple[bool, str]:
        """Release the buffer mutex."""
        success = self.mutex_manager.release()
        return success, "Success" if success else "Failed to release mutex"

    # ============================================================================
    # Boolean Buffer Operations
    # ============================================================================

    def read_bool_input(
        self, buffer_idx: int, bit_idx: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Read a boolean input value."""
        return self.buffer_accessor.read_buffer("bool_input", buffer_idx, bit_idx, thread_safe)

    def read_bool_output(
        self, buffer_idx: int, bit_idx: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Read a boolean output value."""
        return self.buffer_accessor.read_buffer("bool_output", buffer_idx, bit_idx, thread_safe)

    def write_bool_input(
        self, buffer_idx: int, bit_idx: int, value: bool, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a boolean input value (used by Modbus master to update PLC inputs)."""
        return self.buffer_accessor.write_buffer(
            "bool_input", buffer_idx, value, bit_idx, thread_safe
        )

    def write_bool_output(
        self, buffer_idx: int, bit_idx: int, value: bool, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a boolean output value."""
        return self.buffer_accessor.write_buffer(
            "bool_output", buffer_idx, value, bit_idx, thread_safe
        )

    def read_bool_memory(
        self, buffer_idx: int, bit_idx: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Read a boolean memory value (%MX)."""
        return self.buffer_accessor.read_buffer("bool_memory", buffer_idx, bit_idx, thread_safe)

    def write_bool_memory(
        self, buffer_idx: int, bit_idx: int, value: bool, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a boolean memory value (%MX)."""
        return self.buffer_accessor.write_buffer(
            "bool_memory", buffer_idx, value, bit_idx, thread_safe
        )

    # ============================================================================
    # Byte Buffer Operations
    # ============================================================================

    def read_byte_input(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read a byte input value."""
        return self.buffer_accessor.read_buffer("byte_input", buffer_idx, None, thread_safe)

    def write_byte_input(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a byte input value (used by Modbus master to update PLC inputs)."""
        return self.buffer_accessor.write_buffer("byte_input", buffer_idx, value, None, thread_safe)

    def read_byte_output(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read a byte output value."""
        return self.buffer_accessor.read_buffer("byte_output", buffer_idx, None, thread_safe)

    def write_byte_output(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a byte output value."""
        return self.buffer_accessor.write_buffer(
            "byte_output", buffer_idx, value, None, thread_safe
        )

    # ============================================================================
    # Integer Buffer Operations (16-bit)
    # ============================================================================

    def read_int_input(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read an integer input value."""
        return self.buffer_accessor.read_buffer("int_input", buffer_idx, None, thread_safe)

    def write_int_input(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write an integer input value (used by Modbus master to update PLC inputs)."""
        return self.buffer_accessor.write_buffer("int_input", buffer_idx, value, None, thread_safe)

    def read_int_output(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read an integer output value."""
        return self.buffer_accessor.read_buffer("int_output", buffer_idx, None, thread_safe)

    def write_int_output(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write an integer output value."""
        return self.buffer_accessor.write_buffer("int_output", buffer_idx, value, None, thread_safe)

    def read_int_memory(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read an integer memory value."""
        return self.buffer_accessor.read_buffer("int_memory", buffer_idx, None, thread_safe)

    def write_int_memory(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write an integer memory value."""
        return self.buffer_accessor.write_buffer("int_memory", buffer_idx, value, None, thread_safe)

    # ============================================================================
    # Double Integer Buffer Operations (32-bit)
    # ============================================================================

    def read_dint_input(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read a double integer input value."""
        return self.buffer_accessor.read_buffer("dint_input", buffer_idx, None, thread_safe)

    def write_dint_input(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a double integer input value (used by Modbus master to update PLC inputs)."""
        return self.buffer_accessor.write_buffer("dint_input", buffer_idx, value, None, thread_safe)

    def read_dint_output(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read a double integer output value."""
        return self.buffer_accessor.read_buffer("dint_output", buffer_idx, None, thread_safe)

    def write_dint_output(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a double integer output value."""
        return self.buffer_accessor.write_buffer(
            "dint_output", buffer_idx, value, None, thread_safe
        )

    def read_dint_memory(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read a double integer memory value."""
        return self.buffer_accessor.read_buffer("dint_memory", buffer_idx, None, thread_safe)

    def write_dint_memory(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a double integer memory value."""
        return self.buffer_accessor.write_buffer(
            "dint_memory", buffer_idx, value, None, thread_safe
        )

    # ============================================================================
    # Long Integer Buffer Operations (64-bit)
    # ============================================================================

    def read_lint_input(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read a long integer input value."""
        return self.buffer_accessor.read_buffer("lint_input", buffer_idx, None, thread_safe)

    def write_lint_input(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a long integer input value (used by Modbus master to update PLC inputs)."""
        return self.buffer_accessor.write_buffer("lint_input", buffer_idx, value, None, thread_safe)

    def read_lint_output(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read a long integer output value."""
        return self.buffer_accessor.read_buffer("lint_output", buffer_idx, None, thread_safe)

    def write_lint_output(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a long integer output value."""
        return self.buffer_accessor.write_buffer(
            "lint_output", buffer_idx, value, None, thread_safe
        )

    def read_lint_memory(self, buffer_idx: int, thread_safe: bool = True) -> Tuple[int, str]:
        """Read a long integer memory value."""
        return self.buffer_accessor.read_buffer("lint_memory", buffer_idx, None, thread_safe)

    def write_lint_memory(
        self, buffer_idx: int, value: int, thread_safe: bool = True
    ) -> Tuple[bool, str]:
        """Write a long integer memory value."""
        return self.buffer_accessor.write_buffer(
            "lint_memory", buffer_idx, value, None, thread_safe
        )

    # ============================================================================
    # Batch Operations
    # ============================================================================

    def batch_read_values(self, operations: List[Tuple]) -> Tuple[List[Tuple], str]:
        """Process multiple read operations in batch."""
        return self.batch_processor.process_batch_reads(operations)

    def batch_write_values(self, operations: List[Tuple]) -> Tuple[List[Tuple], str]:
        """Process multiple write operations in batch."""
        return self.batch_processor.process_batch_writes(operations)

    def batch_mixed_operations(
        self, read_operations: List[Tuple], write_operations: List[Tuple]
    ) -> Tuple[Dict, str]:
        """Process mixed read and write operations in batch."""
        return self.batch_processor.process_mixed_operations(read_operations, write_operations)

    # ============================================================================
    # Variable access (debug_*) — moved out of SafeBufferAccess.
    #
    # The MatIEC-era flat-index API (get_var_list / get_var_size /
    # get_var_count / get_var_value / get_var_*_batch) is gone. Plugins
    # that need to read/write program variables call the runtime's
    # debug_read / debug_write / debug_set / debug_size function
    # pointers directly via runtime_args.* — see
    # opcua/opcua_memory.py for the typed Python helpers
    # (debug_read_value, debug_write_value, debug_force_value,
    # debug_unforce, initialize_variable_cache).
    # ============================================================================

    # ============================================================================
    # Configuration Operations
    # ============================================================================

    def get_config_path(self) -> Tuple[str, str]:
        """Get configuration file path."""
        return self.config_handler.get_config_path()

    def get_config_file_args_as_map(self) -> Tuple[Dict, str]:
        """Parse configuration file as map."""
        return self.config_handler.get_config_as_map()
