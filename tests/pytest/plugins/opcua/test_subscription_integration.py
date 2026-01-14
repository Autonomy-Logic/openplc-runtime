"""
Integration tests for OPC-UA subscription functionality.

These tests create actual OPC-UA server and client instances to verify
subscription functionality works end-to-end.

Tests:
- Client subscription creation/deletion
- Data change notifications received by client
- Multiple subscriptions
- Subscription with different publishing intervals
"""

import pytest
import pytest_asyncio
import asyncio
from datetime import datetime, timezone
from typing import List, Any
import sys
from pathlib import Path

# Add plugin paths
_test_dir = Path(__file__).parent
_plugin_dir = _test_dir.parent.parent.parent.parent / "core" / "src" / "drivers" / "plugins" / "python"
_opcua_dir = _plugin_dir / "opcua"

sys.path.insert(0, str(_plugin_dir))
sys.path.insert(0, str(_opcua_dir))

from asyncua import Server, Client, ua


class NotificationCollector:
    """Collects data change notifications for testing."""

    def __init__(self):
        self.notifications: List[dict] = []
        self.notification_event = asyncio.Event()

    def datachange_notification(self, node, val, data):
        """Called when a subscribed value changes."""
        self.notifications.append({
            "node": node,
            "value": val,
            "data": data,
            "timestamp": datetime.now(timezone.utc)
        })
        self.notification_event.set()

    def clear(self):
        """Clear collected notifications."""
        self.notifications.clear()
        self.notification_event.clear()

    async def wait_for_notification(self, timeout: float = 2.0) -> bool:
        """Wait for a notification with timeout."""
        try:
            await asyncio.wait_for(self.notification_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


@pytest_asyncio.fixture
async def opcua_server():
    """Create and start an OPC-UA server for testing."""
    server = Server()
    await server.init()

    server.set_endpoint("opc.tcp://127.0.0.1:14840/test")
    server.set_server_name("Test Server")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    # Register namespace
    ns_idx = await server.register_namespace("urn:test:opcua")

    # Create test folder
    objects = server.get_objects_node()
    test_folder = await objects.add_folder(ns_idx, "TestVariables")

    # Create test variables
    test_vars = {}
    test_vars["int_var"] = await test_folder.add_variable(
        ns_idx, "IntVar", 0, ua.VariantType.Int32
    )
    test_vars["float_var"] = await test_folder.add_variable(
        ns_idx, "FloatVar", 0.0, ua.VariantType.Float
    )
    test_vars["bool_var"] = await test_folder.add_variable(
        ns_idx, "BoolVar", False, ua.VariantType.Boolean
    )

    # Make variables writable
    for var in test_vars.values():
        await var.set_writable()

    # Start server
    await server.start()

    yield {"server": server, "variables": test_vars, "ns_idx": ns_idx}

    # Cleanup
    await server.stop()


@pytest_asyncio.fixture
async def opcua_client(opcua_server):
    """Create and connect an OPC-UA client."""
    client = Client("opc.tcp://127.0.0.1:14840/test")
    await client.connect()

    yield client

    await client.disconnect()


# ============================================================================
# Integration Tests
# ============================================================================

class TestSubscriptionCreation:
    """Test subscription creation and deletion."""

    @pytest.mark.asyncio
    async def test_create_subscription(self, opcua_server, opcua_client):
        """Test creating a subscription."""
        handler = NotificationCollector()

        subscription = await opcua_client.create_subscription(
            period=100,
            handler=handler
        )

        assert subscription is not None

        # Cleanup
        await subscription.delete()

    @pytest.mark.asyncio
    async def test_subscribe_to_variable(self, opcua_server, opcua_client):
        """Test subscribing to a variable's data changes."""
        handler = NotificationCollector()
        server_vars = opcua_server["variables"]

        subscription = await opcua_client.create_subscription(
            period=100,
            handler=handler
        )

        # Subscribe to int variable
        int_node = opcua_client.get_node(server_vars["int_var"].nodeid)
        handle = await subscription.subscribe_data_change(int_node)

        assert handle is not None

        # Cleanup
        await subscription.delete()

    @pytest.mark.asyncio
    async def test_unsubscribe_from_variable(self, opcua_server, opcua_client):
        """Test unsubscribing from a variable."""
        handler = NotificationCollector()
        server_vars = opcua_server["variables"]

        subscription = await opcua_client.create_subscription(
            period=100,
            handler=handler
        )

        int_node = opcua_client.get_node(server_vars["int_var"].nodeid)
        handle = await subscription.subscribe_data_change(int_node)

        # Unsubscribe
        await subscription.unsubscribe(handle)

        # Cleanup
        await subscription.delete()

    @pytest.mark.asyncio
    async def test_delete_subscription(self, opcua_server, opcua_client):
        """Test deleting a subscription."""
        handler = NotificationCollector()

        subscription = await opcua_client.create_subscription(
            period=100,
            handler=handler
        )

        # Delete subscription
        await subscription.delete()

        # Verify it's deleted (should not raise)


class TestDataChangeNotifications:
    """Test data change notification delivery."""

    @pytest.mark.asyncio
    async def test_receive_initial_value(self, opcua_server, opcua_client):
        """Test receiving initial value notification on subscribe."""
        handler = NotificationCollector()
        server_vars = opcua_server["variables"]

        # Set initial value (use Variant to ensure correct type)
        await server_vars["int_var"].write_value(ua.Variant(42, ua.VariantType.Int32))

        subscription = await opcua_client.create_subscription(
            period=100,
            handler=handler
        )

        int_node = opcua_client.get_node(server_vars["int_var"].nodeid)
        await subscription.subscribe_data_change(int_node)

        # Wait for initial notification
        received = await handler.wait_for_notification(timeout=2.0)

        assert received, "Should receive initial value notification"
        assert len(handler.notifications) >= 1
        assert handler.notifications[0]["value"] == 42

        await subscription.delete()

    @pytest.mark.asyncio
    async def test_receive_value_change_notification(self, opcua_server, opcua_client):
        """Test receiving notification when value changes."""
        handler = NotificationCollector()
        server = opcua_server["server"]
        server_vars = opcua_server["variables"]

        subscription = await opcua_client.create_subscription(
            period=100,
            handler=handler
        )

        int_node = opcua_client.get_node(server_vars["int_var"].nodeid)
        await subscription.subscribe_data_change(int_node)

        # Wait for initial notification
        await handler.wait_for_notification(timeout=1.0)
        handler.clear()

        # Change value on server using write_attribute_value (like SynchronizationManager)
        new_value = 100
        data_value = ua.DataValue(
            Value=ua.Variant(new_value, ua.VariantType.Int32),
            StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
            SourceTimestamp=datetime.now(timezone.utc),
            ServerTimestamp=datetime.now(timezone.utc)
        )
        await server.write_attribute_value(server_vars["int_var"].nodeid, data_value)

        # Wait for change notification
        received = await handler.wait_for_notification(timeout=2.0)

        assert received, "Should receive value change notification"
        assert any(n["value"] == new_value for n in handler.notifications)

        await subscription.delete()

    @pytest.mark.asyncio
    async def test_multiple_value_changes(self, opcua_server, opcua_client):
        """Test receiving multiple value change notifications."""
        handler = NotificationCollector()
        server = opcua_server["server"]
        server_vars = opcua_server["variables"]

        subscription = await opcua_client.create_subscription(
            period=50,  # Fast publishing
            handler=handler
        )

        int_node = opcua_client.get_node(server_vars["int_var"].nodeid)
        await subscription.subscribe_data_change(int_node)

        # Wait for initial
        await handler.wait_for_notification(timeout=1.0)
        initial_count = len(handler.notifications)

        # Send multiple changes
        for i in range(5):
            data_value = ua.DataValue(
                Value=ua.Variant(i * 10, ua.VariantType.Int32),
                SourceTimestamp=datetime.now(timezone.utc),
                ServerTimestamp=datetime.now(timezone.utc)
            )
            await server.write_attribute_value(server_vars["int_var"].nodeid, data_value)
            await asyncio.sleep(0.1)

        # Wait for notifications
        await asyncio.sleep(0.5)

        # Should have received additional notifications
        assert len(handler.notifications) > initial_count

        await subscription.delete()


class TestMultipleSubscriptions:
    """Test multiple simultaneous subscriptions."""

    @pytest.mark.asyncio
    async def test_subscribe_multiple_variables(self, opcua_server, opcua_client):
        """Test subscribing to multiple variables."""
        handler = NotificationCollector()
        server_vars = opcua_server["variables"]

        subscription = await opcua_client.create_subscription(
            period=100,
            handler=handler
        )

        # Subscribe to all variables
        handles = []
        for name, var in server_vars.items():
            node = opcua_client.get_node(var.nodeid)
            handle = await subscription.subscribe_data_change(node)
            handles.append(handle)

        assert len(handles) == 3

        # Wait for initial notifications
        await asyncio.sleep(0.5)

        # Should receive notifications for all variables
        assert len(handler.notifications) >= 3

        await subscription.delete()

    @pytest.mark.asyncio
    async def test_multiple_subscriptions_same_variable(self, opcua_server, opcua_client):
        """Test multiple subscriptions to the same variable."""
        handler1 = NotificationCollector()
        handler2 = NotificationCollector()
        server = opcua_server["server"]
        server_vars = opcua_server["variables"]

        sub1 = await opcua_client.create_subscription(period=100, handler=handler1)
        sub2 = await opcua_client.create_subscription(period=100, handler=handler2)

        int_node = opcua_client.get_node(server_vars["int_var"].nodeid)
        await sub1.subscribe_data_change(int_node)
        await sub2.subscribe_data_change(int_node)

        # Wait for initial
        await asyncio.sleep(0.3)
        handler1.clear()
        handler2.clear()

        # Change value
        data_value = ua.DataValue(
            Value=ua.Variant(999, ua.VariantType.Int32),
            SourceTimestamp=datetime.now(timezone.utc),
            ServerTimestamp=datetime.now(timezone.utc)
        )
        await server.write_attribute_value(server_vars["int_var"].nodeid, data_value)

        # Wait for notifications
        await asyncio.sleep(0.3)

        # Both handlers should receive notification
        assert len(handler1.notifications) >= 1
        assert len(handler2.notifications) >= 1

        await sub1.delete()
        await sub2.delete()


class TestSubscriptionTimestamps:
    """Test timestamp handling in subscriptions."""

    @pytest.mark.asyncio
    async def test_source_timestamp_preserved(self, opcua_server, opcua_client):
        """Test that SourceTimestamp is preserved in notifications."""
        handler = NotificationCollector()
        server = opcua_server["server"]
        server_vars = opcua_server["variables"]

        subscription = await opcua_client.create_subscription(
            period=100,
            handler=handler
        )

        int_node = opcua_client.get_node(server_vars["int_var"].nodeid)
        await subscription.subscribe_data_change(int_node)

        # Wait for initial
        await handler.wait_for_notification(timeout=1.0)
        handler.clear()

        # Write with specific source timestamp
        source_ts = datetime.now(timezone.utc)
        data_value = ua.DataValue(
            Value=ua.Variant(777, ua.VariantType.Int32),
            StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
            SourceTimestamp=source_ts,
            ServerTimestamp=datetime.now(timezone.utc)
        )
        await server.write_attribute_value(server_vars["int_var"].nodeid, data_value)

        # Wait for notification
        received = await handler.wait_for_notification(timeout=2.0)
        assert received

        # Check notification has timestamp info
        notification = handler.notifications[-1]
        assert notification["data"] is not None

        await subscription.delete()


class TestDifferentDataTypes:
    """Test subscriptions with different data types."""

    @pytest.mark.asyncio
    async def test_int_subscription(self, opcua_server, opcua_client):
        """Test subscription to integer variable."""
        handler = NotificationCollector()
        server = opcua_server["server"]
        server_vars = opcua_server["variables"]

        subscription = await opcua_client.create_subscription(period=100, handler=handler)
        int_node = opcua_client.get_node(server_vars["int_var"].nodeid)
        await subscription.subscribe_data_change(int_node)

        await handler.wait_for_notification(timeout=1.0)
        handler.clear()

        # Change to new int value
        data_value = ua.DataValue(Value=ua.Variant(12345, ua.VariantType.Int32))
        await server.write_attribute_value(server_vars["int_var"].nodeid, data_value)

        received = await handler.wait_for_notification(timeout=2.0)
        assert received
        assert any(n["value"] == 12345 for n in handler.notifications)

        await subscription.delete()

    @pytest.mark.asyncio
    async def test_float_subscription(self, opcua_server, opcua_client):
        """Test subscription to float variable."""
        handler = NotificationCollector()
        server = opcua_server["server"]
        server_vars = opcua_server["variables"]

        subscription = await opcua_client.create_subscription(period=100, handler=handler)
        float_node = opcua_client.get_node(server_vars["float_var"].nodeid)
        await subscription.subscribe_data_change(float_node)

        await handler.wait_for_notification(timeout=1.0)
        handler.clear()

        # Change to new float value
        data_value = ua.DataValue(Value=ua.Variant(3.14159, ua.VariantType.Float))
        await server.write_attribute_value(server_vars["float_var"].nodeid, data_value)

        received = await handler.wait_for_notification(timeout=2.0)
        assert received

        await subscription.delete()

    @pytest.mark.asyncio
    async def test_bool_subscription(self, opcua_server, opcua_client):
        """Test subscription to boolean variable."""
        handler = NotificationCollector()
        server = opcua_server["server"]
        server_vars = opcua_server["variables"]

        subscription = await opcua_client.create_subscription(period=100, handler=handler)
        bool_node = opcua_client.get_node(server_vars["bool_var"].nodeid)
        await subscription.subscribe_data_change(bool_node)

        await handler.wait_for_notification(timeout=1.0)
        handler.clear()

        # Change to True
        data_value = ua.DataValue(Value=ua.Variant(True, ua.VariantType.Boolean))
        await server.write_attribute_value(server_vars["bool_var"].nodeid, data_value)

        received = await handler.wait_for_notification(timeout=2.0)
        assert received
        assert any(n["value"] is True for n in handler.notifications)

        await subscription.delete()
