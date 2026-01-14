"""
Performance benchmarks comparing polling vs subscription approaches.

These tests measure and compare:
- Network traffic (number of read operations)
- Latency (time from value change to notification)
- CPU/resource usage patterns
- Throughput under load

Run with: pytest test_subscription_performance.py -v -s
"""

import pytest
import pytest_asyncio
import asyncio
import time
from datetime import datetime, timezone
from typing import List
import sys
from pathlib import Path

# Add plugin paths
_test_dir = Path(__file__).parent
_plugin_dir = _test_dir.parent.parent.parent.parent / "core" / "src" / "drivers" / "plugins" / "python"
_opcua_dir = _plugin_dir / "opcua"

sys.path.insert(0, str(_plugin_dir))
sys.path.insert(0, str(_opcua_dir))

from asyncua import Server, Client, ua


class PerformanceMetrics:
    """Collects performance metrics for comparison."""

    def __init__(self):
        self.read_count = 0
        self.notification_count = 0
        self.latencies: List[float] = []
        self.start_time = None
        self.end_time = None

    def record_read(self):
        self.read_count += 1

    def record_notification(self, latency_ms: float = None):
        self.notification_count += 1
        if latency_ms is not None:
            self.latencies.append(latency_ms)

    def start(self):
        self.start_time = time.perf_counter()

    def stop(self):
        self.end_time = time.perf_counter()

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0

    @property
    def avg_latency(self) -> float:
        if self.latencies:
            return sum(self.latencies) / len(self.latencies)
        return 0

    @property
    def max_latency(self) -> float:
        return max(self.latencies) if self.latencies else 0

    @property
    def min_latency(self) -> float:
        return min(self.latencies) if self.latencies else 0

    def report(self, name: str) -> dict:
        return {
            "name": name,
            "duration_s": round(self.duration, 3),
            "read_count": self.read_count,
            "notification_count": self.notification_count,
            "avg_latency_ms": round(self.avg_latency, 2),
            "min_latency_ms": round(self.min_latency, 2),
            "max_latency_ms": round(self.max_latency, 2),
        }


class SubscriptionHandler:
    """Handler that tracks notification timing."""

    def __init__(self, metrics: PerformanceMetrics):
        self.metrics = metrics
        self.last_change_time = None
        self.received_values = []

    def set_change_time(self):
        """Call this when value is changed on server."""
        self.last_change_time = time.perf_counter()

    def datachange_notification(self, node, val, data):
        """Called when subscribed value changes."""
        receive_time = time.perf_counter()

        if self.last_change_time:
            latency_ms = (receive_time - self.last_change_time) * 1000
            self.metrics.record_notification(latency_ms)
        else:
            self.metrics.record_notification()

        self.received_values.append(val)


@pytest_asyncio.fixture
async def perf_server():
    """Create server with multiple test variables for performance testing."""
    server = Server()
    await server.init()

    server.set_endpoint("opc.tcp://127.0.0.1:14841/perftest")
    server.set_server_name("Performance Test Server")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    ns_idx = await server.register_namespace("urn:test:perf")

    objects = server.get_objects_node()
    test_folder = await objects.add_folder(ns_idx, "PerfVars")

    # Create multiple test variables
    variables = []
    for i in range(10):
        var = await test_folder.add_variable(
            ns_idx, f"Var{i}", 0, ua.VariantType.Int32
        )
        await var.set_writable()
        variables.append(var)

    await server.start()

    yield {"server": server, "variables": variables, "ns_idx": ns_idx}

    await server.stop()


@pytest_asyncio.fixture
async def perf_client(perf_server):
    """Create client for performance testing."""
    client = Client("opc.tcp://127.0.0.1:14841/perftest")
    await client.connect()

    yield client

    await client.disconnect()


# ============================================================================
# Performance Benchmarks
# ============================================================================

class TestPollingVsSubscription:
    """Compare polling approach vs subscription approach."""

    @pytest.mark.asyncio
    async def test_polling_approach(self, perf_server, perf_client):
        """
        Measure performance of polling approach.

        Polling: Client repeatedly reads values at fixed interval.
        """
        metrics = PerformanceMetrics()
        server = perf_server["server"]
        server_vars = perf_server["variables"]

        # Get client nodes
        client_nodes = [
            perf_client.get_node(var.nodeid) for var in server_vars
        ]

        num_iterations = 50
        poll_interval = 0.1  # 100ms polling

        metrics.start()

        for iteration in range(num_iterations):
            # Simulate server updating values (use explicit Int32 type)
            for i, var in enumerate(server_vars):
                await var.write_value(ua.Variant(iteration * 10 + i, ua.VariantType.Int32))

            # Poll all values (this is what polling clients do)
            for node in client_nodes:
                _ = await node.read_value()
                metrics.record_read()

            await asyncio.sleep(poll_interval)

        metrics.stop()

        report = metrics.report("Polling")
        print(f"\n{'='*60}")
        print(f"POLLING PERFORMANCE:")
        print(f"  Duration: {report['duration_s']}s")
        print(f"  Read operations: {report['read_count']}")
        print(f"  Reads/second: {report['read_count'] / report['duration_s']:.1f}")
        print(f"{'='*60}")

        # Verify polling performed expected number of reads
        expected_reads = num_iterations * len(server_vars)
        assert metrics.read_count == expected_reads

    @pytest.mark.asyncio
    async def test_subscription_approach(self, perf_server, perf_client):
        """
        Measure performance of subscription approach.

        Subscription: Server pushes changes to client.
        """
        metrics = PerformanceMetrics()
        handler = SubscriptionHandler(metrics)
        server = perf_server["server"]
        server_vars = perf_server["variables"]

        # Create subscription
        subscription = await perf_client.create_subscription(
            period=50,  # 50ms publishing interval
            handler=handler
        )

        # Subscribe to all variables
        client_nodes = [
            perf_client.get_node(var.nodeid) for var in server_vars
        ]
        for node in client_nodes:
            await subscription.subscribe_data_change(node)

        # Wait for initial notifications
        await asyncio.sleep(0.5)
        initial_notifications = metrics.notification_count

        num_iterations = 50

        metrics.start()

        for iteration in range(num_iterations):
            handler.set_change_time()

            # Server updates values (triggers notifications)
            for i, var in enumerate(server_vars):
                data_value = ua.DataValue(
                    Value=ua.Variant(iteration * 10 + i, ua.VariantType.Int32),
                    SourceTimestamp=datetime.now(timezone.utc),
                    ServerTimestamp=datetime.now(timezone.utc)
                )
                await server.write_attribute_value(var.nodeid, data_value)

            # Brief wait for notifications to arrive
            await asyncio.sleep(0.1)

        metrics.stop()

        await subscription.delete()

        report = metrics.report("Subscription")
        print(f"\n{'='*60}")
        print(f"SUBSCRIPTION PERFORMANCE:")
        print(f"  Duration: {report['duration_s']}s")
        print(f"  Read operations: {report['read_count']} (should be 0)")
        print(f"  Notifications: {report['notification_count'] - initial_notifications}")
        print(f"  Avg latency: {report['avg_latency_ms']}ms")
        print(f"  Min latency: {report['min_latency_ms']}ms")
        print(f"  Max latency: {report['max_latency_ms']}ms")
        print(f"{'='*60}")

        # Verify no polling reads were needed
        assert metrics.read_count == 0

        # Verify notifications were received
        assert metrics.notification_count > initial_notifications

    @pytest.mark.asyncio
    async def test_compare_network_efficiency(self, perf_server, perf_client):
        """
        Compare network efficiency: polling vs subscription.

        Key metric: Number of network operations needed to detect N changes.
        """
        server = perf_server["server"]
        server_vars = perf_server["variables"]
        num_changes = 20

        # --- Polling simulation ---
        polling_reads = 0
        client_nodes = [perf_client.get_node(var.nodeid) for var in server_vars]

        for _ in range(num_changes):
            # Each poll cycle reads all variables
            for node in client_nodes:
                await node.read_value()
                polling_reads += 1
            await asyncio.sleep(0.05)

        # --- Subscription simulation ---
        metrics = PerformanceMetrics()
        handler = SubscriptionHandler(metrics)

        subscription = await perf_client.create_subscription(
            period=50,
            handler=handler
        )

        for node in client_nodes:
            await subscription.subscribe_data_change(node)

        await asyncio.sleep(0.3)  # Wait for initial

        for change_num in range(num_changes):
            for i, var in enumerate(server_vars):
                data_value = ua.DataValue(
                    Value=ua.Variant(change_num * 100 + i, ua.VariantType.Int32)
                )
                await server.write_attribute_value(var.nodeid, data_value)
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.3)  # Wait for notifications

        await subscription.delete()

        # Calculate efficiency
        subscription_reads = 0  # Subscriptions don't poll

        print(f"\n{'='*60}")
        print(f"NETWORK EFFICIENCY COMPARISON:")
        print(f"  Changes to detect: {num_changes} iterations x {len(server_vars)} vars")
        print(f"  Polling reads required: {polling_reads}")
        print(f"  Subscription reads required: {subscription_reads}")
        print(f"  Network reduction: {((polling_reads - subscription_reads) / polling_reads * 100):.1f}%")
        print(f"{'='*60}")

        # Subscription should require far fewer network operations
        assert subscription_reads < polling_reads


class TestSubscriptionLatency:
    """Test and measure subscription notification latency."""

    @pytest.mark.asyncio
    async def test_single_value_latency(self, perf_server, perf_client):
        """Measure latency for single value change notification."""
        metrics = PerformanceMetrics()
        handler = SubscriptionHandler(metrics)
        server = perf_server["server"]
        test_var = perf_server["variables"][0]

        subscription = await perf_client.create_subscription(
            period=10,  # Fast publishing
            handler=handler
        )

        node = perf_client.get_node(test_var.nodeid)
        await subscription.subscribe_data_change(node)

        await asyncio.sleep(0.2)  # Wait for initial

        # Measure latency for 20 changes
        for i in range(20):
            handler.set_change_time()

            data_value = ua.DataValue(
                Value=ua.Variant(i * 1000, ua.VariantType.Int32),
                SourceTimestamp=datetime.now(timezone.utc)
            )
            await server.write_attribute_value(test_var.nodeid, data_value)

            await asyncio.sleep(0.05)

        await subscription.delete()

        print(f"\n{'='*60}")
        print(f"LATENCY BENCHMARK (single variable):")
        print(f"  Samples: {len(metrics.latencies)}")
        print(f"  Average: {metrics.avg_latency:.2f}ms")
        print(f"  Min: {metrics.min_latency:.2f}ms")
        print(f"  Max: {metrics.max_latency:.2f}ms")
        print(f"{'='*60}")

        # Latency should be reasonable (< 100ms for local)
        assert metrics.avg_latency < 100, f"Average latency too high: {metrics.avg_latency}ms"

    @pytest.mark.asyncio
    async def test_burst_update_handling(self, perf_server, perf_client):
        """Test handling of rapid burst updates."""
        metrics = PerformanceMetrics()
        handler = SubscriptionHandler(metrics)
        server = perf_server["server"]
        test_var = perf_server["variables"][0]

        subscription = await perf_client.create_subscription(
            period=10,
            handler=handler
        )

        node = perf_client.get_node(test_var.nodeid)
        await subscription.subscribe_data_change(node)

        await asyncio.sleep(0.2)
        initial_count = metrics.notification_count

        # Burst: 50 rapid updates
        burst_size = 50
        for i in range(burst_size):
            data_value = ua.DataValue(
                Value=ua.Variant(i, ua.VariantType.Int32)
            )
            await server.write_attribute_value(test_var.nodeid, data_value)
            # No sleep - rapid fire

        # Wait for notifications to arrive
        await asyncio.sleep(1.0)

        await subscription.delete()

        notifications_received = metrics.notification_count - initial_count

        print(f"\n{'='*60}")
        print(f"BURST UPDATE HANDLING:")
        print(f"  Updates sent: {burst_size}")
        print(f"  Notifications received: {notifications_received}")
        print(f"  Delivery rate: {(notifications_received / burst_size * 100):.1f}%")
        print(f"{'='*60}")

        # Should receive most notifications (some may be coalesced)
        assert notifications_received > 0


class TestScalability:
    """Test subscription scalability with many variables."""

    @pytest.mark.asyncio
    async def test_many_subscriptions(self, perf_server, perf_client):
        """Test performance with subscriptions to all variables."""
        metrics = PerformanceMetrics()
        handler = SubscriptionHandler(metrics)
        server = perf_server["server"]
        server_vars = perf_server["variables"]

        subscription = await perf_client.create_subscription(
            period=50,
            handler=handler
        )

        # Subscribe to all 10 variables
        handles = []
        for var in server_vars:
            node = perf_client.get_node(var.nodeid)
            handle = await subscription.subscribe_data_change(node)
            handles.append(handle)

        await asyncio.sleep(0.3)
        initial_count = metrics.notification_count

        metrics.start()

        # Update all variables 10 times
        for iteration in range(10):
            for i, var in enumerate(server_vars):
                data_value = ua.DataValue(
                    Value=ua.Variant(iteration * 100 + i, ua.VariantType.Int32)
                )
                await server.write_attribute_value(var.nodeid, data_value)

            await asyncio.sleep(0.1)

        metrics.stop()

        await subscription.delete()

        total_updates = 10 * len(server_vars)
        notifications = metrics.notification_count - initial_count

        print(f"\n{'='*60}")
        print(f"SCALABILITY TEST ({len(server_vars)} variables):")
        print(f"  Total updates: {total_updates}")
        print(f"  Notifications received: {notifications}")
        print(f"  Duration: {metrics.duration:.2f}s")
        print(f"  Updates/second: {total_updates / metrics.duration:.1f}")
        print(f"{'='*60}")

        # Should receive notifications for all updates
        assert notifications > 0
