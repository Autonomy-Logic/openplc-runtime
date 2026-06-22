"""
Centralized logging module for OPC-UA plugins (server and client).

This module provides a singleton logger that integrates with the OpenPLC
runtime logging system while providing fallback to standard output.
"""

import sys
from datetime import datetime, timezone
from typing import Callable, Optional


class OpcuaLogger:
    """
    Singleton logger for OPC-UA plugins.

    Integrates with OpenPLC runtime logging when available,
    falls back to stdout/stderr otherwise.
    """

    _instance: Optional["OpcuaLogger"] = None

    def __init__(self):
        self._log_info_fn: Optional[Callable[[str], None]] = None
        self._log_warn_fn: Optional[Callable[[str], None]] = None
        self._log_error_fn: Optional[Callable[[str], None]] = None
        self._log_debug_fn: Optional[Callable[[str], None]] = None
        self._initialized = False

    @classmethod
    def get_instance(cls) -> "OpcuaLogger":
        """Get the singleton logger instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance. Useful for testing."""
        cls._instance = None

    def initialize(self, logging_accessor) -> bool:
        """
        Initialize logger with OpenPLC runtime logging accessor.

        Args:
            logging_accessor: SafeLoggingAccess instance from runtime

        Returns:
            True if initialization successful, False otherwise
        """
        if logging_accessor is None:
            return False

        if not getattr(logging_accessor, "is_valid", False):
            return False

        self._log_info_fn = getattr(logging_accessor, "log_info", None)
        self._log_warn_fn = getattr(logging_accessor, "log_warn", None)
        self._log_error_fn = getattr(logging_accessor, "log_error", None)
        self._log_debug_fn = getattr(logging_accessor, "log_debug", None)
        self._initialized = True
        return True

    def info(self, message: str) -> None:
        """Log an informational message."""
        if self._initialized and self._log_info_fn:
            try:
                self._log_info_fn(message)
                return
            except Exception:
                pass
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [INFO] [OPCUA] {message}", file=sys.stdout)

    def warn(self, message: str) -> None:
        """Log a warning message."""
        if self._initialized and self._log_warn_fn:
            try:
                self._log_warn_fn(message)
                return
            except Exception:
                pass
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [WARN] [OPCUA] {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        """Log an error message."""
        if self._initialized and self._log_error_fn:
            try:
                self._log_error_fn(message)
                return
            except Exception:
                pass
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [ERROR] [OPCUA] {message}", file=sys.stderr)

    def debug(self, message: str) -> None:
        """Log a debug message."""
        if self._initialized and self._log_debug_fn:
            try:
                self._log_debug_fn(message)
                return
            except Exception:
                pass
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [DEBUG] [OPCUA] {message}", file=sys.stdout)


# Module-level convenience functions
def get_logger() -> OpcuaLogger:
    """Get the singleton logger instance."""
    return OpcuaLogger.get_instance()


def log_info(message: str) -> None:
    """Log an informational message."""
    get_logger().info(message)


def log_warn(message: str) -> None:
    """Log a warning message."""
    get_logger().warn(message)


def log_error(message: str) -> None:
    """Log an error message."""
    get_logger().error(message)


def log_debug(message: str) -> None:
    """Log a debug message."""
    get_logger().debug(message)
