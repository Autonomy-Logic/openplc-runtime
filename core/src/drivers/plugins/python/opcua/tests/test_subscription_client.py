#!/usr/bin/env python3
"""
OPC-UA Subscription Test Client.

This script tests subscription functionality by connecting to the OpenPLC
OPC-UA server and subscribing to data changes.

Usage:
    python test_subscription_client.py [endpoint_url]

Default endpoint: opc.tcp://localhost:4840/openplc/opcua
"""

import asyncio
import sys
from datetime import datetime

from asyncua import Client, ua


class SubscriptionHandler:
    """
    Handler for subscription notifications.

    This class receives callbacks when subscribed values change.
    """

    def __init__(self):
        self.notification_count = 0
        self.last_notification_time = None

    def datachange_notification(self, node, val, data):
        """
        Called when a subscribed data value changes.

        Args:
            node: The Node that changed
            val: The new value
            data: DataChangeNotification with full details
        """
        self.notification_count += 1
        self.last_notification_time = datetime.now()

        # Extract source timestamp if available
        source_ts = None
        if hasattr(data, 'monitored_item') and data.monitored_item:
            if hasattr(data.monitored_item, 'Value') and data.monitored_item.Value:
                source_ts = data.monitored_item.Value.SourceTimestamp

        print(f"[{self.last_notification_time.strftime('%H:%M:%S.%f')[:-3]}] "
              f"Data Change #{self.notification_count}")
        print(f"  Node: {node}")
        print(f"  Value: {val}")
        if source_ts:
            print(f"  Source Timestamp: {source_ts}")
        print()

    def event_notification(self, event):
        """Called when an event is received."""
        print(f"Event received: {event}")


async def test_subscriptions(endpoint_url: str):
    """
    Test OPC-UA subscriptions.

    Args:
        endpoint_url: The server endpoint URL
    """
    print(f"Connecting to: {endpoint_url}")
    print("-" * 60)

    client = Client(url=endpoint_url)

    try:
        await client.connect()
        print("Connected successfully!")
        print()

        # Get namespace index for our server
        namespace_uri = "urn:openplc:opcua:datatype:test"
        try:
            ns_idx = await client.get_namespace_index(namespace_uri)
            print(f"Found namespace '{namespace_uri}' at index {ns_idx}")
        except Exception as e:
            print(f"Could not find namespace, using index 2: {e}")
            ns_idx = 2

        # Browse for available nodes
        print()
        print("Browsing root node for objects...")
        root = client.get_root_node()
        objects = await root.get_children()

        for obj in objects:
            name = await obj.read_browse_name()
            print(f"  {name}")

        # Try to find some test variables to subscribe to
        print()
        print("Looking for test variables...")

        test_nodes = []

        # Try to find some common variable names
        variable_names = [
            "test_bool",
            "test_int",
            "test_real",
            "temperature",
            "pressure",
            "motor_speed",
        ]

        objects_node = client.get_objects_node()

        for var_name in variable_names:
            try:
                # Try to browse for the variable
                node_id = f"ns={ns_idx};s={var_name}"
                node = client.get_node(node_id)
                value = await node.read_value()
                print(f"  Found: {var_name} = {value}")
                test_nodes.append(node)
            except Exception:
                pass

        if not test_nodes:
            # Browse objects folder to find any variables
            print("  No named variables found, browsing for any variables...")
            try:
                children = await objects_node.get_children()
                for child in children[:10]:  # Limit to first 10
                    try:
                        node_class = await child.read_node_class()
                        if node_class == ua.NodeClass.Variable:
                            name = await child.read_browse_name()
                            value = await child.read_value()
                            print(f"  Found variable: {name.Name} = {value}")
                            test_nodes.append(child)
                    except Exception:
                        pass
            except Exception as e:
                print(f"  Browse failed: {e}")

        if not test_nodes:
            print()
            print("ERROR: No variables found to subscribe to!")
            print("Make sure the OPC-UA server is running and has variables configured.")
            return

        # Create subscription
        print()
        print("=" * 60)
        print("Creating subscription...")

        handler = SubscriptionHandler()

        # Create subscription with 100ms publishing interval
        subscription = await client.create_subscription(
            period=100,  # Publishing interval in ms
            handler=handler
        )

        print(f"Subscription created with publishing interval: 100ms")

        # Subscribe to found nodes
        handles = []
        for node in test_nodes:
            try:
                handle = await subscription.subscribe_data_change(node)
                handles.append(handle)
                name = await node.read_browse_name()
                print(f"  Subscribed to: {name.Name}")
            except Exception as e:
                print(f"  Failed to subscribe to node: {e}")

        if not handles:
            print("ERROR: Could not subscribe to any variables!")
            return

        print()
        print("=" * 60)
        print("Waiting for data changes...")
        print("(Change PLC values or wait for sync loop updates)")
        print("Press Ctrl+C to stop")
        print("=" * 60)
        print()

        # Wait and monitor for changes
        start_time = datetime.now()
        last_count = 0

        try:
            while True:
                await asyncio.sleep(1)

                # Print status every 5 seconds if no notifications
                elapsed = (datetime.now() - start_time).total_seconds()
                if handler.notification_count == last_count and int(elapsed) % 5 == 0:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                          f"Waiting... ({handler.notification_count} notifications so far)")

                last_count = handler.notification_count

        except asyncio.CancelledError:
            pass

    except KeyboardInterrupt:
        print()
        print("=" * 60)
        print("Test stopped by user")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Cleanup
        print()
        print("Disconnecting...")
        try:
            await client.disconnect()
            print("Disconnected")
        except Exception:
            pass

        # Print summary
        if 'handler' in dir():
            print()
            print("=" * 60)
            print("SUMMARY")
            print("=" * 60)
            print(f"Total notifications received: {handler.notification_count}")
            if handler.last_notification_time:
                print(f"Last notification at: {handler.last_notification_time}")


async def main():
    """Main entry point."""
    # Default endpoint
    endpoint_url = "opc.tcp://localhost:4840/openplc/opcua"

    # Allow override via command line
    if len(sys.argv) > 1:
        endpoint_url = sys.argv[1]

    await test_subscriptions(endpoint_url)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
