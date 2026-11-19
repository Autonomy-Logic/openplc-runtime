# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

import asyncio
import contextlib
import gc
import socket
import sys
import threading
import time
from typing import List, Optional, Tuple

import pytest
from asyncua import Client, Server, ua
from asyncua.server.address_space import NodeManagementService
from asyncua.ua.uaerrors import UaStatusCodeError

import address_space
import opcua_security
import plugin

_OpcuaServerManager = plugin.OpcuaServerManager

GRACE_PERIOD_S = 2.0
OVERRUN_BOUND_S = 6.0
FORCED_CANCEL_LINE = "Graceful stop did not complete in 2s"
SERVER_THREAD_NAME = "opcua-server"
REQUEST_TIMEOUT_S = 1.0
STARTUP_TIMEOUT_S = 15.0
SESSION_CLOSE_TIMEOUT_S = 2.0 * REQUEST_TIMEOUT_S
FILL_CALLS = 5821
SLOWED_FILL_S = 5.0
SECURITY_STEP_DELAY_S = 0.4
VARIABLE_DELAY_S = 0.1
PORT_POLL_PERIOD_S = 0.05
PORT_WATCH_AFTER_FILL_S = 1.0
STOP_AFTER_ENTERED_S = 0.1
ABORT_LATENCY_S = 1.0
EARLY_RUN_BOUND_S = 0.5
FALLBACK_WARNING = "stoppable address-space fill"


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


def _wait_until(moment: float) -> None:
    """Block the calling thread until the monotonic clock reaches the moment."""
    threading.Event().wait(max(0.0, moment - time.monotonic()))


class _SlowedStep:
    """A start-up step slowed by a synchronous sleep on each call, recording when it is entered."""

    def __init__(self, delay: float):
        self.delay = delay
        self.active = True
        self.entered = threading.Event()
        self._calls = 0
        self._progress = threading.Condition()

    def enter(self) -> None:
        with self._progress:
            self._calls += 1
            self._progress.notify_all()
        self.entered.set()
        if self.active and self.delay:
            time.sleep(self.delay)

    def wait_for_calls(self, count: int, timeout: float) -> bool:
        """Wait until the step was entered `count` times."""
        with self._progress:
            return self._progress.wait_for(lambda: self._calls >= count, timeout)


class _PacedFill(_SlowedStep):
    """asyncua's standard address-space fill, paced by synchronous sleeps on each try_add_nodes call
    so it lasts SLOWED_FILL_S with no suspension point, as the fill on a slow device does."""

    def __init__(self):
        super().__init__(SLOWED_FILL_S / FILL_CALLS)
        self.entered_at: Optional[float] = None

    def enter(self) -> None:
        with self._progress:
            if self.entered_at is None:
                self.entered_at = time.monotonic()
            self._calls += 1
            slot = self.entered_at + self._calls * self.delay
            self._progress.notify_all()
        self.entered.set()
        if self.active:
            time.sleep(max(0.0, slot - time.monotonic()))


def _slowed_coroutine(step: _SlowedStep, original):
    """Wrap an async method so each call enters the step, synchronously, before it runs."""

    async def slowed(*args, **kwargs):
        step.enter()
        return await original(*args, **kwargs)

    return slowed


def _slowed_function(step: _SlowedStep, original):
    """Wrap a method so each call enters the step before it runs."""

    def slowed(*args, **kwargs):
        step.enter()
        return original(*args, **kwargs)

    return slowed


class _StartupSlowdowns:
    """Make the OPC-UA start-up block its event loop like the real one, step by step."""

    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch
        self.fill = _PacedFill()
        self.variables = _SlowedStep(VARIABLE_DELAY_S)
        self.certificate_check = _SlowedStep(SECURITY_STEP_DELAY_S)
        self.load_certificate = _SlowedStep(SECURITY_STEP_DELAY_S)
        self.load_private_key = _SlowedStep(SECURITY_STEP_DELAY_S)
        self.server_start = _SlowedStep(0.0)

    def slow_fill(self) -> None:
        original = NodeManagementService.try_add_nodes
        fill = self.fill

        def try_add_nodes(service, *args, **kwargs):
            fill.enter()
            yield from original(service, *args, **kwargs)

        self._monkeypatch.setattr(NodeManagementService, "try_add_nodes", try_add_nodes)

    def slow_security(self) -> None:
        manager = opcua_security.OpcuaSecurityManager
        self._monkeypatch.setattr(
            manager,
            "_is_certificate_valid",
            _slowed_function(self.certificate_check, manager._is_certificate_valid),
        )
        for name, step in (
            ("load_certificate", self.load_certificate),
            ("load_private_key", self.load_private_key),
        ):
            self._monkeypatch.setattr(Server, name, _slowed_coroutine(step, getattr(Server, name)))

    def slow_variables(self) -> None:
        builder = address_space.AddressSpaceBuilder
        self._monkeypatch.setattr(
            builder,
            "_create_simple_variable",
            _slowed_coroutine(self.variables, builder._create_simple_variable),
        )

    def watch_server_start(self) -> None:
        slowed = _slowed_coroutine(self.server_start, Server.start)
        self._monkeypatch.setattr(Server, "start", slowed)

    def release(self) -> None:
        """Drop every slowdown, so start-up work still in progress finishes at full speed."""
        for step in (
            self.fill,
            self.variables,
            self.certificate_check,
            self.load_certificate,
            self.load_private_key,
            self.server_start,
        ):
            step.active = False


class _PortWatch:
    """Polls a loopback port every PORT_POLL_PERIOD_S in its own thread and records each accepted
    connection."""

    def __init__(self, port: int):
        self.port = port
        self.accepted_at: List[float] = []
        self._done = threading.Event()
        self.thread = threading.Thread(target=self._poll, name="opcua-test-port-watch", daemon=True)

    def start(self) -> "_PortWatch":
        self.thread.start()
        return self

    def stop_at(self, moment: float) -> None:
        _wait_until(moment)
        self.stop()

    def stop(self) -> None:
        self._done.set()
        self.thread.join(timeout=5.0)

    def _poll(self) -> None:
        while not self._done.is_set():
            if not _port_refuses_connections(self.port):
                self.accepted_at.append(time.monotonic())
            self._done.wait(PORT_POLL_PERIOD_S)


def _threads_started_since(before, *ignored: threading.Thread) -> List[str]:
    """Names of the live threads that were not alive in `before`, leaving out `ignored`."""
    return [
        t.name
        for t in threading.enumerate()
        if t.is_alive() and t not in before and t not in ignored
    ]


@pytest.fixture
def slowed_plugin(opcua_plugin, monkeypatch):
    """The OPC-UA plugin harness with asyncua's standard address-space fill paced to last
    SLOWED_FILL_S."""
    slowdowns = _StartupSlowdowns(monkeypatch)
    slowdowns.slow_fill()
    yield opcua_plugin, slowdowns
    slowdowns.release()


def _stop_while_starting(harness: _PluginHarness, fill: _PacedFill, into_fill_s: Optional[float]):
    """start_loop(), then stop_loop() right away or `into_fill_s` after the slowed fill began.

    Returns:
        (stop_loop() result, its duration, the threads started since start_loop() still alive when
        it returned, the moments the port accepted a connection from the call until
        PORT_WATCH_AFTER_FILL_S after the slowed fill would have ended)
    """
    threads_before = set(threading.enumerate())
    harness.start()
    if into_fill_s is not None:
        assert fill.entered.wait(STARTUP_TIMEOUT_S), "the slowed fill never began"
        _wait_until(fill.entered_at + into_fill_s)

    watch = _PortWatch(harness.server.PORT).start()
    called_at = time.monotonic()
    result, elapsed = harness.timed_stop()
    leftover = _threads_started_since(threads_before, watch.thread)

    fill_began_at = fill.entered_at if fill.entered_at is not None else called_at
    watch.stop_at(max(called_at, fill_began_at) + SLOWED_FILL_S + PORT_WATCH_AFTER_FILL_S)
    return result, elapsed, leftover, watch.accepted_at


@pytest.mark.parametrize(
    "into_fill_s",
    [None, 0.5, 2.5],
    ids=["right_after_start_loop", "0.5s_into_the_fill", "2.5s_into_the_fill"],
)
def test_a_stop_during_the_blocking_start_up_finishes_within_the_grace_period(
    slowed_plugin, opcua_log_records, capfd, into_fill_s
):
    harness, slowdowns = slowed_plugin

    result, elapsed, leftover, accepted_at = _stop_while_starting(
        harness, slowdowns.fill, into_fill_s
    )

    assert result is True, f"stop_loop() returned {result!r} after {elapsed:.2f}s"
    assert elapsed < GRACE_PERIOD_S, f"stop_loop() took {elapsed:.2f}s"
    assert not _forced_cancel_logged(opcua_log_records.lines)
    assert opcua_log_records.lines_at("ERROR") == []
    assert "Traceback" not in capfd.readouterr().err
    assert leftover == [], f"threads still alive after stop_loop(): {leftover}"
    assert accepted_at == [], "the port accepted a connection after stop_loop() was called"


def test_the_next_start_after_a_stopped_start_up_serves_clients(slowed_plugin, opcua_log_lines):
    harness, slowdowns = slowed_plugin
    _stop_while_starting(harness, slowdowns.fill, 0.5)
    slowdowns.release()

    harness.start()
    assert _wait_for_listener(harness.server.PORT, STARTUP_TIMEOUT_S)
    harness.clients(count=1, write_period=0.1, verify_read_back=True)
    lines_before_stop = len(opcua_log_lines)
    result, elapsed = harness.timed_stop()

    assert result is True
    assert elapsed < GRACE_PERIOD_S, f"stop_loop() took {elapsed:.2f}s"
    assert not _forced_cancel_logged(opcua_log_lines[lines_before_stop:])


class _ManagerRun:
    """Runs OpcuaServerManager.run() on its own event loop in a test thread."""

    def __init__(self, manager: _OpcuaServerManager, slowdowns: _StartupSlowdowns):
        self.manager = manager
        self.slowdowns = slowdowns
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread = threading.Thread(
            target=self._run, name="opcua-test-manager-run", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def ended_within(self, timeout: float) -> bool:
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def shut_down(self) -> None:
        self.slowdowns.release()
        loop = self._loop
        if self._thread.is_alive() and loop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.manager.stop()))
        if self._thread.ident is not None:
            self._thread.join(timeout=10.0)
        _close_leaked_listeners()

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.manager.run()


@pytest.fixture
def manager_run(monkeypatch, secure_manager_server, secure_manager_accessor, opcua_log_records):
    """A server manager on 14841 whose security setup, fill and address-space build all block
    its event loop."""
    slowdowns = _StartupSlowdowns(monkeypatch)
    slowdowns.slow_security()
    slowdowns.slow_fill()
    slowdowns.slow_variables()
    slowdowns.watch_server_start()
    config = plugin.load_config(secure_manager_server.config_path)
    manager = _OpcuaServerManager(
        config, secure_manager_accessor, str(secure_manager_server.plugin_dir)
    )
    run = _ManagerRun(manager, slowdowns)
    yield run
    run.shut_down()


@pytest.mark.parametrize(
    ("step", "calls", "later_steps"),
    [
        pytest.param(
            "load_private_key",
            1,
            ("fill", "variables", "server_start"),
            id="inside_load_private_key",
        ),
        pytest.param("fill", 1, ("variables", "server_start"), id="inside_the_fill"),
        pytest.param(
            "variables", 3, ("server_start",), id="inside_the_address_space_build_after_3_variables"
        ),
    ],
)
def test_a_stop_request_during_a_start_up_step_aborts_the_start_up(
    manager_run, secure_manager_server, opcua_log_records, capfd, step, calls, later_steps
):
    watch = _PortWatch(secure_manager_server.PORT).start()
    manager_run.start()
    assert getattr(manager_run.slowdowns, step).wait_for_calls(calls, STARTUP_TIMEOUT_S)
    threading.Event().wait(STOP_AFTER_ENTERED_S)

    manager_run.manager.request_stop()
    ended = manager_run.ended_within(ABORT_LATENCY_S)
    watch.stop()

    assert ended, f"run() still going {ABORT_LATENCY_S}s after request_stop()"
    assert watch.accepted_at == [], f"port {secure_manager_server.PORT} accepted a connection"
    began = [name for name in later_steps if getattr(manager_run.slowdowns, name).entered.is_set()]
    assert began == [], f"steps after the aborted one began: {began}"
    assert opcua_log_records.lines_at("ERROR") == []
    assert "Traceback" not in capfd.readouterr().err


def test_a_stop_requested_before_run_skips_the_whole_start_up(manager_run, secure_manager_server):
    manager_run.manager.request_stop()
    manager_run.start()

    assert manager_run.ended_within(EARLY_RUN_BOUND_S)
    assert not manager_run.slowdowns.load_certificate.entered.is_set()
    assert not (secure_manager_server.plugin_dir / "certs").exists()


@pytest.mark.parametrize("missing", ["standard_address_space", "InternalServer"])
def test_without_the_asyncua_internals_the_server_starts_on_a_plain_server(
    opcua_plugin, opcua_log_records, monkeypatch, missing
):
    monkeypatch.setattr(sys.modules[_OpcuaServerManager.__module__], missing, None)

    opcua_plugin.start()

    assert _wait_for_listener(opcua_plugin.server.PORT, STARTUP_TIMEOUT_S)
    warnings = [line for line in opcua_log_records.lines_at("WARN") if FALLBACK_WARNING in line]
    assert len(warnings) == 1, f"fallback warnings: {warnings}"
    result, _ = opcua_plugin.timed_stop()
    assert result is True
