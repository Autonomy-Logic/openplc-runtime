# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

import asyncio
import contextlib
import gc
import socket
import threading
import time
from typing import List, Optional, Tuple

import pytest
from asyncua import Client, ua
from asyncua.ua.uaerrors import UaStatusCodeError

import plugin

_OpcuaServerManager = plugin.OpcuaServerManager

GRACE_PERIOD_S = 2.0
OVERRUN_BOUND_S = 6.0
FORCED_CANCEL_LINE = "Graceful stop did not complete in 2s"
SERVER_THREAD_NAME = "opcua-server"
REQUEST_TIMEOUT_S = 1.0
STARTUP_TIMEOUT_S = 15.0
SESSION_CLOSE_TIMEOUT_S = 2.0 * REQUEST_TIMEOUT_S


def _server_threads() -> List[threading.Thread]:
    """Live threads carrying the OPC-UA server thread name."""
    return [t for t in threading.enumerate() if t.name == SERVER_THREAD_NAME and t.is_alive()]


def _port_refuses_connections(port: int) -> bool:
    """True when a TCP connect to the port on the loopback address is refused."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return False
    except ConnectionRefusedError:
        return True
    except socket.timeout:
        return False


def _wait_for_listener(port: int, timeout: float) -> bool:
    """Poll the port until it accepts a TCP connection or the timeout ends."""
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while time.monotonic() < deadline:
        if not _port_refuses_connections(port):
            return True
        pause.wait(0.02)
    return False


def _close_leaked_listeners() -> None:
    """Close asyncio listening servers a stop left behind, so the next test can bind the port."""
    gc.collect()
    for obj in gc.get_objects():
        if isinstance(obj, asyncio.base_events.Server):
            with contextlib.suppress(Exception):
                obj.close()


def _forced_cancel_logged(lines: List[str]) -> bool:
    """True when the OPC-UA log holds the forced-cancellation line of stop_loop()."""
    return any(FORCED_CANCEL_LINE in line for line in list(lines))


def _closed_by_server(exc: BaseException) -> bool:
    """True when a failed request shows the server closed the connection or session."""
    seen: Optional[BaseException] = exc
    while seen is not None:
        if isinstance(seen, (ConnectionError, UaStatusCodeError)):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


async def _serve_ignoring_cancellation(serve, release: threading.Event) -> None:
    """Run the real server under a task that swallows every cancellation until released."""
    inner = asyncio.ensure_future(serve)
    while not release.is_set():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            continue
    inner.cancel()
    await asyncio.gather(inner, return_exceptions=True)


def _server_manager_class(
    created: List[_OpcuaServerManager],
    release: threading.Event,
    start_delay: float,
    ignore_cancellation: bool,
):
    """Build an OpcuaServerManager subclass that records each instance and can delay its
    start or ignore cancellation."""

    class _TestServerManager(_OpcuaServerManager):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        async def run(self) -> None:
            if start_delay:
                await asyncio.sleep(start_delay)
            if ignore_cancellation:
                await _serve_ignoring_cancellation(super().run(), release)
            else:
                await super().run()

    return _TestServerManager


class _IgnoreNotifications:
    """Subscription handler that accepts data changes and does nothing with them."""

    def datachange_notification(self, node, val, data) -> None:
        return None


class _ClientSwarm:
    """Anonymous asyncua sessions on their own event loop, each writing "setpoint" periodically."""

    def __init__(
        self, server, count: int, write_period: float, subscribe: bool, verify_read_back: bool
    ):
        self._server = server
        self._count = count
        self._write_period = write_period
        self._subscribe = subscribe
        self._verify_read_back = verify_read_back
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="opcua-test-clients", daemon=True
        )
        self._clients: List[Client] = []
        self._sessions = []
        self._ready = [threading.Event() for _ in range(count)]
        self._closed = [threading.Event() for _ in range(count)]
        self._failures: List[List[str]] = [[] for _ in range(count)]
        self._writes = [0] * count
        self._progress = threading.Condition()
        self._closing = False

    def start(self) -> None:
        """Connect every session; return once each wrote "setpoint" twice without a failure."""
        self._thread.start()
        self._sessions = [
            asyncio.run_coroutine_threadsafe(self._session(index), self._loop)
            for index in range(self._count)
        ]
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        for index, ready in enumerate(self._ready):
            if not ready.wait(max(0.0, deadline - time.monotonic())):
                raise AssertionError(f"client {index} never wrote setpoint: {self._report()}")
        assert not any(self._failures), f"a client failed before the stop: {self._report()}"

    def wait_for_writes(self, extra: int, timeout: float) -> bool:
        """Wait until the sessions completed `extra` more successful writes."""
        with self._progress:
            target = sum(self._writes) + extra
            return self._progress.wait_for(lambda: sum(self._writes) >= target, timeout)

    def wait_all_closed(self, timeout: float) -> bool:
        """Wait until every session saw the server close its connection or session."""
        deadline = time.monotonic() + timeout
        return all(closed.wait(max(0.0, deadline - time.monotonic())) for closed in self._closed)

    def close(self) -> None:
        self._closing = True
        if not self._thread.is_alive():
            return
        asyncio.run_coroutine_threadsafe(self._cancel_everything(), self._loop).result(10.0)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10.0)
        if not self._thread.is_alive():
            self._loop.close()

    def _report(self) -> str:
        errors = []
        for session in self._sessions:
            if session.done() and not session.cancelled() and session.exception() is not None:
                errors.append(repr(session.exception()))
        return f"failures={self._failures} session_errors={errors}"

    async def _connect(self) -> Client:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_TIMEOUT_S
        while True:
            client = Client(self._server.ENDPOINT, timeout=REQUEST_TIMEOUT_S)
            try:
                await client.connect()
                return client
            except (OSError, asyncio.TimeoutError):
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.05)

    async def _session(self, index: int) -> None:
        client = await self._connect()
        self._clients.append(client)
        namespace = await client.get_namespace_index(self._server.NAMESPACE_URI)
        node = client.get_node(ua.NodeId(self._server.NODE_ID, namespace))
        if self._subscribe:
            subscription = await client.create_subscription(100, _IgnoreNotifications())
            await subscription.subscribe_data_change(node)
        value = index * 1_000_000
        while not self._closing:
            value += 1
            try:
                await node.write_value(ua.Variant(value, ua.VariantType.Int32))
                if self._verify_read_back:
                    read_back = await node.read_value()
                    if read_back != value:
                        raise AssertionError(f"wrote {value} to setpoint, read back {read_back}")
            except Exception as exc:
                if self._closing:
                    return
                self._failures[index].append(repr(exc))
                if _closed_by_server(exc):
                    self._closed[index].set()
                    return
                continue
            with self._progress:
                self._writes[index] += 1
                self._progress.notify_all()
            if self._writes[index] >= 2:
                self._ready[index].set()
            await asyncio.sleep(self._write_period)

    async def _cancel_everything(self) -> None:
        for client in self._clients:
            with contextlib.suppress(Exception):
                client.disconnect_socket()
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class _PluginHarness:
    """Drives the OPC-UA plugin entry points against the stub runtime and records the
    server managers start_loop() builds."""

    def __init__(self, monkeypatch, server, runtime_args, accessor_factory):
        self.server = server
        self.runtime_args = runtime_args
        self.managers: List[_OpcuaServerManager] = []
        self.release = threading.Event()
        self._monkeypatch = monkeypatch
        self._swarms: List[_ClientSwarm] = []
        monkeypatch.setattr(plugin, "_runtime_args", runtime_args)
        monkeypatch.setattr(plugin, "SafeBufferAccess", accessor_factory)
        self.use_server()

    def use_server(self, start_delay: float = 0.0, ignore_cancellation: bool = False) -> None:
        self._monkeypatch.setattr(
            plugin,
            "OpcuaServerManager",
            _server_manager_class(self.managers, self.release, start_delay, ignore_cancellation),
        )

    def start(self) -> None:
        assert plugin.start_loop() is True

    def clients(
        self,
        count: int,
        write_period: float,
        subscribe: bool = False,
        verify_read_back: bool = False,
    ) -> _ClientSwarm:
        swarm = _ClientSwarm(self.server, count, write_period, subscribe, verify_read_back)
        self._swarms.append(swarm)
        swarm.start()
        return swarm

    def timed_stop(self) -> Tuple[bool, float]:
        began = time.monotonic()
        result = plugin.stop_loop()
        return result, time.monotonic() - began

    def latest_accessor(self):
        return self.managers[-1].buffer_accessor

    def shut_down(self) -> None:
        self.release.set()
        if _server_threads():
            plugin.stop_loop()
        for thread in _server_threads():
            thread.join(timeout=10.0)
        for swarm in self._swarms:
            swarm.close()
        self.managers.clear()
        self._monkeypatch.setattr(plugin, "_server_manager", None, raising=False)
        _close_leaked_listeners()


@pytest.fixture
def opcua_plugin(
    monkeypatch,
    anonymous_operator_server,
    stub_runtime_args,
    mock_buffer_access_factory,
    opcua_log_lines,
):
    harness = _PluginHarness(
        monkeypatch, anonymous_operator_server, stub_runtime_args, mock_buffer_access_factory
    )
    yield harness
    harness.shut_down()


def test_clients_holding_sessions_open_cannot_stretch_the_stop(opcua_plugin):
    opcua_plugin.start()
    clients = opcua_plugin.clients(count=5, write_period=0.1, subscribe=True)
    assert _server_threads(), "no thread named opcua-server is running before the stop"

    result, elapsed = opcua_plugin.timed_stop()

    assert result is True
    assert elapsed < GRACE_PERIOD_S, f"stop_loop() took {elapsed:.2f}s"
    sessions_closed = clients.wait_all_closed(SESSION_CLOSE_TIMEOUT_S)
    assert sessions_closed, "a client session is still open after the stop"
    assert _port_refuses_connections(opcua_plugin.server.PORT)
    assert not _server_threads()


def test_a_stop_with_a_writing_client_never_forces_cancellation(opcua_plugin, opcua_log_lines):
    opcua_plugin.start()
    opcua_plugin.clients(count=1, write_period=0.1)

    result, elapsed = opcua_plugin.timed_stop()

    assert not _forced_cancel_logged(opcua_log_lines)
    assert result is True
    assert elapsed < GRACE_PERIOD_S, f"stop_loop() took {elapsed:.2f}s"


def test_a_server_overrunning_its_stop_still_closes_its_port(opcua_plugin):
    opcua_plugin.use_server(ignore_cancellation=True)
    opcua_plugin.start()
    opcua_plugin.clients(count=1, write_period=0.01)

    result, elapsed = opcua_plugin.timed_stop()

    assert result is False
    assert elapsed < OVERRUN_BOUND_S, f"stop_loop() took {elapsed:.2f}s"
    assert _port_refuses_connections(opcua_plugin.server.PORT)
    assert opcua_plugin.latest_accessor().is_valid is False


def test_a_normal_stop_invalidates_the_buffer_accessor(opcua_plugin):
    opcua_plugin.start()
    assert _wait_for_listener(opcua_plugin.server.PORT, STARTUP_TIMEOUT_S)

    result, _ = opcua_plugin.timed_stop()

    assert result is True
    assert opcua_plugin.latest_accessor().is_valid is False


def test_a_stop_issued_during_start_leaves_no_server_running(opcua_plugin, opcua_log_lines):
    opcua_plugin.use_server(start_delay=0.5)
    opcua_plugin.start()

    result, elapsed = opcua_plugin.timed_stop()

    assert not _server_threads()
    assert _port_refuses_connections(opcua_plugin.server.PORT)
    assert not _forced_cancel_logged(opcua_log_lines)
    assert result is True
    assert elapsed < GRACE_PERIOD_S, f"stop_loop() took {elapsed:.2f}s"
    threading.Event().wait(GRACE_PERIOD_S)
    refused_later = _port_refuses_connections(opcua_plugin.server.PORT)
    assert refused_later, "a start still in progress opened the port after the stop"


def test_an_invalidated_accessor_keeps_client_traffic_off_the_runtime(opcua_plugin):
    opcua_plugin.start()
    clients = opcua_plugin.clients(count=1, write_period=0.02, subscribe=True)
    assert opcua_plugin.runtime_args.debug_calls() > 0, "client traffic never reached the runtime"

    opcua_plugin.latest_accessor().invalidate()
    assert clients.wait_for_writes(extra=2, timeout=STARTUP_TIMEOUT_S)
    calls_after_invalidate = opcua_plugin.runtime_args.debug_calls()
    assert clients.wait_for_writes(extra=5, timeout=STARTUP_TIMEOUT_S)

    assert opcua_plugin.runtime_args.debug_calls() == calls_after_invalidate


def test_repeated_start_stop_cycles_each_stop_within_the_grace_period(
    opcua_plugin, opcua_log_lines
):
    durations = []
    for cycle in range(10):
        port_free = _port_refuses_connections(opcua_plugin.server.PORT)
        assert port_free, f"cycle {cycle}: port still accepting before start_loop()"
        opcua_plugin.start()
        clients = opcua_plugin.clients(count=1, write_period=0.1, verify_read_back=True)
        result, elapsed = opcua_plugin.timed_stop()
        clients.close()
        assert result is True, f"cycle {cycle}: stop_loop() returned {result!r}"
        durations.append(elapsed)

    assert not _forced_cancel_logged(opcua_log_lines)
    assert max(durations) < GRACE_PERIOD_S, f"stop durations: {[round(d, 2) for d in durations]}"
    assert len({id(manager.buffer_accessor) for manager in opcua_plugin.managers}) == 10
