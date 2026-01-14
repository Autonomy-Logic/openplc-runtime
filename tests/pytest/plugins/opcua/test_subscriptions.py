"""
Unit tests for OPC-UA subscription functionality.

Tests:
- Subscription creation and deletion
- Data change notifications
- Timestamp handling for subscriptions
- SynchronizationManager subscription support
"""

import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch
import sys
from pathlib import Path

# Add plugin paths
_test_dir = Path(__file__).parent
_plugin_dir = _test_dir.parent.parent.parent.parent / "core" / "src" / "drivers" / "plugins" / "python"
_opcua_dir = _plugin_dir / "opcua"

sys.path.insert(0, str(_plugin_dir))
sys.path.insert(0, str(_opcua_dir))

from asyncua import ua


class MockNode:
    """Mock OPC-UA node for testing."""

    def __init__(self, nodeid="ns=2;i=1", value=0):
        self.nodeid = nodeid
        self._value = value
        self._datavalue = None

    async def read_value(self):
        return self._value

    async def write_value(self, value):
        self._value = value

    def set_value(self, value):
        self._value = value


class MockServer:
    """Mock OPC-UA server for testing subscription features."""

    def __init__(self):
        self.written_values = []
        self.write_attribute_calls = []

    async def write_attribute_value(self, nodeid, datavalue):
        """Record write_attribute_value calls for verification."""
        self.write_attribute_calls.append({
            "nodeid": nodeid,
            "datavalue": datavalue,
            "timestamp": datetime.now(timezone.utc)
        })


class MockVariableNode:
    """Mock VariableNode for testing."""

    def __init__(self, index, datatype="INT", access_mode="readonly", array_length=None):
        self.debug_var_index = index
        self.datatype = datatype
        self.access_mode = access_mode
        self.array_length = array_length
        self.node = MockNode(nodeid=f"ns=2;i={index}", value=0)


class MockBufferAccess:
    """Mock SafeBufferAccess for testing."""

    def __init__(self):
        self._values = {}

    def get_var_value(self, index):
        return (self._values.get(index, 0), "Success")

    def get_var_values_batch(self, indices):
        """Returns (results, msg) where results is list of (value, message) tuples."""
        results = []
        for idx in indices:
            val = self._values.get(idx, 0)
            results.append((val, "Success"))
        return (results, "Success")

    def set_var_values_batch(self, pairs):
        results = []
        for idx, val in pairs:
            self._values[idx] = val
            results.append((True, "Success"))
        return (results, "Batch write completed")

    def set_value(self, index, value):
        self._values[index] = value


# ============================================================================
# Unit Tests for Subscription Support
# ============================================================================

class TestDataValueTimestamps:
    """Test that DataValue timestamps are properly set for subscriptions."""

    @pytest.mark.asyncio
    async def test_datavalue_has_source_timestamp(self):
        """Verify DataValue includes SourceTimestamp from PLC cycle."""
        from synchronization import SynchronizationManager

        mock_server = MockServer()
        mock_buffer = MockBufferAccess()
        mock_buffer.set_value(0, 42)

        var_nodes = {
            0: MockVariableNode(0, "INT", "readonly")
        }

        sync_mgr = SynchronizationManager(mock_buffer, var_nodes, mock_server)
        await sync_mgr.initialize()

        # Set cycle timestamp
        sync_mgr._cycle_timestamp = datetime.now(timezone.utc)

        # Trigger sync
        await sync_mgr.sync_runtime_to_opcua()

        # Verify write_attribute_value was called with DataValue
        assert len(mock_server.write_attribute_calls) == 1
        call = mock_server.write_attribute_calls[0]
        datavalue = call["datavalue"]

        assert datavalue.SourceTimestamp is not None
        assert datavalue.ServerTimestamp is not None

    @pytest.mark.asyncio
    async def test_datavalue_has_good_status(self):
        """Verify DataValue has Good status code."""
        from synchronization import SynchronizationManager

        mock_server = MockServer()
        mock_buffer = MockBufferAccess()
        mock_buffer.set_value(0, 100)

        var_nodes = {
            0: MockVariableNode(0, "INT", "readonly")
        }

        sync_mgr = SynchronizationManager(mock_buffer, var_nodes, mock_server)
        await sync_mgr.initialize()
        sync_mgr._cycle_timestamp = datetime.now(timezone.utc)

        await sync_mgr.sync_runtime_to_opcua()

        call = mock_server.write_attribute_calls[0]
        datavalue = call["datavalue"]

        assert datavalue.StatusCode_.value == ua.StatusCodes.Good


class TestWriteAttributeValue:
    """Test that write_attribute_value is used for subscription notifications."""

    @pytest.mark.asyncio
    async def test_uses_write_attribute_value_with_server(self):
        """Verify write_attribute_value is called when server is provided."""
        from synchronization import SynchronizationManager

        mock_server = MockServer()
        mock_buffer = MockBufferAccess()
        mock_buffer.set_value(0, 50)

        var_nodes = {
            0: MockVariableNode(0, "INT", "readonly")
        }

        sync_mgr = SynchronizationManager(mock_buffer, var_nodes, mock_server)
        await sync_mgr.initialize()
        sync_mgr._cycle_timestamp = datetime.now(timezone.utc)

        await sync_mgr.sync_runtime_to_opcua()

        # Should use write_attribute_value
        assert len(mock_server.write_attribute_calls) == 1

    @pytest.mark.asyncio
    async def test_fallback_to_write_value_without_server(self):
        """Verify fallback to write_value when no server provided."""
        from synchronization import SynchronizationManager

        mock_buffer = MockBufferAccess()
        mock_buffer.set_value(0, 75)

        mock_node = MockNode()
        var_node = MockVariableNode(0, "INT", "readonly")
        var_node.node = mock_node

        var_nodes = {0: var_node}

        # No server provided
        sync_mgr = SynchronizationManager(mock_buffer, var_nodes, None)
        await sync_mgr.initialize()
        sync_mgr._cycle_timestamp = datetime.now(timezone.utc)

        await sync_mgr.sync_runtime_to_opcua()

        # Value should be written via write_value fallback
        # (MockNode stores value directly)


class TestArraySubscriptionSupport:
    """Test subscription support for array variables."""

    @pytest.mark.asyncio
    async def test_array_uses_write_attribute_value(self):
        """Verify arrays also use write_attribute_value for subscriptions."""
        from synchronization import SynchronizationManager

        mock_server = MockServer()
        mock_buffer = MockBufferAccess()

        # Set array element values
        for i in range(5):
            mock_buffer.set_value(10 + i, i * 10)

        var_nodes = {
            10: MockVariableNode(10, "INT", "readonly", array_length=5)
        }

        sync_mgr = SynchronizationManager(mock_buffer, var_nodes, mock_server)
        await sync_mgr.initialize()
        sync_mgr._cycle_timestamp = datetime.now(timezone.utc)

        await sync_mgr.sync_runtime_to_opcua()

        # Should have one call for the array
        assert len(mock_server.write_attribute_calls) == 1

        call = mock_server.write_attribute_calls[0]
        datavalue = call["datavalue"]

        # Array should have proper timestamps
        assert datavalue.SourceTimestamp is not None
        assert datavalue.ServerTimestamp is not None


class TestSubscriptionCycleTimestamp:
    """Test cycle timestamp handling for subscription accuracy."""

    @pytest.mark.asyncio
    async def test_cycle_timestamp_updated_each_cycle(self):
        """Verify cycle timestamp is updated for each sync cycle."""
        from synchronization import SynchronizationManager

        mock_server = MockServer()
        mock_buffer = MockBufferAccess()
        mock_buffer.set_value(0, 1)

        var_nodes = {
            0: MockVariableNode(0, "INT", "readonly")
        }

        sync_mgr = SynchronizationManager(mock_buffer, var_nodes, mock_server)
        await sync_mgr.initialize()

        # First cycle
        ts1 = datetime.now(timezone.utc)
        sync_mgr._cycle_timestamp = ts1
        await sync_mgr.sync_runtime_to_opcua()

        call1_ts = mock_server.write_attribute_calls[0]["datavalue"].SourceTimestamp

        # Small delay
        await asyncio.sleep(0.01)

        # Second cycle with new timestamp
        ts2 = datetime.now(timezone.utc)
        sync_mgr._cycle_timestamp = ts2
        await sync_mgr.sync_runtime_to_opcua()

        call2_ts = mock_server.write_attribute_calls[1]["datavalue"].SourceTimestamp

        # Timestamps should be different
        assert call1_ts != call2_ts
        assert call2_ts > call1_ts


class TestMultipleVariableSubscriptions:
    """Test subscription support with multiple variables."""

    @pytest.mark.asyncio
    async def test_multiple_variables_get_notifications(self):
        """Verify all variables trigger subscription notifications."""
        from synchronization import SynchronizationManager

        mock_server = MockServer()
        mock_buffer = MockBufferAccess()

        # Set up multiple variables
        for i in range(5):
            mock_buffer.set_value(i, i * 100)

        var_nodes = {
            i: MockVariableNode(i, "INT", "readonly")
            for i in range(5)
        }

        sync_mgr = SynchronizationManager(mock_buffer, var_nodes, mock_server)
        await sync_mgr.initialize()
        sync_mgr._cycle_timestamp = datetime.now(timezone.utc)

        await sync_mgr.sync_runtime_to_opcua()

        # All 5 variables should have been updated
        assert len(mock_server.write_attribute_calls) == 5

    @pytest.mark.asyncio
    async def test_mixed_readonly_readwrite_variables(self):
        """Verify both readonly and readwrite variables work with subscriptions."""
        from synchronization import SynchronizationManager

        mock_server = MockServer()
        mock_buffer = MockBufferAccess()

        mock_buffer.set_value(0, 10)
        mock_buffer.set_value(1, 20)

        var_nodes = {
            0: MockVariableNode(0, "INT", "readonly"),
            1: MockVariableNode(1, "INT", "readwrite"),
        }

        sync_mgr = SynchronizationManager(mock_buffer, var_nodes, mock_server)
        await sync_mgr.initialize()
        sync_mgr._cycle_timestamp = datetime.now(timezone.utc)

        await sync_mgr.sync_runtime_to_opcua()

        # Both variables should be updated
        assert len(mock_server.write_attribute_calls) == 2


class TestDataTypeSupport:
    """Test subscription support for different data types."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("datatype,value", [
        ("BOOL", True),
        ("BOOL", False),
        ("INT", 12345),
        ("INT", -12345),
        ("DINT", 2147483647),
        ("REAL", 3.14159),
        ("BYTE", 255),
    ])
    async def test_datatype_subscription_notification(self, datatype, value):
        """Verify different data types work with subscription notifications."""
        from synchronization import SynchronizationManager

        mock_server = MockServer()
        mock_buffer = MockBufferAccess()
        mock_buffer.set_value(0, value)

        var_nodes = {
            0: MockVariableNode(0, datatype, "readonly")
        }

        sync_mgr = SynchronizationManager(mock_buffer, var_nodes, mock_server)
        await sync_mgr.initialize()
        sync_mgr._cycle_timestamp = datetime.now(timezone.utc)

        await sync_mgr.sync_runtime_to_opcua()

        assert len(mock_server.write_attribute_calls) == 1
        call = mock_server.write_attribute_calls[0]

        # Verify DataValue structure
        assert call["datavalue"].Value is not None
        assert call["datavalue"].SourceTimestamp is not None
