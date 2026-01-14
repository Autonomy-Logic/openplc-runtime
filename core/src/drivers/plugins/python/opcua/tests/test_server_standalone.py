#!/usr/bin/env python3
"""
Standalone OPC-UA Test Server.

This script runs a minimal OPC-UA server to test subscription functionality
without requiring the full PLC runtime.

Usage:
    python test_server_standalone.py

The server will:
1. Create test variables that change periodically
2. Accept client connections
3. Support subscriptions with data change notifications
"""

import asyncio
import sys
import os
from datetime import datetime, timezone
from typing import Dict, Any

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asyncua import Server, ua


class TestOpcuaServer:
    """
    Standalone test server for subscription verification.
    """

    def __init__(self, endpoint: str = "opc.tcp://0.0.0.0:4840/openplc/opcua"):
        self.endpoint = endpoint
        self.server: Server = None
        self.namespace_idx: int = None
        self.running = False

        # Test variables
        self.test_nodes: Dict[str, Any] = {}
        self.counter = 0

    async def setup(self) -> bool:
        """Initialize the server."""
        try:
            self.server = Server()
            self.server.set_endpoint(self.endpoint)
            self.server.set_server_name("OpenPLC Test Server")

            # Disable security for testing
            self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

            await self.server.init()

            # Register namespace
            self.namespace_idx = await self.server.register_namespace(
                "urn:openplc:opcua:datatype:test"
            )

            print(f"Namespace registered at index {self.namespace_idx}")

            # Create test variables
            await self._create_test_variables()

            return True

        except Exception as e:
            print(f"Setup failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _create_test_variables(self):
        """Create test variables for subscription testing."""
        objects = self.server.get_objects_node()

        # Create a folder for test variables
        test_folder = await objects.add_folder(
            self.namespace_idx,
            "TestVariables"
        )

        # Create various test variables
        test_vars = [
            ("counter", 0, ua.VariantType.Int32),
            ("temperature", 25.0, ua.VariantType.Float),
            ("pressure", 101.325, ua.VariantType.Float),
            ("motor_running", False, ua.VariantType.Boolean),
            ("status_message", "Initializing", ua.VariantType.String),
        ]

        for name, initial_value, var_type in test_vars:
            node = await test_folder.add_variable(
                self.namespace_idx,
                name,
                initial_value,
                var_type
            )
            await node.set_writable()
            self.test_nodes[name] = node
            print(f"  Created variable: {name} = {initial_value}")

        # Create an array variable
        array_node = await test_folder.add_variable(
            self.namespace_idx,
            "sensor_array",
            [0.0, 0.0, 0.0, 0.0],
            ua.VariantType.Float
        )
        await array_node.set_writable()
        self.test_nodes["sensor_array"] = array_node
        print(f"  Created array: sensor_array = [0.0, 0.0, 0.0, 0.0]")

    async def start(self):
        """Start the server."""
        await self.server.start()
        self.running = True
        print(f"\nServer started at: {self.endpoint}")
        print("=" * 60)

    async def update_values(self):
        """
        Periodically update test values to trigger subscriptions.

        This simulates PLC value changes.
        """
        import math
        import random

        while self.running:
            try:
                self.counter += 1
                now = datetime.now(timezone.utc)

                # Update counter
                counter_node = self.test_nodes["counter"]
                data_value = ua.DataValue(
                    Value=ua.Variant(self.counter, ua.VariantType.Int32),
                    StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
                    SourceTimestamp=now,
                    ServerTimestamp=now
                )
                await self.server.write_attribute_value(
                    counter_node.nodeid,
                    data_value
                )

                # Update temperature (sinusoidal + noise)
                temp = 25.0 + 5.0 * math.sin(self.counter / 10.0) + random.uniform(-0.5, 0.5)
                temp_node = self.test_nodes["temperature"]
                data_value = ua.DataValue(
                    Value=ua.Variant(round(temp, 2), ua.VariantType.Float),
                    StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
                    SourceTimestamp=now,
                    ServerTimestamp=now
                )
                await self.server.write_attribute_value(
                    temp_node.nodeid,
                    data_value
                )

                # Update pressure (slowly varying)
                pressure = 101.325 + 0.5 * math.sin(self.counter / 50.0)
                pressure_node = self.test_nodes["pressure"]
                data_value = ua.DataValue(
                    Value=ua.Variant(round(pressure, 3), ua.VariantType.Float),
                    StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
                    SourceTimestamp=now,
                    ServerTimestamp=now
                )
                await self.server.write_attribute_value(
                    pressure_node.nodeid,
                    data_value
                )

                # Toggle motor every 10 cycles
                motor_running = (self.counter // 10) % 2 == 1
                motor_node = self.test_nodes["motor_running"]
                data_value = ua.DataValue(
                    Value=ua.Variant(motor_running, ua.VariantType.Boolean),
                    StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
                    SourceTimestamp=now,
                    ServerTimestamp=now
                )
                await self.server.write_attribute_value(
                    motor_node.nodeid,
                    data_value
                )

                # Update status message periodically
                if self.counter % 5 == 0:
                    status = f"Running - Cycle {self.counter}"
                    status_node = self.test_nodes["status_message"]
                    data_value = ua.DataValue(
                        Value=ua.Variant(status, ua.VariantType.String),
                        StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
                        SourceTimestamp=now,
                        ServerTimestamp=now
                    )
                    await self.server.write_attribute_value(
                        status_node.nodeid,
                        data_value
                    )

                # Update sensor array
                array_values = [
                    round(random.uniform(0, 100), 2),
                    round(random.uniform(0, 100), 2),
                    round(random.uniform(0, 100), 2),
                    round(random.uniform(0, 100), 2),
                ]
                array_node = self.test_nodes["sensor_array"]
                data_value = ua.DataValue(
                    Value=ua.Variant(array_values, ua.VariantType.Float),
                    StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
                    SourceTimestamp=now,
                    ServerTimestamp=now
                )
                await self.server.write_attribute_value(
                    array_node.nodeid,
                    data_value
                )

                # Print status every 10 cycles
                if self.counter % 10 == 0:
                    print(f"[{now.strftime('%H:%M:%S')}] Cycle {self.counter}: "
                          f"temp={temp:.1f}, motor={'ON' if motor_running else 'OFF'}")

                await asyncio.sleep(0.1)  # 100ms cycle time

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error updating values: {e}")
                await asyncio.sleep(1)

    async def stop(self):
        """Stop the server."""
        self.running = False
        if self.server:
            await self.server.stop()
        print("\nServer stopped")

    async def run(self):
        """Main run loop."""
        if not await self.setup():
            return

        await self.start()

        print("\nServer is running. Test variables are being updated every 100ms.")
        print("Connect with a client to test subscriptions.")
        print("Press Ctrl+C to stop.\n")

        # Run the update loop
        try:
            await self.update_values()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()


async def main():
    """Main entry point."""
    print("=" * 60)
    print("OPC-UA Subscription Test Server")
    print("=" * 60)

    server = TestOpcuaServer()

    try:
        await server.run()
    except KeyboardInterrupt:
        print("\nShutdown requested...")
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
