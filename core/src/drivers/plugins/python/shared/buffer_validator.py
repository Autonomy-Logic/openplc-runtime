# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Buffer Validator for OpenPLC Python Plugin System

This module provides centralized validation logic for buffer operations.
It validates buffer indices, bit indices, value ranges, and operation parameters.
"""

from typing import Any, Optional, Tuple

try:
    # Try relative imports first (when used as package)
    from .component_interfaces import IBufferValidator
    from .buffer_types import get_buffer_types
except ImportError:
    # Fall back to absolute imports (when testing standalone)
    from component_interfaces import IBufferValidator
    from buffer_types import get_buffer_types


class BufferValidator(IBufferValidator):
    """
    Centralized validation for buffer operations.

    This class consolidates all validation logic that was previously scattered
    throughout the SafeBufferAccess class. It provides comprehensive validation
    for buffer indices, bit indices, value ranges, and operation parameters.
    """

    def __init__(self, runtime_args):
        """
        Initialize the buffer validator.

        Args:
            runtime_args: PluginRuntimeArgs instance
        """
        self.args = runtime_args
        self.buffer_types = get_buffer_types()

    def validate_buffer_index(self, buffer_idx: int, buffer_type: str) -> Tuple[bool, str]:
        """
        Validate buffer index for a given buffer type.

        Args:
            buffer_idx: Buffer index to validate
            buffer_type: Buffer type name (e.g., 'bool_input', 'int_output')

        Returns:
            Tuple[bool, str]: (is_valid, error_message)
        """
        try:
            # Check if buffer type exists
            if not self.buffer_types.validate_buffer_exists(buffer_type):
                return False, f"Unknown buffer type: {buffer_type}"

            # Validate index range
            if buffer_idx < 0:
                return False, f"Buffer index cannot be negative: {buffer_idx}"

            if buffer_idx >= self.args.buffer_size:
                return False, f"Buffer index out of range: {buffer_idx} >= {self.args.buffer_size}"

            return True, "Success"

        except (AttributeError, TypeError) as e:
            return False, f"Validation error: {e}"

    def validate_bit_index(self, bit_idx: int) -> Tuple[bool, str]:
        """
        Validate bit index for boolean operations.

        Args:
            bit_idx: Bit index to validate (0-63 for 64-bit buffers)

        Returns:
            Tuple[bool, str]: (is_valid, error_message)
        """
        try:
            if bit_idx < 0:
                return False, f"Bit index cannot be negative: {bit_idx}"

            if bit_idx >= self.args.bits_per_buffer:
                return False, f"Bit index out of range: {bit_idx} >= {self.args.bits_per_buffer}"

            return True, "Success"

        except (AttributeError, TypeError) as e:
            return False, f"Bit index validation error: {e}"

    def validate_value_range(self, value: Any, buffer_type: str) -> Tuple[bool, str]:
        """
        Validate that a value is within the acceptable range for a buffer type.

        Args:
            value: Value to validate
            buffer_type: Buffer type name (e.g., 'bool_input', 'int_output')

        Returns:
            Tuple[bool, str]: (is_valid, error_message)
        """
        try:
            # Get buffer type info
            buffer_type_obj, _ = self.buffer_types.get_buffer_info(buffer_type)
            min_val, max_val = buffer_type_obj.value_range

            # Handle boolean values
            if buffer_type_obj.name == 'bool':
                if isinstance(value, bool):
                    return True, "Success"
                elif isinstance(value, (int, float)):
                    if value in (0, 1):
                        return True, "Success"
                    else:
                        return False, f"Boolean value must be 0 or 1, got: {value}"
                else:
                    return False, f"Invalid type for boolean buffer: {type(value)}"

            # Handle numeric values
            if not isinstance(value, (int, float)):
                return False, f"Value must be numeric, got: {type(value)}"

            # Convert to int for range checking
            int_value = int(value)

            if int_value < min_val:
                return False, f"Value too small: {int_value} < {min_val}"

            if int_value > max_val:
                return False, f"Value too large: {int_value} > {max_val}"

            return True, "Success"

        except (AttributeError, TypeError, ValueError) as e:
            return False, f"Value validation error: {e}"

    def validate_operation_params(self, buffer_type: str, buffer_idx: int,
                                bit_idx: Optional[int] = None, value: Any = None) -> Tuple[bool, str]:
        """
        Comprehensive validation of all operation parameters.

        Args:
            buffer_type: Buffer type name
            buffer_idx: Buffer index
            bit_idx: Bit index (required for boolean operations)
            value: Value to write (for write operations)

        Returns:
            Tuple[bool, str]: (is_valid, error_message)
        """
        try:
            # Validate buffer index
            is_valid, msg = self.validate_buffer_index(buffer_idx, buffer_type)
            if not is_valid:
                return False, msg

            # Get buffer type info
            buffer_type_obj, _ = self.buffer_types.get_buffer_info(buffer_type)

            # Validate bit index for boolean operations
            if buffer_type_obj.requires_bit_index:
                if bit_idx is None:
                    return False, f"Bit index required for {buffer_type}"
                is_valid, msg = self.validate_bit_index(bit_idx)
                if not is_valid:
                    return False, msg
            elif bit_idx is not None:
                return False, f"Bit index not allowed for {buffer_type}"

            # Validate value if provided
            if value is not None:
                is_valid, msg = self.validate_value_range(value, buffer_type)
                if not is_valid:
                    return False, msg

            return True, "All parameters valid"

        except (AttributeError, TypeError, ValueError) as e:
            return False, f"Parameter validation error: {e}"

    def get_buffer_constraints(self, buffer_type: str) -> Tuple[Tuple[int, int], bool]:
        """
        Get buffer constraints for a given type.

        Args:
            buffer_type: Buffer type name

        Returns:
            Tuple[Tuple[int, int], bool]: ((min_val, max_val), requires_bit_index)
        """
        try:
            buffer_type_obj, _ = self.buffer_types.get_buffer_info(buffer_type)
            return buffer_type_obj.value_range, buffer_type_obj.requires_bit_index
        except (AttributeError, TypeError, ValueError) as e:
            # Return safe defaults on error
            return ((0, 0), False)

    def is_buffer_type_supported(self, buffer_type: str) -> bool:
        """
        Check if a buffer type is supported.

        Args:
            buffer_type: Buffer type name to check

        Returns:
            bool: True if supported, False otherwise
        """
        return self.buffer_types.validate_buffer_exists(buffer_type)

    def get_validation_summary(self) -> dict:
        """
        Get a summary of validation configuration.

        Returns:
            dict: Validation configuration summary
        """
        try:
            return {
                'buffer_size': self.args.buffer_size,
                'bits_per_buffer': self.args.bits_per_buffer,
                'supported_buffer_types': list(self.buffer_types.get_all_buffers().keys()),
                'supported_base_types': list(self.buffer_types.get_all_types().keys())
            }
        except (AttributeError, TypeError) as e:
            return {'error': str(e)}
