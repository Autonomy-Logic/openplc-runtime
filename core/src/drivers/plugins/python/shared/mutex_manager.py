# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Mutex Manager for OpenPLC Python Plugin System

This module provides centralized lock management for thread-safe buffer reads.
It wraps the runtime's flush-on-lock image API: acquire() takes the image mutex
and drains the journal (so reads see freshly committed values), release() frees
it. Buffer WRITES do not use this lock -- they go through journal_write_*.

The public method names (acquire / release / with_mutex) are unchanged so the
plugin-facing BufferAccessor stays source-compatible; only the underlying lock
moved from the retired buffer_mutex to image_lock()/image_unlock().
"""

from typing import Any, Callable
try:
    # Try relative imports first (when used as package)
    from .component_interfaces import IMutexManager
except ImportError:
    # Fall back to absolute imports (when testing standalone)
    from component_interfaces import IMutexManager


class MutexManager(IMutexManager):
    """
    Manages mutex operations for thread-safe buffer access.

    This class encapsulates all mutex-related functionality, providing a clean
    interface for acquiring, releasing, and using mutexes in a thread-safe manner.
    """

    def __init__(self, runtime_args):
        """
        Initialize the mutex manager.

        Args:
            runtime_args: PluginRuntimeArgs instance containing mutex pointers
        """
        self.args = runtime_args

    def acquire(self) -> bool:
        """
        Acquire the image lock (image mutex + journal drain).

        Returns:
            bool: True if the lock was acquired, False if unavailable
        """
        if not self.args.image_lock:
            return False

        self.args.image_lock()
        return True

    def release(self) -> bool:
        """
        Release the image lock.

        Returns:
            bool: True if released, False if unavailable
        """
        if not self.args.image_unlock:
            return False

        self.args.image_unlock()
        return True

    def with_mutex(self, operation: Callable[[], Any]) -> Any:
        """
        Execute an operation within a mutex-protected context.

        This method acquires the mutex, executes the operation, and ensures
        the mutex is always released, even if the operation raises an exception.

        Args:
            operation: Callable that performs the operation to protect

        Returns:
            Any: Result of the operation, or (False, error_message) if mutex acquisition fails

        Example:
            result = mutex_manager.with_mutex(lambda: self._perform_buffer_read())
        """
        if not self.acquire():
            return False, "Failed to acquire mutex"

        try:
            return operation()
        finally:
            self.release()

    def is_mutex_available(self) -> bool:
        """
        Check if the image lock is available for use.

        Returns:
            bool: True if the lock function pointers are valid, False otherwise
        """
        return (
            self.args.image_lock is not None and
            self.args.image_unlock is not None
        )

    def get_mutex_status(self) -> str:
        """
        Get a human-readable status of the lock configuration.

        Returns:
            str: Status description
        """
        if not self.args.image_lock:
            return "No image_lock function available"
        if not self.args.image_unlock:
            return "No image_unlock function available"
        return "Image lock properly configured"
