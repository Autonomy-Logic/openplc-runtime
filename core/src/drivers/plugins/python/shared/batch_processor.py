# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Batch Processor for OpenPLC Python Plugin System

This module handles batch operations for optimized buffer access.
It processes multiple read/write operations with a single mutex acquisition.

Note on thread safety:
- Read operations require mutex to ensure consistent reads from image tables
- Write operations go through the journal buffer system which is internally
  thread-safe. The mutex is still acquired for batch writes to maintain API
  consistency and to support mixed read/write operations atomically.
"""

from typing import List, Tuple, Dict, Any
try:
    # Try relative imports first (when used as package)
    from .component_interfaces import IBatchProcessor
    from .buffer_accessor import GenericBufferAccessor
    from .mutex_manager import MutexManager
except ImportError:
    # Fall back to absolute imports (when testing standalone)
    from component_interfaces import IBatchProcessor
    from buffer_accessor import GenericBufferAccessor
    from mutex_manager import MutexManager


class BatchProcessor(IBatchProcessor):
    """
    Processes batch operations for optimized buffer access.

    This class handles multiple buffer operations in a single batch,
    acquiring the mutex only once for the entire batch. This provides
    better performance for operations that need to access multiple buffers.
    """

    def __init__(self, buffer_accessor: GenericBufferAccessor, mutex_manager: MutexManager):
        """
        Initialize the batch processor.

        Args:
            buffer_accessor: GenericBufferAccessor instance
            mutex_manager: MutexManager instance
        """
        self.accessor = buffer_accessor
        self.mutex = mutex_manager

    def process_batch_reads(self, operations: List[Tuple]) -> Tuple[List[Tuple], str]:
        """
        Process multiple read operations in a batch.

        Args:
            operations: List of read operation tuples
                       Format: [('buffer_type', buffer_idx, bit_idx), ...]
                       bit_idx is optional for non-boolean operations

        Returns:
            Tuple[List[Tuple], str]: (results, error_message)
                results format: [(success, value, error_msg), ...]
        """
        if not operations:
            return [], "No operations provided"

        results = []

        # Acquire mutex once for all operations
        if not self.mutex.acquire():
            return [], "Failed to acquire mutex for batch read"

        try:
            for operation in operations:
                try:
                    if len(operation) < 2:
                        results.append((False, None, "Invalid operation format"))
                        continue

                    buffer_type = operation[0]
                    buffer_idx = operation[1]
                    bit_idx = operation[2] if len(operation) > 2 else None

                    # Perform read operation without additional mutex
                    value, msg = self.accessor.read_buffer(buffer_type, buffer_idx, bit_idx, thread_safe=False)

                    if msg == "Success":
                        results.append((True, value, msg))
                    else:
                        results.append((False, None, msg))

                except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
                    results.append((False, None, f"Exception during batch read operation: {e}"))

            return results, "Batch read completed"

        finally:
            # Always release the mutex
            self.mutex.release()

    def process_batch_writes(self, operations: List[Tuple]) -> Tuple[List[Tuple], str]:
        """
        Process multiple write operations in a batch.

        Args:
            operations: List of write operation tuples
                       Format: [('buffer_type', buffer_idx, value, bit_idx), ...]
                       bit_idx is optional for non-boolean operations

        Returns:
            Tuple[List[Tuple], str]: (results, error_message)
                results format: [(success, error_msg), ...]
        """
        if not operations:
            return [], "No operations provided"

        results = []

        # Acquire mutex once for all operations
        if not self.mutex.acquire():
            return [], "Failed to acquire mutex for batch write"

        try:
            for operation in operations:
                try:
                    if len(operation) < 3:
                        results.append((False, "Invalid operation format"))
                        continue

                    buffer_type = operation[0]
                    buffer_idx = operation[1]
                    value = operation[2]
                    bit_idx = operation[3] if len(operation) > 3 else None

                    # Perform write operation without additional mutex
                    success, msg = self.accessor.write_buffer(buffer_type, buffer_idx, value, bit_idx, thread_safe=False)

                    results.append((success, msg))

                except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
                    results.append((False, f"Exception during batch write operation: {e}"))

            return results, "Batch write completed"

        finally:
            # Always release the mutex
            self.mutex.release()

    def process_mixed_operations(self, read_operations: List[Tuple],
                               write_operations: List[Tuple]) -> Tuple[Dict, str]:
        """
        Process mixed read and write operations in a batch.

        Args:
            read_operations: List of read operation tuples (same format as process_batch_reads)
            write_operations: List of write operation tuples (same format as process_batch_writes)

        Returns:
            Tuple[Dict, str]: (results_dict, error_message)
                results_dict format: {'reads': [(success, value, error_msg), ...],
                                    'writes': [(success, error_msg), ...]}
        """
        if not read_operations and not write_operations:
            return {}, "No operations provided"

        read_results = []
        write_results = []

        # Acquire mutex once for all operations
        if not self.mutex.acquire():
            return {}, "Failed to acquire mutex for mixed operations"

        try:
            # Process read operations first (typically safer order)
            if read_operations:
                for operation in read_operations:
                    try:
                        if len(operation) < 2:
                            read_results.append((False, None, "Invalid operation format"))
                            continue

                        buffer_type = operation[0]
                        buffer_idx = operation[1]
                        bit_idx = operation[2] if len(operation) > 2 else None

                        # Perform read operation without additional mutex
                        value, msg = self.accessor.read_buffer(buffer_type, buffer_idx, bit_idx, thread_safe=False)

                        if msg == "Success":
                            read_results.append((True, value, msg))
                        else:
                            read_results.append((False, None, msg))

                    except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
                        read_results.append((False, None, f"Exception during mixed read operation: {e}"))

            # Process write operations
            if write_operations:
                for operation in write_operations:
                    try:
                        if len(operation) < 3:
                            write_results.append((False, "Invalid operation format"))
                            continue

                        buffer_type = operation[0]
                        buffer_idx = operation[1]
                        value = operation[2]
                        bit_idx = operation[3] if len(operation) > 3 else None

                        # Perform write operation without additional mutex
                        success, msg = self.accessor.write_buffer(buffer_type, buffer_idx, value, bit_idx, thread_safe=False)

                        write_results.append((success, msg))

                    except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
                        write_results.append((False, f"Exception during mixed write operation: {e}"))

            results = {
                'reads': read_results,
                'writes': write_results
            }

            return results, "Mixed batch operations completed"

        finally:
            # Always release the mutex
            self.mutex.release()

    def validate_batch_operations(self, operations: List[Tuple], is_read: bool = True) -> Tuple[bool, str]:
        """
        Validate batch operations before processing.

        Args:
            operations: List of operation tuples to validate
            is_read: True for read operations, False for write operations

        Returns:
            Tuple[bool, str]: (is_valid, error_message)
        """
        if not operations:
            return True, "Empty batch is valid"

        expected_min_length = 2 if is_read else 3

        for i, operation in enumerate(operations):
            if not isinstance(operation, (list, tuple)):
                return False, f"Operation {i} is not a list or tuple"

            if len(operation) < expected_min_length:
                op_type = "read" if is_read else "write"
                return False, f"Operation {i} has insufficient parameters for {op_type}"

            # Additional validation could be added here
            buffer_type = operation[0]
            if not isinstance(buffer_type, str):
                return False, f"Operation {i}: buffer_type must be a string"

        return True, "Batch operations are valid"
