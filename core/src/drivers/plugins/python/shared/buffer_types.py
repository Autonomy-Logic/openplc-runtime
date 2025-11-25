"""
Buffer Type Definitions for OpenPLC Python Plugin System

This module defines all buffer types and their characteristics in a centralized,
extensible way. Adding a new buffer type requires only adding a new class here.
"""

import ctypes
from typing import Tuple

try:
    # Try relative imports first (when used as package)
    from .component_interfaces import IBufferType
except ImportError:
    # Fall back to absolute imports (when testing standalone)
    from component_interfaces import IBufferType


class BoolBufferType(IBufferType):
    """Boolean buffer type (1-bit values accessed via bit indexing)"""

    @property
    def name(self) -> str:
        return "bool"

    @property
    def size_bytes(self) -> int:
        return 1

    @property
    def value_range(self) -> Tuple[int, int]:
        return (0, 1)

    @property
    def requires_bit_index(self) -> bool:
        return True

    @property
    def ctype_class(self) -> type:
        return ctypes.c_uint8


class ByteBufferType(IBufferType):
    """Byte buffer type (8-bit unsigned integer)"""

    @property
    def name(self) -> str:
        return "byte"

    @property
    def size_bytes(self) -> int:
        return 1

    @property
    def value_range(self) -> Tuple[int, int]:
        return (0, 255)

    @property
    def requires_bit_index(self) -> bool:
        return False

    @property
    def ctype_class(self) -> type:
        return ctypes.c_uint8


class IntBufferType(IBufferType):
    """Integer buffer type (16-bit unsigned integer)"""

    @property
    def name(self) -> str:
        return "int"

    @property
    def size_bytes(self) -> int:
        return 2

    @property
    def value_range(self) -> Tuple[int, int]:
        return (0, 65535)

    @property
    def requires_bit_index(self) -> bool:
        return False

    @property
    def ctype_class(self) -> type:
        return ctypes.c_uint16


class DintBufferType(IBufferType):
    """Double integer buffer type (32-bit unsigned integer)"""

    @property
    def name(self) -> str:
        return "dint"

    @property
    def size_bytes(self) -> int:
        return 4

    @property
    def value_range(self) -> Tuple[int, int]:
        return (0, 4294967295)

    @property
    def requires_bit_index(self) -> bool:
        return False

    @property
    def ctype_class(self) -> type:
        return ctypes.c_uint32


class LintBufferType(IBufferType):
    """Long integer buffer type (64-bit unsigned integer)"""

    @property
    def name(self) -> str:
        return "lint"

    @property
    def size_bytes(self) -> int:
        return 8

    @property
    def value_range(self) -> Tuple[int, int]:
        return (0, 18446744073709551615)

    @property
    def requires_bit_index(self) -> bool:
        return False

    @property
    def ctype_class(self) -> type:
        return ctypes.c_uint64


# Core buffer types (module-level constants)
_BUFFER_TYPES = {
    'bool': BoolBufferType(),
    'byte': ByteBufferType(),
    'int': IntBufferType(),
    'dint': DintBufferType(),
    'lint': LintBufferType(),
}

# Buffer type mappings (used by the facade to map method names to types)
_BUFFER_MAPPINGS = {
    # Boolean buffers
    'bool_input': ('bool', 'input'),
    'bool_output': ('bool', 'output'),

    # Byte buffers
    'byte_input': ('byte', 'input'),
    'byte_output': ('byte', 'output'),

    # Integer buffers (16-bit)
    'int_input': ('int', 'input'),
    'int_output': ('int', 'output'),
    'int_memory': ('int', 'memory'),

    # Double integer buffers (32-bit)
    'dint_input': ('dint', 'input'),
    'dint_output': ('dint', 'output'),
    'dint_memory': ('dint', 'memory'),

    # Long integer buffers (64-bit)
    'lint_input': ('lint', 'input'),
    'lint_output': ('lint', 'output'),
    'lint_memory': ('lint', 'memory'),
}


class BufferTypes:
    """
    Utility class for accessing buffer type definitions and metadata.
    
    This class provides a centralized way to access buffer type definitions
    and metadata. It's used by validators and accessors to understand buffer
    characteristics.
    """

    @staticmethod
    def get_type(type_name: str) -> IBufferType:
        """Get buffer type definition by name"""
        if type_name not in _BUFFER_TYPES:
            raise ValueError(f"Unknown buffer type: {type_name}")
        return _BUFFER_TYPES[type_name]

    @staticmethod
    def get_buffer_info(buffer_name: str) -> Tuple[IBufferType, str]:
        """
        Get buffer type and direction for a buffer name

        Args:
            buffer_name: e.g., 'bool_input', 'int_output', 'dint_memory'

        Returns:
            Tuple of (IBufferType, direction) where direction is 'input', 'output', or 'memory'
        """
        if buffer_name not in _BUFFER_MAPPINGS:
            raise ValueError(f"Unknown buffer name: {buffer_name}")

        type_name, direction = _BUFFER_MAPPINGS[buffer_name]
        buffer_type = BufferTypes.get_type(type_name)
        return buffer_type, direction

    @staticmethod
    def get_all_types() -> dict:
        """Get all buffer type definitions"""
        return _BUFFER_TYPES.copy()

    @staticmethod
    def get_all_buffers() -> dict:
        """Get all buffer name mappings"""
        return _BUFFER_MAPPINGS.copy()

    @staticmethod
    def validate_type_exists(type_name: str) -> bool:
        """Check if a buffer type exists"""
        return type_name in _BUFFER_TYPES

    @staticmethod
    def validate_buffer_exists(buffer_name: str) -> bool:
        """Check if a buffer name exists"""
        return buffer_name in _BUFFER_MAPPINGS


def get_buffer_types() -> BufferTypes:
    """Get the BufferTypes utility class"""
    return BufferTypes
