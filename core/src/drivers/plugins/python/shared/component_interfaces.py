# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Component Interfaces for Modular SafeBufferAccess Architecture

This module defines the abstract interfaces that each component must implement.
These interfaces ensure loose coupling and testability while maintaining API compatibility.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Tuple, Any, Optional
import ctypes


class IBufferType:
    """Interface for buffer type definitions"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Buffer type name (e.g., 'bool', 'byte', 'int')"""
        pass

    @property
    @abstractmethod
    def size_bytes(self) -> int:
        """Size in bytes of this buffer type"""
        pass

    @property
    @abstractmethod
    def value_range(self) -> Tuple[int, int]:
        """Valid value range (min, max)"""
        pass

    @property
    @abstractmethod
    def requires_bit_index(self) -> bool:
        """Whether this type requires bit index for access"""
        pass

    @property
    @abstractmethod
    def ctype_class(self) -> type:
        """Corresponding ctypes class"""
        pass


class IMutexManager:
    """Interface for mutex management operations"""

    @abstractmethod
    def acquire(self) -> bool:
        """Acquire the mutex. Returns True on success."""
        pass

    @abstractmethod
    def release(self) -> bool:
        """Release the mutex. Returns True on success."""
        pass

    @abstractmethod
    def with_mutex(self, operation: callable) -> Any:
        """Execute operation within mutex context. Returns operation result."""
        pass


class IBufferValidator:
    """Interface for buffer validation operations"""

    @abstractmethod
    def validate_buffer_index(self, buffer_idx: int, buffer_type: str) -> Tuple[bool, str]:
        """Validate buffer index. Returns (is_valid, error_message)"""
        pass

    @abstractmethod
    def validate_bit_index(self, bit_idx: int) -> Tuple[bool, str]:
        """Validate bit index for boolean operations. Returns (is_valid, error_message)"""
        pass

    @abstractmethod
    def validate_value_range(self, value: Any, buffer_type: str) -> Tuple[bool, str]:
        """Validate value is within acceptable range. Returns (is_valid, error_message)"""
        pass

    @abstractmethod
    def validate_operation_params(self, buffer_type: str, buffer_idx: int,
                                bit_idx: Optional[int] = None, value: Any = None) -> Tuple[bool, str]:
        """Comprehensive parameter validation. Returns (is_valid, error_message)"""
        pass


class IBufferAccessor:
    """Interface for generic buffer access operations"""

    @abstractmethod
    def read_buffer(self, buffer_type: str, buffer_idx: int, bit_idx: Optional[int] = None,
                   thread_safe: bool = True) -> Tuple[Any, str]:
        """Generic buffer read operation. Returns (value, error_message)"""
        pass

    @abstractmethod
    def write_buffer(self, buffer_type: str, buffer_idx: int, value: Any,
                    bit_idx: Optional[int] = None, thread_safe: bool = True) -> Tuple[bool, str]:
        """Generic buffer write operation. Returns (success, error_message)"""
        pass

    @abstractmethod
    def get_buffer_pointer(self, buffer_type: str) -> Optional[ctypes.POINTER]:
        """Get the buffer pointer for a given type. Returns None if invalid."""
        pass


class IBatchProcessor:
    """Interface for batch operations"""

    @abstractmethod
    def process_batch_reads(self, operations: List[Tuple]) -> Tuple[List[Tuple], str]:
        """Process multiple read operations. Returns (results, error_message)"""
        pass

    @abstractmethod
    def process_batch_writes(self, operations: List[Tuple]) -> Tuple[List[Tuple], str]:
        """Process multiple write operations. Returns (results, error_message)"""
        pass

    @abstractmethod
    def process_mixed_operations(self, read_operations: List[Tuple],
                               write_operations: List[Tuple]) -> Tuple[Dict, str]:
        """Process mixed read/write operations. Returns (results_dict, error_message)"""
        pass


class IConfigHandler:
    """Interface for configuration file operations"""

    @abstractmethod
    def get_config_path(self) -> Tuple[str, str]:
        """Get configuration file path. Returns (path, error_message)"""
        pass

    @abstractmethod
    def get_config_as_map(self) -> Tuple[Dict, str]:
        """Parse config file as key-value map. Returns (config_dict, error_message)"""
        pass


class ISafeBufferAccess:
    """Main interface that maintains API compatibility"""

    @property
    @abstractmethod
    def is_valid(self) -> bool:
        """Whether the instance is properly initialized"""
        pass

    @property
    @abstractmethod
    def error_msg(self) -> str:
        """Error message if initialization failed"""
        pass

    # All the public methods from the original API must be implemented
    # See API_SPECIFICATION.md for complete list

    @abstractmethod
    def acquire_mutex(self) -> Tuple[bool, str]:
        pass

    @abstractmethod
    def release_mutex(self) -> Tuple[bool, str]:
        pass

    # Boolean operations
    @abstractmethod
    def read_bool_input(self, buffer_idx: int, bit_idx: int, thread_safe: bool = True) -> Tuple[bool, str]:
        pass

    @abstractmethod
    def read_bool_output(self, buffer_idx: int, bit_idx: int, thread_safe: bool = True) -> Tuple[bool, str]:
        pass

    @abstractmethod
    def write_bool_output(self, buffer_idx: int, bit_idx: int, value: bool, thread_safe: bool = True) -> Tuple[bool, str]:
        pass

    # And so on for all other methods...
    # (Complete list in API_SPECIFICATION.md)
