"""
OPC UA Synchronization Manager.

This module provides bidirectional data synchronization between
OPC-UA server nodes and PLC runtime variables.

Sync Directions:
1. OPC-UA → Runtime: Client writes propagated to PLC
2. Runtime → OPC-UA: PLC values published to clients

Subscription Support:
- Uses write_attribute_value() with DataValue for optimal notification triggering
- Includes SourceTimestamp from PLC cycle and ServerTimestamp for audit trail
- Automatically triggers data change notifications for subscribed clients
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Callable

from asyncua import ua, Server

# Add directories to path for module access
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Import local modules (handle both package and direct loading)
try:
    from .opcua_logging import log_info, log_warn, log_error, log_debug
    from .opcua_types import VariableNode, VariableMetadata
    from .opcua_utils import map_plc_to_opcua_type, convert_value_for_opcua, convert_value_for_plc
    from .opcua_memory import read_memory_direct, initialize_variable_cache
except ImportError:
    from opcua_logging import log_info, log_warn, log_error, log_debug
    from opcua_types import VariableNode, VariableMetadata
    from opcua_utils import map_plc_to_opcua_type, convert_value_for_opcua, convert_value_for_plc
    from opcua_memory import read_memory_direct, initialize_variable_cache

from shared import SafeBufferAccess


class SynchronizationManager:
    """
    Manages bidirectional data synchronization between OPC-UA and PLC runtime.

    Features:
    - Unified sync loop (both directions in single cycle)
    - Change detection to minimize writes
    - Direct memory access optimization when available
    - Batch operations for efficiency
    - Subscription support with proper timestamps

    Usage:
        sync_mgr = SynchronizationManager(buffer_accessor, variable_nodes, server)
        await sync_mgr.initialize()
        await sync_mgr.run(is_running_callback, cycle_time)
    """

    def __init__(
        self,
        buffer_accessor: SafeBufferAccess,
        variable_nodes: Dict[int, VariableNode],
        server: Optional[Server] = None
    ):
        """
        Initialize the synchronization manager.

        Args:
            buffer_accessor: SafeBufferAccess for PLC memory operations
            variable_nodes: Dict mapping variable index to VariableNode
            server: Optional Server instance for optimized write_attribute_value
        """
        self.buffer_accessor = buffer_accessor
        self.variable_nodes = variable_nodes
        self.server = server

        # Optimization: metadata cache for direct memory access
        self.variable_metadata: Dict[int, VariableMetadata] = {}
        self._direct_memory_access_enabled = False

        # Change detection cache (var_index -> last_value)
        self.opcua_value_cache: Dict[int, Any] = {}

        # Readwrite nodes (filtered from variable_nodes)
        self._readwrite_nodes: Dict[int, VariableNode] = {}

        # Cycle timestamp for subscription notifications
        self._cycle_timestamp: Optional[datetime] = None

    async def initialize(self) -> bool:
        """
        Initialize the synchronization manager.

        Sets up:
        - Filters readwrite nodes
        - Initializes metadata cache for direct memory access (including array elements)

        Returns:
            True if initialization successful
        """
        try:
            # Filter readwrite nodes
            self._readwrite_nodes = {
                var_index: var_node
                for var_index, var_node in self.variable_nodes.items()
                if var_node.access_mode == "readwrite"
            }

            log_info(f"Sync manager: {len(self._readwrite_nodes)} readwrite nodes, "
                     f"{len(self.variable_nodes) - len(self._readwrite_nodes)} readonly nodes")

            # Initialize metadata cache for direct memory access
            if self.variable_nodes:
                # Collect all indices including array elements
                var_indices = []
                for var_index, var_node in self.variable_nodes.items():
                    if var_node.array_length and var_node.array_length > 0:
                        # For arrays, add all element indices
                        for i in range(var_node.array_length):
                            var_indices.append(var_index + i)
                    else:
                        var_indices.append(var_index)

                self.variable_metadata = initialize_variable_cache(
                    self.buffer_accessor,
                    var_indices
                )
                self._direct_memory_access_enabled = bool(self.variable_metadata)

                if self._direct_memory_access_enabled:
                    log_info(f"Direct memory access enabled for {len(self.variable_metadata)} indices")
                else:
                    log_info("Using batch operations for sync")

            return True

        except Exception as e:
            log_error(f"Failed to initialize sync manager: {e}")
            return False

    async def run(
        self,
        is_running: Callable[[], bool],
        cycle_time_seconds: float
    ) -> None:
        """
        Run the unified synchronization loop.

        Executes both sync directions sequentially in a single cycle:
        1. OPC-UA → Runtime (client writes to PLC)
        2. Runtime → OPC-UA (PLC values to clients)

        Args:
            is_running: Callback that returns False when loop should stop
            cycle_time_seconds: Time between sync cycles in seconds
        """
        log_info(f"Starting sync loop (cycle time: {cycle_time_seconds*1000:.0f}ms)")

        while is_running():
            try:
                # Capture cycle timestamp for subscription notifications
                self._cycle_timestamp = datetime.now(timezone.utc)

                # Direction 1: OPC-UA → Runtime
                await self.sync_opcua_to_runtime()

                # Direction 2: Runtime → OPC-UA
                await self.sync_runtime_to_opcua()

                # Wait for next cycle
                await asyncio.sleep(cycle_time_seconds)

            except asyncio.CancelledError:
                log_info("Sync loop cancelled")
                break
            except Exception as e:
                log_error(f"Error in sync loop: {e}")
                await asyncio.sleep(0.1)  # Brief pause on error

        log_info("Sync loop stopped")

    async def sync_opcua_to_runtime(self) -> None:
        """
        Synchronize values from OPC-UA readwrite nodes to PLC runtime.

        Only syncs changed values to minimize PLC writes.
        """
        try:
            if not self._readwrite_nodes:
                return

            # Collect values to write (only changed values)
            values_to_write = []
            indices_to_write = []

            for var_index, var_node in self._readwrite_nodes.items():
                try:
                    # Read current value from OPC-UA node
                    opcua_value = await var_node.node.read_value()

                    # Extract actual value
                    actual_value = self._extract_opcua_value(opcua_value)
                    if actual_value is None:
                        continue

                    # Check if this is an array node
                    if var_node.array_length and var_node.array_length > 0:
                        # Handle array: value should be a list
                        if isinstance(actual_value, (list, tuple)):
                            for i, elem_value in enumerate(actual_value):
                                elem_index = var_index + i
                                plc_value = convert_value_for_plc(var_node.datatype, elem_value)

                                # Check if element has changed
                                if self._has_value_changed(elem_index, plc_value):
                                    values_to_write.append(plc_value)
                                    indices_to_write.append(elem_index)
                                    self.opcua_value_cache[elem_index] = plc_value
                                    log_debug(f"Array element {elem_index} changed: {plc_value}")
                        continue

                    # Handle scalar value
                    plc_value = convert_value_for_plc(var_node.datatype, actual_value)

                    # Check if value has changed
                    if self._has_value_changed(var_index, plc_value):
                        values_to_write.append(plc_value)
                        indices_to_write.append(var_index)

                        # Update cache
                        self.opcua_value_cache[var_index] = plc_value
                        log_debug(f"Variable {var_index} changed: {plc_value}")

                except Exception as e:
                    log_error(f"Error reading OPC-UA variable {var_index}: {e}")
                    continue

            # Batch write to PLC if we have changed values
            if values_to_write:
                await self._write_to_plc_batch(indices_to_write, values_to_write)

        except Exception as e:
            log_error(f"Error in OPC-UA to runtime sync: {e}")

    async def sync_runtime_to_opcua(self) -> None:
        """
        Synchronize values from PLC runtime to OPC-UA nodes.

        Uses direct memory access when available, falls back to batch operations.
        """
        try:
            if not self.variable_nodes:
                return

            if self._direct_memory_access_enabled and self.variable_metadata:
                await self._update_via_direct_memory_access()
            else:
                await self._update_via_batch_operations()

        except Exception as e:
            log_error(f"Error in runtime to OPC-UA sync: {e}")

    async def _update_via_direct_memory_access(self) -> None:
        """
        Update OPC-UA nodes using direct memory access.

        This is the optimized path - zero C calls per variable.
        """
        for var_index, metadata in self.variable_metadata.items():
            try:
                # Direct memory read
                value = read_memory_direct(metadata.address, metadata.size)

                var_node = self.variable_nodes.get(var_index)
                if var_node:
                    await self._update_opcua_node(var_node, value)

            except Exception as e:
                log_error(f"Direct memory access failed for var {var_index}: {e}")

    async def _update_via_batch_operations(self) -> None:
        """
        Update OPC-UA nodes using batch operations.

        Fallback when direct memory access is not available.
        """
        var_indices = list(self.variable_nodes.keys())

        # Single batch call for all values
        results, msg = self.buffer_accessor.get_var_values_batch(var_indices)

        if msg != "Success":
            log_error(f"Batch read failed: {msg}")
            return

        # Process results
        for i, (value, var_msg) in enumerate(results):
            var_index = var_indices[i]
            var_node = self.variable_nodes.get(var_index)

            if var_msg == "Success" and value is not None and var_node:
                await self._update_opcua_node(var_node, value)

    async def _update_opcua_node(self, var_node: VariableNode, value: Any) -> None:
        """
        Update an OPC-UA node with a new value.

        Uses write_attribute_value() with DataValue for optimal subscription support.
        This approach:
        - Triggers data change notifications for subscribed clients
        - Includes SourceTimestamp (PLC cycle time) and ServerTimestamp
        - Bypasses PreWrite callbacks (server-internal operation)
        - Is faster than write_value() for server-side updates

        Args:
            var_node: The VariableNode to update
            value: Raw value from PLC memory (single value, not used for arrays)
        """
        try:
            # Check if this is an array node
            if var_node.array_length and var_node.array_length > 0:
                await self._update_array_node(var_node)
                return

            # Convert to OPC-UA format
            opcua_value = convert_value_for_opcua(var_node.datatype, value)

            # Get expected OPC-UA type
            expected_type = map_plc_to_opcua_type(var_node.datatype)

            # Create Variant with explicit type
            variant = ua.Variant(opcua_value, expected_type)

            # Create DataValue with timestamps for subscription notifications
            # SourceTimestamp: When the value was read from PLC (cycle time)
            # ServerTimestamp: When the server processed it (now)
            data_value = ua.DataValue(
                Value=variant,
                StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
                SourceTimestamp=self._cycle_timestamp,
                ServerTimestamp=datetime.now(timezone.utc)
            )

            # Use write_attribute_value for optimal subscription triggering
            # This is faster than write_value and properly triggers notifications
            if self.server:
                await self.server.write_attribute_value(
                    var_node.node.nodeid,
                    data_value
                )
            else:
                # Fallback to write_value if no server reference
                await var_node.node.write_value(variant)

        except Exception as e:
            log_error(f"Failed to update OPC-UA node {var_node.debug_var_index}: {e}")

    async def _update_array_node(self, var_node: VariableNode) -> None:
        """
        Update an OPC-UA array node by reading all elements from PLC memory.

        Arrays in PLC have consecutive indices starting from debug_var_index.
        Uses DataValue with timestamps for subscription support.

        Args:
            var_node: The VariableNode representing the array
        """
        try:
            base_index = var_node.debug_var_index
            array_length = var_node.array_length

            # Read all array elements from PLC
            element_indices = list(range(base_index, base_index + array_length))

            # Try direct memory access first for array elements
            array_values = []
            if self._direct_memory_access_enabled:
                for idx in element_indices:
                    metadata = self.variable_metadata.get(idx)
                    if metadata:
                        raw_value = read_memory_direct(metadata.address, metadata.size)
                        opcua_value = convert_value_for_opcua(var_node.datatype, raw_value)
                        array_values.append(opcua_value)
                    else:
                        # Fallback: read via buffer accessor
                        val, msg = self.buffer_accessor.get_var_value(idx)
                        if msg == "Success" and val is not None:
                            opcua_value = convert_value_for_opcua(var_node.datatype, val)
                            array_values.append(opcua_value)
                        else:
                            # Use default value
                            array_values.append(self._get_default_value(var_node.datatype))
            else:
                # Use batch operation
                results, batch_msg = self.buffer_accessor.get_var_values_batch(element_indices)
                for val, msg in results:
                    if msg == "Success" and val is not None:
                        opcua_value = convert_value_for_opcua(var_node.datatype, val)
                        array_values.append(opcua_value)
                    else:
                        array_values.append(self._get_default_value(var_node.datatype))

            # Get expected OPC-UA type
            expected_type = map_plc_to_opcua_type(var_node.datatype)

            # Create array Variant
            variant = ua.Variant(array_values, expected_type)

            # Create DataValue with timestamps for subscription notifications
            data_value = ua.DataValue(
                Value=variant,
                StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
                SourceTimestamp=self._cycle_timestamp,
                ServerTimestamp=datetime.now(timezone.utc)
            )

            # Use write_attribute_value for subscription support
            if self.server:
                await self.server.write_attribute_value(
                    var_node.node.nodeid,
                    data_value
                )
            else:
                # Fallback to write_value if no server reference
                await var_node.node.write_value(variant)

        except Exception as e:
            log_error(f"Failed to update array node {var_node.debug_var_index}: {e}")

    def _get_default_value(self, datatype: str) -> Any:
        """Get default value for a datatype."""
        dtype = datatype.upper()
        if dtype == "BOOL":
            return False
        elif dtype in ["FLOAT", "REAL"]:
            return 0.0
        elif dtype == "STRING":
            return ""
        else:
            return 0

    async def _write_to_plc_batch(
        self,
        indices: list,
        values: list
    ) -> None:
        """
        Write values to PLC using batch operation.

        Args:
            indices: List of variable indices
            values: List of values to write
        """
        try:
            # Combine into tuples as expected by the API
            index_value_pairs = list(zip(indices, values))
            results, msg = self.buffer_accessor.set_var_values_batch(index_value_pairs)

            if msg not in ["Success", "Batch write completed"]:
                log_error(f"Batch write to PLC failed: {msg}")
                return

            # Check individual results
            failed_count = sum(1 for success, _ in results if not success)
            if failed_count > 0:
                log_error(f"Batch write: {failed_count}/{len(results)} failures")
            else:
                log_debug(f"Successfully wrote {len(results)} values to PLC")

        except Exception as e:
            log_error(f"Error in batch write: {e}")

    def _has_value_changed(self, var_index: int, new_value: Any) -> bool:
        """
        Check if a value has changed compared to cached value.

        Args:
            var_index: Variable index
            new_value: New value to compare

        Returns:
            True if value has changed
        """
        if var_index not in self.opcua_value_cache:
            return True

        cached_value = self.opcua_value_cache[var_index]

        # Float comparison with tolerance
        if isinstance(new_value, float) and isinstance(cached_value, float):
            return abs(new_value - cached_value) > 1e-6

        # Exact comparison for other types
        return new_value != cached_value

    def _extract_opcua_value(self, opcua_value: Any) -> Any:
        """
        Extract actual value from OPC-UA response.

        Args:
            opcua_value: Value from OPC-UA node read

        Returns:
            Extracted value or None on error
        """
        try:
            # If it's a DataValue with Value attribute, extract it
            if hasattr(opcua_value, "Value"):
                return opcua_value.Value

            # Already a plain value
            return opcua_value

        except Exception as e:
            log_error(f"Failed to extract OPC-UA value: {e}")
            return None
