#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Safe Logging Access Module

This module provides safe logging operations for OpenPLC Python plugins
"""

class SafeLoggingAccess:
    """Wrapper class for safe logging operations with validation"""

    def __init__(self, runtime_args):
        """
        Initialize with validated runtime args
        Args:
            runtime_args: PluginRuntimeArgs instance
        """
        self.args = runtime_args
        self.is_valid, self.error_msg = self._validate_logging_functions()

    def _validate_logging_functions(self):
        """
        Validate that logging function pointers are available
        Returns: (bool, str) - (is_valid, error_message)
        """
        try:
            # Check if logging functions are available
            if not hasattr(self.args, 'log_info') or not self.args.log_info:
                return False, "log_info function pointer is NULL"
            if not hasattr(self.args, 'log_debug') or not self.args.log_debug:
                return False, "log_debug function pointer is NULL"
            if not hasattr(self.args, 'log_warn') or not self.args.log_warn:
                return False, "log_warn function pointer is NULL"
            if not hasattr(self.args, 'log_error') or not self.args.log_error:
                return False, "log_error function pointer is NULL"

            return True, "All logging functions valid"

        except (AttributeError, TypeError) as e:
            return False, f"Structure access error during logging validation: {e}"
        except (ValueError, OverflowError) as e:
            return False, f"Value validation error during logging validation: {e}"
        except OSError as e:
            return False, f"System error during logging validation: {e}"

    @staticmethod
    def _handle_logging_exception(exception, operation_name):
        """
        Centralized exception handling for logging operations
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

    def log_info(self, message):
        """
        Safely log an informational message
        Args:
            message: String message to log
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid logging setup: {self.error_msg}"

        try:
            if not isinstance(message, str):
                return False, f"Message must be a string, got {type(message)}"

            # Convert to bytes for C function call
            message_bytes = message.encode('utf-8')

            # Call the C logging function
            self.args.log_info(message_bytes)
            return True, "Success"

        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_logging_exception(e, "info logging")

    def log_debug(self, message):
        """
        Safely log a debug message
        Args:
            message: String message to log
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid logging setup: {self.error_msg}"

        try:
            if not isinstance(message, str):
                return False, f"Message must be a string, got {type(message)}"

            # Convert to bytes for C function call
            message_bytes = message.encode('utf-8')

            # Call the C logging function
            self.args.log_debug(message_bytes)
            return True, "Success"

        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_logging_exception(e, "debug logging")

    def log_warn(self, message):
        """
        Safely log a warning message
        Args:
            message: String message to log
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid logging setup: {self.error_msg}"

        try:
            if not isinstance(message, str):
                return False, f"Message must be a string, got {type(message)}"

            # Convert to bytes for C function call
            message_bytes = message.encode('utf-8')

            # Call the C logging function
            self.args.log_warn(message_bytes)
            return True, "Success"

        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_logging_exception(e, "warning logging")

    def log_error(self, message):
        """
        Safely log an error message
        Args:
            message: String message to log
        Returns: (bool, str) - (success, error_message)
        """
        if not self.is_valid:
            return False, f"Invalid logging setup: {self.error_msg}"

        try:
            if not isinstance(message, str):
                return False, f"Message must be a string, got {type(message)}"

            # Convert to bytes for C function call
            message_bytes = message.encode('utf-8')

            # Call the C logging function
            self.args.log_error(message_bytes)
            return True, "Success"

        except (AttributeError, TypeError, ValueError, OverflowError, OSError, MemoryError) as e:
            return False, self._handle_logging_exception(e, "error logging")
