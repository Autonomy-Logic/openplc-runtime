"""
Debug Utilities for OpenPLC Python Plugin System

This module provides debug and variable access utilities.
It handles variable listing, size queries, value reading/writing, and other debug operations.
"""

from typing import List, Tuple, Dict, Any, Optional
import ctypes
try:
    # Try relative imports first (when used as package)
    from .component_interfaces import IDebugUtils
except ImportError:
    # Fall back to absolute imports (when testing standalone)
    from component_interfaces import IDebugUtils


class DebugUtils(IDebugUtils):
    """
    Provides debug and variable access utilities.

    This class encapsulates all debug-related operations, including variable
    discovery, size queries, and direct memory access for debugging purposes.
    """

    def __init__(self, runtime_args):
        """
        Initialize the debug utilities.

        Args:
            runtime_args: PluginRuntimeArgs instance
        """
        self.args = runtime_args

    def get_var_list(self, indexes: List[int]) -> Tuple[List[int], str]:
        """
        Get a list of variable addresses for the given indexes.

        Args:
            indexes: List of integer indexes to get addresses for

        Returns:
            Tuple[List[int], str]: (addresses, error_message)
                addresses format: [address1, address2, ...] where each address is an int
        """
        if not indexes:
            return [], "No indexes provided"

        if not isinstance(indexes, (list, tuple)):
            return [], "Indexes must be a list or tuple"

        try:
            # Convert Python list to C arrays
            num_vars = len(indexes)
            indexes_array = (ctypes.c_size_t * num_vars)(*indexes)
            result_array = (ctypes.c_void_p * num_vars)()

            # Call the C function
            self.args.get_var_list(num_vars, indexes_array, result_array)

            # Convert result back to Python list
            addresses = []
            for i in range(num_vars):
                addr = result_array[i]
                if addr is None:
                    addresses.append(None)
                else:
                    # Convert void pointer to integer address
                    addresses.append(ctypes.cast(addr, ctypes.c_void_p).value)

            return addresses, "Success"

        except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
            return [], f"Exception during get_var_list: {e}"

    def get_var_size(self, index: int) -> Tuple[int, str]:
        """
        Get the size of a variable at the given index.

        Args:
            index: Integer index of the variable

        Returns:
            Tuple[int, str]: (size, error_message)
        """
        try:  
            size = self.args.get_var_size(ctypes.c_size_t(index))
            return size, "Success"

        except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
            return 0, f"Exception during get_var_size: {e}"

    def get_var_value(self, index: int) -> Tuple[Any, str]:
        """
        Read a variable value by index with automatic type handling based on size.

        Args:
            index: Integer index of the variable

        Returns:
            Tuple[Any, str]: (value, error_message)
        """
        try:       
            # Get variable address and size
            addresses, addr_err = self.get_var_list([index])
            if not addresses or addresses[0] is None:
                return None, f"Failed to get variable address: {addr_err}"

            size, size_err = self.get_var_size(index)
            if size == 0:
                return None, f"Failed to get variable size: {size_err}"

            address = addresses[0]

            # Read value based on size (since we can't determine exact type)
            if size == 1:
                # Could be BOOL, BOOL_O, or SINT - read as unsigned and let user interpret
                value_ptr = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint8))
                value = value_ptr.contents.value
                return value, "Success"

            elif size == 2:
                # 16-bit unsigned integer
                value_ptr = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint16))
                value = value_ptr.contents.value
                return value, "Success"

            elif size == 4:
                # 32-bit unsigned integer (could be TIME)
                value_ptr = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint32))
                value = value_ptr.contents.value
                return value, "Success"

            elif size == 8:
                # 64-bit unsigned integer (could be TIME)
                value_ptr = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint64))
                value = value_ptr.contents.value
                return value, "Success"

            else:
                return None, f"Unsupported variable size: {size}"

        except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
            return None, f"Exception during get_var_value: {e}"

    def set_var_value(self, index: int, value: Any) -> Tuple[bool, str]:
        """
        Write a variable value by index with size-based validation.

        Args:
            index: Integer index of the variable
            value: Value to write

        Returns:
            Tuple[bool, str]: (success, error_message)
        """
        try:   
            # Get variable address and size
            addresses, addr_err = self.get_var_list([index])
            if not addresses or addresses[0] is None:
                return False, f"Failed to get variable address: {addr_err}"

            size, size_err = self.get_var_size(index)
            if size == 0:
                return False, f"Failed to get variable size: {size_err}"

            address = addresses[0]

            # Validate value type
            if not isinstance(value, (bool, int)):
                return False, f"Invalid value type: expected bool or int, got {type(value)}"

            # Convert boolean to integer
            if isinstance(value, bool):
                value = 1 if value else 0

            # Validate and write value based on size
            if size == 1:
                # 8-bit value (BOOL, BOOL_O, or SINT)
                if not (0 <= value <= 255):
                    return False, f"Invalid value: {value} (must be 0-255 for 8-bit)"
                value_ptr = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint8))
                value_ptr.contents.value = value
                return True, "Success"

            elif size == 2:
                # 16-bit unsigned integer
                if not (0 <= value <= 65535):
                    return False, f"Invalid value: {value} (must be 0-65535 for 16-bit)"
                value_ptr = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint16))
                value_ptr.contents.value = value
                return True, "Success"

            elif size == 4:
                # 32-bit unsigned integer
                if not (0 <= value <= 4294967295):
                    return False, f"Invalid value: {value} (must be 0-4294967295 for 32-bit)"
                value_ptr = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint32))
                value_ptr.contents.value = value
                return True, "Success"

            elif size == 8:
                # 64-bit unsigned integer
                if not (0 <= value <= 18446744073709551615):
                    return False, f"Invalid value: {value} (must be 0-18446744073709551615 for 64-bit)"
                value_ptr = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint64))
                value_ptr.contents.value = value
                return True, "Success"

            else:
                return False, f"Unsupported variable size: {size}"

        except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
            return False, f"Exception during set_var_value: {e}"

    def get_var_count(self) -> Tuple[int, str]:
        """
        Get the total number of debug variables available.

        Returns:
            Tuple[int, str]: (count, error_message)
        """
        try:
            count = self.args.get_var_count()
            return count, "Success"

        except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
            return 0, f"Exception during get_var_count: {e}"

    def get_var_info(self, index: int) -> Tuple[Dict, str]:
        """
        Get comprehensive information about a variable.

        Args:
            index: Integer index of the variable

        Returns:
            Tuple[Dict, str]: (info_dict, error_message)
                info_dict format: {'address': int, 'size': int, 'inferred_type': str}
        """
        try:
            # Get variable address
            addresses, addr_err = self.get_var_list([index])
            if not addresses or addresses[0] is None:
                return {}, f"Failed to get variable address: {addr_err}"

            # Get variable size
            size, size_err = self.get_var_size(index)
            if size == 0:
                return {}, f"Failed to get variable size: {size_err}"

            # Infer type from size
            inferred_type = self._infer_var_type_from_size(size)

            info = {
                'address': addresses[0],
                'size': size,
                'inferred_type': inferred_type
            }

            return info, "Success"

        except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
            return {}, f"Exception during get_var_info: {e}"

    def _infer_var_type_from_size(self, size: int) -> str:
        """
        Infer variable type based on size.

        Based on debug.c size mappings:
        - BOOL/BOOL_O: sizeof(BOOL) = 1 byte
        - SINT: sizeof(SINT) = 1 byte
        - TIME: sizeof(TIME) = 4 or 8 bytes

        Args:
            size: Size in bytes

        Returns:
            str: Inferred type name for debugging
        """
        if size == 1:
            return "BOOL_OR_SINT"  # Cannot distinguish between BOOL and SINT by size alone
        elif size == 2:
            return "UINT16"
        elif size == 4:
            return "UINT32_OR_TIME"
        elif size == 8:
            return "UINT64_OR_TIME"
        else:
            return "UNKNOWN"
