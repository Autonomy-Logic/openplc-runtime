# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Centralized Plugin Logger Module

Provides a simple, consistent logging interface for OpenPLC Python plugins that
routes log messages through the central logging system:

    C runtime log functions -> Unix socket -> Python log server ->
    REST API -> OpenPLC Editor

This ensures all plugin logs are visible in the Editor's log viewer.

Usage:
    from shared import PluginLogger

    # In plugin init():
    logger = PluginLogger("MODBUS_SLAVE", runtime_args)

    # Throughout plugin:
    logger.info("Server started on port 502")
    logger.error("Connection failed: timeout")
    logger.warn("Retrying connection...")
    logger.debug("Processing request...")

The plugin name is automatically prefixed to all messages, e.g.:
    "[MODBUS_SLAVE] Server started on port 502"
"""

from datetime import datetime, timezone
from typing import Optional
from .safe_logging_access import SafeLoggingAccess


class PluginLogger:
    """
    Thread-safe logger for OpenPLC plugins that routes messages to the
    central logging system.

    Attributes:
        plugin_name: Name of the plugin (used as prefix in log messages)
        is_valid: True if the logger is properly connected to central logging
    """

    def __init__(self, plugin_name: str, runtime_args=None):
        """
        Initialize the plugin logger.

        Args:
            plugin_name: Name of the plugin (e.g., "MODBUS_SLAVE", "MODBUS_MASTER").
                        This will be used as a prefix in all log messages.
            runtime_args: PluginRuntimeArgs instance containing logging function
                         pointers. If None, logger will fall back to print().
        """
        self.plugin_name = plugin_name
        self._prefix = f"[{plugin_name}]"
        self._logging_access: Optional[SafeLoggingAccess] = None
        self._is_valid = False

        if runtime_args is not None:
            self._logging_access = SafeLoggingAccess(runtime_args)
            if self._logging_access.is_valid:
                self._is_valid = True
            else:
                # Log the validation failure via fallback
                self._fallback_print(
                    "WARN",
                    f"SafeLoggingAccess not valid: {self._logging_access.error_msg}. "
                    "Falling back to print()."
                )
                self._logging_access = None

    @property
    def is_valid(self) -> bool:
        """Returns True if logger is connected to central logging system."""
        return self._is_valid

    def _format_message(self, message: str) -> str:
        """Format message with plugin name prefix."""
        return f"{self._prefix} {message}"

    def _fallback_print(self, level: str, message: str):
        """
        Fallback to print when logging access is unavailable.

        This ensures messages are still visible in stdout even if the
        central logging system is not available.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [{level}] {self._prefix} {message}")

    def info(self, message: str) -> bool:
        """
        Log an informational message.

        Args:
            message: The message to log

        Returns:
            True if message was sent to central logging, False if fallback was used
        """
        formatted = self._format_message(message)
        if self._logging_access:
            success, error_msg = self._logging_access.log_info(formatted)
            if success:
                return True
            # If logging failed, fall back to print
            self._fallback_print("INFO", message)
            return False
        else:
            self._fallback_print("INFO", message)
            return False

    def error(self, message: str) -> bool:
        """
        Log an error message.

        Args:
            message: The message to log

        Returns:
            True if message was sent to central logging, False if fallback was used
        """
        formatted = self._format_message(message)
        if self._logging_access:
            success, error_msg = self._logging_access.log_error(formatted)
            if success:
                return True
            self._fallback_print("ERROR", message)
            return False
        else:
            self._fallback_print("ERROR", message)
            return False

    def warn(self, message: str) -> bool:
        """
        Log a warning message.

        Args:
            message: The message to log

        Returns:
            True if message was sent to central logging, False if fallback was used
        """
        formatted = self._format_message(message)
        if self._logging_access:
            success, error_msg = self._logging_access.log_warn(formatted)
            if success:
                return True
            self._fallback_print("WARN", message)
            return False
        else:
            self._fallback_print("WARN", message)
            return False

    def debug(self, message: str) -> bool:
        """
        Log a debug message.

        Args:
            message: The message to log

        Returns:
            True if message was sent to central logging, False if fallback was used
        """
        formatted = self._format_message(message)
        if self._logging_access:
            success, error_msg = self._logging_access.log_debug(formatted)
            if success:
                return True
            self._fallback_print("DEBUG", message)
            return False
        else:
            self._fallback_print("DEBUG", message)
            return False
