# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""
Tests for the per-device ``reconnect`` setting of the Modbus master on TCP endpoints.

The plugin is driven through its lifecycle (``start_loop`` / ``stop_loop``) against a scripted
pymodbus client double, an in-memory IEC image and a virtual clock: no socket is opened and no
retry delay is slept for real.
"""

# pylint: disable=too-few-public-methods,too-many-arguments

from __future__ import annotations

import json
import sys
import threading
import time as real_time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union

import pytest
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.pdu import ExceptionResponse

import core.src.drivers.plugins.python.modbus_master.modbus_master_plugin as plugin
from core.src.drivers.plugins.python.shared.plugin_config_decode.modbus_master_config_model import (
    ModbusDeviceConfig,
    ModbusMasterConfig,
)

ENDPOINT_HOST = "192.168.1.50"
ENDPOINT_PORT = 502
ENDPOINT = f"{ENDPOINT_HOST}:{ENDPOINT_PORT}"
SERIAL_PORT = "/dev/ttyUSB0"
SET_TO_ZERO = "set-to-zero"
KEEP_LAST = "keep-last-value"
FAILED_FIRST_CONNECT = "failed first connect"
CONNECTION_LOST = "connection lost"
WAIT_SECONDS = 5.0


class _Absent:
    """Marks a configuration key that is left out of the device JSON."""


ABSENT = _Absent()


class OkReply:
    """Successful response; for a read it carries one register and one coil value."""

    def __init__(self, value: int = 1) -> None:
        self.registers = [value]
        self.bits = [bool(value)]

    def isError(self) -> bool:  # pylint: disable=invalid-name
        """Report success, as a pymodbus response does."""
        return False


class ErrorReply:
    """Response that is not an ExceptionResponse but reports an error through isError()."""

    def isError(self) -> bool:  # pylint: disable=invalid-name
        """Report failure."""
        return True


class Raise:
    """Scripted reply that raises ``error``; with ``drops`` the socket is closed first."""

    def __init__(self, error: Exception, drops: bool) -> None:
        self.error = error
        self.drops = drops


Reply = Union[OkReply, ErrorReply, ExceptionResponse, ModbusIOException, Raise]


class Signal:
    """Wakes the test thread whenever a device thread changes something the test observes."""

    def __init__(self) -> None:
        self._condition = threading.Condition()

    def notify(self) -> None:
        """Wake every waiter so it re-evaluates its predicate."""
        with self._condition:
            self._condition.notify_all()

    def wait_until(self, predicate: Callable[[], bool], timeout: float = WAIT_SECONDS) -> bool:
        """Block until ``predicate`` holds or ``timeout`` real seconds pass; return the outcome."""
        with self._condition:
            return self._condition.wait_for(predicate, timeout)


class FakeClock:
    """Stands in for a module's ``time``: ``sleep`` advances a virtual clock and returns at once."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        """Advance the virtual clock by ``seconds`` and yield the GIL."""
        self.now += seconds
        real_time.sleep(0)

    def __getattr__(self, name: str) -> object:
        return getattr(real_time, name)


class ScriptedClient:
    """Pymodbus client double whose connect results and per-address replies follow a script."""

    def __init__(self, signal: Signal, clock: FakeClock) -> None:
        self.signal = signal
        self.clock = clock
        self.online = True
        self.connected = False
        self.connect_attempts: List[float] = []
        self.requests: List[Tuple[str, int, int]] = []
        self.answered: List[int] = []
        self.failure_index: Optional[int] = None
        self._replies: Dict[int, List[Reply]] = {}

    def script(self, address: int, *replies: Reply) -> None:
        """Queue the replies given, in order, to the next requests on ``address``."""
        self._replies.setdefault(address, []).extend(replies)

    def connect(self) -> bool:
        """Record an attempt at the virtual time; succeed only while the slave is online."""
        self.connect_attempts.append(self.clock.now)
        self.connected = self.online
        self.signal.notify()
        return self.connected

    def close(self) -> None:
        """Close the socket."""
        self.connected = False

    def reads(self, address: int) -> int:
        """Number of read requests sent to ``address``."""
        return sum(1 for kind, addr, _ in self.requests if kind == "read" and addr == address)

    def writes(self, address: int) -> int:
        """Number of write requests sent to ``address``."""
        return sum(1 for kind, addr, _ in self.requests if kind == "write" and addr == address)

    def requests_after_failure(self) -> List[Tuple[str, int, int]]:
        """Requests sent after the first scripted failure."""
        if self.failure_index is None:
            return []
        return self.requests[self.failure_index :]

    def answered_reads(self, address: int) -> int:
        """Reads of ``address`` the slave answered successfully on a live connection."""
        return sum(
            1
            for index in self.answered
            if self.requests[index][0] == "read" and self.requests[index][1] == address
        )

    def answered_reads_after_failure(self, address: int) -> int:
        """Reads of ``address`` answered successfully after the first scripted failure."""
        if self.failure_index is None:
            return 0
        return sum(
            1
            for index in self.answered
            if index >= self.failure_index
            and self.requests[index][0] == "read"
            and self.requests[index][1] == address
        )

    def _request(self, kind: str, address: int, device_id: int, default: Reply) -> Reply:
        self.requests.append((kind, address, device_id))
        try:
            if not self.connected:
                raise ConnectionException("Client cannot connect (automatic retry continuing) !!")
            queue = self._replies.get(address)
            reply = queue.pop(0) if queue else default
            if not isinstance(reply, OkReply) and self.failure_index is None:
                self.failure_index = len(self.requests)
            if isinstance(reply, Raise):
                if reply.drops:
                    self.connected = False
                raise reply.error
            if isinstance(reply, OkReply):
                self.answered.append(len(self.requests) - 1)
            return reply
        finally:
            self.signal.notify()

    def read_coils(self, address: int, count: int = 1, device_id: int = 1) -> Reply:
        """FC 1."""
        return self._request("read", address, device_id, OkReply())

    def read_discrete_inputs(self, address: int, count: int = 1, device_id: int = 1) -> Reply:
        """FC 2."""
        return self._request("read", address, device_id, OkReply())

    def read_holding_registers(self, address: int, count: int = 1, device_id: int = 1) -> Reply:
        """FC 3."""
        return self._request("read", address, device_id, OkReply())

    def read_input_registers(self, address: int, count: int = 1, device_id: int = 1) -> Reply:
        """FC 4."""
        return self._request("read", address, device_id, OkReply())

    def write_coil(self, address: int, value: object, device_id: int = 1) -> Reply:
        """FC 5."""
        return self._request("write", address, device_id, OkReply())

    def write_register(self, address: int, value: object, device_id: int = 1) -> Reply:
        """FC 6."""
        return self._request("write", address, device_id, OkReply())

    def write_coils(self, address: int, values: object, device_id: int = 1) -> Reply:
        """FC 15."""
        return self._request("write", address, device_id, OkReply())

    def write_registers(self, address: int, values: object, device_id: int = 1) -> Reply:
        """FC 16."""
        return self._request("write", address, device_id, OkReply())


ImageKey = Tuple[str, int, object]


class FakeSafeBufferAccess:
    """In-memory IEC image exposing the SafeBufferAccess surface the Modbus master uses."""

    def __init__(self, config_path: str) -> None:
        self.is_valid = True
        self.error_msg = ""
        self.config_path = config_path
        self.image: Dict[ImageKey, object] = {}
        self.writes: List[Tuple[ImageKey, object]] = []

    def get_config_path(self) -> Tuple[str, str]:
        """Path of the plugin configuration file."""
        return self.config_path, ""

    def acquire_mutex(self) -> Tuple[bool, str]:
        """Always granted."""
        return True, "Success"

    def release_mutex(self) -> Tuple[bool, str]:
        """Always released."""
        return True, "Success"

    def zero_writes(self, key: ImageKey) -> int:
        """Number of times zero was written to ``key``."""
        return sum(1 for written, value in self.writes if written == key and value == 0)

    def __getattr__(self, name: str) -> Callable[..., Tuple[object, str]]:
        operation, _, buffer_type = name.partition("_")
        if operation == "write":
            return self._writer(buffer_type)
        if operation == "read":
            return self._reader(buffer_type)
        raise AttributeError(name)

    def _writer(self, buffer_type: str) -> Callable[..., Tuple[object, str]]:
        def write(index: int, *rest: object, **_options: object) -> Tuple[object, str]:
            key: ImageKey = (buffer_type, index, rest[0] if len(rest) == 2 else None)
            self.image[key] = rest[-1]
            self.writes.append((key, rest[-1]))
            return True, "Success"

        return write

    def _reader(self, buffer_type: str) -> Callable[..., Tuple[object, str]]:
        def read(index: int, *rest: object, **_options: object) -> Tuple[object, str]:
            return self.image.get((buffer_type, index, rest[0] if rest else None), 0), "Success"

        return read


class RecordingLogger:
    """PluginLogger double that keeps every message with its level."""

    def __init__(self, signal: Signal) -> None:
        self.signal = signal
        self.records: List[Tuple[str, str]] = []

    def _record(self, level: str, message: str) -> bool:
        self.records.append((level, message))
        self.signal.notify()
        return True

    def info(self, message: str) -> bool:
        """Info level."""
        return self._record("info", message)

    def debug(self, message: str) -> bool:
        """Debug level."""
        return self._record("debug", message)

    def warn(self, message: str) -> bool:
        """Warning level."""
        return self._record("warn", message)

    def warning(self, message: str) -> bool:
        """Warning level, alternative name."""
        return self._record("warn", message)

    def error(self, message: str) -> bool:
        """Error level."""
        return self._record("error", message)

    def messages(self, level: str) -> List[str]:
        """Messages logged at ``level``."""
        return [message for logged, message in self.records if logged == level]

    def give_ups(self, endpoint: Optional[str] = ENDPOINT) -> List[str]:
        """Warnings that announce giving up on ``endpoint``, or on any endpoint when None."""
        return [
            message
            for message in self.messages("warn")
            if (endpoint is None or endpoint in message)
            and (FAILED_FIRST_CONNECT in message.lower() or CONNECTION_LOST in message.lower())
        ]


class ModbusMasterHarness:
    """Runs start_loop/stop_loop against scripted slaves, a fake IEC image and a virtual clock."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.signal = Signal()
        self.clock = FakeClock()
        self.logger = RecordingLogger(self.signal)
        self.config_path = tmp_path / "modbus_master.json"
        self.sba = FakeSafeBufferAccess(str(self.config_path))
        self.clients: Dict[str, ScriptedClient] = {}
        self.client_creations = 0
        connection_module = sys.modules[plugin.ModbusConnectionManager.__module__]
        monkeypatch.setattr(connection_module, "ModbusTcpClient", self._tcp_client)
        monkeypatch.setattr(connection_module, "ModbusSerialClient", self._serial_client)
        monkeypatch.setattr(connection_module, "time", self.clock)
        monkeypatch.setattr(plugin, "time", self.clock)
        monkeypatch.setattr(plugin, "SafeBufferAccess", lambda _runtime_args: self.sba)
        monkeypatch.setattr(plugin, "runtime_args", SimpleNamespace())
        monkeypatch.setattr(plugin, "logger", self.logger)
        monkeypatch.setattr(plugin, "slave_threads", [])
        monkeypatch.setattr(plugin, "modbus_master_config", None)
        monkeypatch.setattr(plugin, "safe_buffer_accessor", None)

    def client(self, key: str = ENDPOINT) -> ScriptedClient:
        """The scripted slave behind ``key`` (``host:port`` or a serial port)."""
        if key not in self.clients:
            self.clients[key] = ScriptedClient(self.signal, self.clock)
        return self.clients[key]

    def _tcp_client(self, host: str = "", port: int = 0, **_options: object) -> ScriptedClient:
        self.client_creations += 1
        return self.client(f"{host}:{port}")

    def _serial_client(self, port: str = "", **_options: object) -> ScriptedClient:
        self.client_creations += 1
        return self.client(port)

    def start(self, *devices: Dict[str, object]) -> bool:
        """Write the plugin configuration and call start_loop()."""
        self.config_path.write_text(json.dumps(list(devices)), encoding="utf-8")
        return plugin.start_loop()

    def run(self, *devices: Dict[str, object]) -> None:
        """Start the plugin and require start_loop() to accept the configuration."""
        assert self.start(*devices), "start_loop() refused the configuration"

    def stop(self) -> bool:
        """Call stop_loop()."""
        return plugin.stop_loop()

    def wait_until(self, predicate: Callable[[], bool]) -> bool:
        """Wait for ``predicate`` driven by the device threads."""
        return self.signal.wait_until(predicate)

    def wait_for_give_up(self, endpoint: str = ENDPOINT) -> None:
        """Require a give-up warning for ``endpoint`` to be logged."""
        logged = self.wait_until(lambda: len(self.logger.give_ups(endpoint)) >= 1)
        assert logged, f"no give-up warning was logged for {endpoint}"


@pytest.fixture(name="master")
def fixture_master(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[ModbusMasterHarness]:
    """Modbus master plugin wired to scripted slaves; its device threads are stopped afterwards."""
    harness = ModbusMasterHarness(monkeypatch, tmp_path)
    yield harness
    plugin.stop_loop()


def read_point(
    address: int, iec_location: str, error_handling: str = KEEP_LAST
) -> Dict[str, object]:
    """FC 3 point of one register, polled every 100 ms."""
    return {
        "fc": 3,
        "offset": str(address),
        "iec_location": iec_location,
        "len": 1,
        "cycle_time_ms": 100,
        "error_handling": error_handling,
    }


def write_point(address: int, iec_location: str) -> Dict[str, object]:
    """FC 6 point of one register, written every 100 ms."""
    return {
        "fc": 6,
        "offset": str(address),
        "iec_location": iec_location,
        "len": 1,
        "cycle_time_ms": 100,
    }


def tcp_device(
    name: str,
    *,
    reconnect: object = ABSENT,
    host: str = ENDPOINT_HOST,
    port: int = ENDPOINT_PORT,
    slave_id: int = 1,
    io_points: Optional[List[Dict[str, object]]] = None,
) -> Dict[str, object]:
    """Device JSON as the editor writes it for a Modbus TCP slave."""
    config: Dict[str, object] = {
        "type": "SLAVE",
        "transport": "tcp",
        "host": host,
        "port": port,
        "slave_id": slave_id,
        "timeout_ms": 500,
        "io_points": io_points if io_points is not None else [read_point(0, "%IW0")],
    }
    if not isinstance(reconnect, _Absent):
        config["reconnect"] = reconnect
    return {"name": name, "protocol": "MODBUS", "config": config}


def rtu_device(
    name: str,
    *,
    reconnect: object = ABSENT,
    slave_id: int = 1,
    io_points: Optional[List[Dict[str, object]]] = None,
) -> Dict[str, object]:
    """Device JSON as the editor writes it for a Modbus RTU slave."""
    config: Dict[str, object] = {
        "type": "SLAVE",
        "transport": "rtu",
        "serial_port": SERIAL_PORT,
        "baud_rate": 9600,
        "parity": "N",
        "stop_bits": 1,
        "data_bits": 8,
        "slave_id": slave_id,
        "timeout_ms": 500,
        "io_points": io_points if io_points is not None else [read_point(0, "%IW0")],
    }
    if not isinstance(reconnect, _Absent):
        config["reconnect"] = reconnect
    return {"name": name, "protocol": "MODBUS", "config": config}


def master_config(*devices: Dict[str, object]) -> ModbusMasterConfig:
    """ModbusMasterConfig holding the devices given."""
    config = ModbusMasterConfig()
    config.devices = [ModbusDeviceConfig.from_dict(device) for device in devices]
    return config


def expected_retry_delays(count: int) -> List[float]:
    """Delays between attempts: 2 s, growing x1.5, capped at 30 s."""
    delays: List[float] = []
    delay = 2.0
    for _ in range(count):
        delays.append(delay)
        delay = min(delay * 1.5, 30.0)
    return delays


def connection_reset() -> Raise:
    """The slave closes the TCP connection."""
    return Raise(ConnectionException("Connection reset by peer"), drops=True)


PUMP_IMAGE = ("int_input", 0, None)
SECOND_ZERO_IMAGE = ("int_input", 1, None)
KEEP_LAST_IMAGE = ("int_input", 2, None)

LOST_CONNECTION_ERRORS = [
    pytest.param(connection_reset(), id="connection-exception-socket-closed"),
    pytest.param(
        Raise(ConnectionException("Client cannot connect"), drops=False),
        id="connection-exception-socket-still-open",
    ),
    pytest.param(
        Raise(
            ModbusIOException(
                "ERROR: No response received of the last requests (default: retries+3), "
                "CLOSING CONNECTION."
            ),
            drops=True,
        ),
        id="io-exception-on-a-closed-socket",
    ),
]

FAILED_POLL_ERRORS = [
    pytest.param(ExceptionResponse(3, 2), id="exception-response"),
    pytest.param(ErrorReply(), id="error-response"),
    pytest.param(
        ModbusIOException("No response received after 3 retries, continue with next request"),
        id="timeout-returned",
    ),
    pytest.param(
        Raise(
            ModbusIOException("No response received after 3 retries, continue with next request"),
            drops=False,
        ),
        id="timeout-raised",
    ),
    pytest.param(
        Raise(ModbusIOException("Unable to decode request"), drops=False), id="malformed-frame"
    ),
]

RECONNECT_ON_OR_ABSENT = [
    pytest.param(True, id="reconnect-true"),
    pytest.param(ABSENT, id="reconnect-absent"),
]


def run_lost_connection_on_read(master: ModbusMasterHarness, reconnect: object) -> ScriptedClient:
    """Pump polls three set-to-zero/keep-last reads and one write; the second poll of %IW0 drops."""
    client = master.client()
    client.script(0, OkReply(7), connection_reset())
    client.script(10, OkReply(8))
    client.script(20, OkReply(9))
    master.run(
        tcp_device(
            "Pump",
            reconnect=reconnect,
            io_points=[
                read_point(0, "%IW0", SET_TO_ZERO),
                read_point(10, "%IW1", SET_TO_ZERO),
                read_point(20, "%IW2", KEEP_LAST),
                write_point(30, "%QW0"),
            ],
        )
    )
    return client


def run_gateway(master: ModbusMasterHarness, reconnect: object) -> ScriptedClient:
    """Pump (slave 1) and Meter (slave 2) behind one gateway endpoint."""
    master.run(
        tcp_device(
            "Pump", reconnect=reconnect, slave_id=1, io_points=[read_point(0, "%IW0", SET_TO_ZERO)]
        ),
        tcp_device(
            "Meter",
            reconnect=reconnect,
            slave_id=2,
            io_points=[read_point(10, "%IW1", SET_TO_ZERO)],
        ),
    )
    return master.client()


class TestReconnectConfiguration:
    """The ``reconnect`` key of a device's ``config`` object."""

    def test_reconnect_defaults_to_true_when_the_key_is_absent(self) -> None:
        device = ModbusDeviceConfig.from_dict(tcp_device("Pump"))

        assert device.reconnect is True

    @pytest.mark.parametrize("value", [True, False])
    def test_reconnect_reads_an_explicit_boolean(self, value: bool) -> None:
        device = ModbusDeviceConfig.from_dict(tcp_device("Pump", reconnect=value))

        assert device.reconnect is value

    @pytest.mark.parametrize("value", ["false", 0, 1], ids=["string", "zero", "one"])
    def test_non_boolean_reconnect_is_refused_naming_the_device(self, value: object) -> None:
        config = master_config(tcp_device("Pump", reconnect=value))

        with pytest.raises(ValueError, match="Pump"):
            config.validate()

    def test_rtu_device_with_a_reconnect_key_is_accepted(self) -> None:
        master_config(rtu_device("Drive", reconnect=False)).validate()


class TestReconnectMismatchOnOneEndpoint:
    """Devices sharing a ``host:port`` must agree on ``reconnect``."""

    @pytest.mark.parametrize(
        "first,second",
        [
            pytest.param(True, False, id="true-vs-false"),
            pytest.param(ABSENT, False, id="absent-vs-false"),
        ],
    )
    def test_disagreeing_devices_are_refused_naming_the_endpoint_and_the_devices(
        self, first: object, second: object
    ) -> None:
        config = master_config(
            tcp_device("Pump", reconnect=first, slave_id=1),
            tcp_device("Meter", reconnect=second, slave_id=2),
        )

        with pytest.raises(ValueError) as refused:
            config.validate()

        message = str(refused.value)
        assert ENDPOINT in message and "Pump" in message and "Meter" in message

    def test_gateway_devices_agreeing_on_reconnect_off_are_accepted(self) -> None:
        master_config(
            tcp_device("Pump", reconnect=False, slave_id=1),
            tcp_device("Meter", reconnect=False, slave_id=2),
        ).validate()

    def test_devices_on_different_ports_may_disagree(self) -> None:
        master_config(
            tcp_device("Pump", reconnect=True, port=502),
            tcp_device("Meter", reconnect=False, port=503),
        ).validate()

    def test_start_loop_logs_the_mismatch_and_returns_false(
        self, master: ModbusMasterHarness
    ) -> None:
        master.client().online = False

        started = master.start(
            tcp_device("Pump", reconnect=True, slave_id=1),
            tcp_device("Meter", reconnect=False, slave_id=2),
        )

        refusals = master.logger.messages("error") + master.logger.messages("warn")
        assert started is False
        assert any(ENDPOINT in message for message in refusals)

    def test_start_loop_starts_no_thread_for_a_mismatched_endpoint(
        self, master: ModbusMasterHarness
    ) -> None:
        master.client().online = False

        master.start(
            tcp_device("Pump", reconnect=True, slave_id=1),
            tcp_device("Meter", reconnect=False, slave_id=2),
        )

        assert plugin.slave_threads == []
        assert master.client_creations == 0


class TestOfflineAtStartup:
    """A single TCP device whose slave does not answer the first connect."""

    def test_reconnect_off_makes_a_single_connect_attempt(
        self, master: ModbusMasterHarness
    ) -> None:
        client = master.client()
        client.online = False
        master.run(tcp_device("Pump", reconnect=False))
        master.wait_for_give_up()

        master.stop()

        assert len(client.connect_attempts) == 1

    def test_reconnect_off_logs_failed_first_connect_once_with_endpoint_and_device(
        self, master: ModbusMasterHarness
    ) -> None:
        master.client().online = False
        master.run(tcp_device("Pump", reconnect=False))
        master.wait_for_give_up()

        master.stop()

        give_ups = master.logger.give_ups()
        assert len(give_ups) == 1
        assert "Pump" in give_ups[0] and FAILED_FIRST_CONNECT in give_ups[0].lower()

    @pytest.mark.parametrize("reconnect", RECONNECT_ON_OR_ABSENT)
    def test_reconnect_on_or_absent_keeps_retrying(
        self, master: ModbusMasterHarness, reconnect: object
    ) -> None:
        client = master.client()
        client.online = False
        master.run(tcp_device("Pump", reconnect=reconnect))

        retried = master.wait_until(lambda: len(client.connect_attempts) >= 10)
        master.stop()

        assert retried and master.logger.give_ups(None) == []

    @pytest.mark.parametrize("reconnect", RECONNECT_ON_OR_ABSENT)
    def test_reconnect_on_or_absent_waits_2s_growing_by_half_up_to_30s(
        self, master: ModbusMasterHarness, reconnect: object
    ) -> None:
        client = master.client()
        client.online = False
        master.run(tcp_device("Pump", reconnect=reconnect))
        assert master.wait_until(lambda: len(client.connect_attempts) >= 10)

        master.stop()

        attempts = client.connect_attempts[:10]
        delays = [later - earlier for earlier, later in zip(attempts, attempts[1:])]
        assert delays == pytest.approx(expected_retry_delays(9), abs=0.1)


class TestLostConnection:
    """A single TCP device whose connection drops after it was established."""

    @pytest.mark.parametrize("failure", LOST_CONNECTION_ERRORS)
    def test_reconnect_off_logs_connection_lost_once_with_endpoint_and_device(
        self, master: ModbusMasterHarness, failure: Raise
    ) -> None:
        master.client().script(0, OkReply(7), failure)
        master.run(tcp_device("Pump", reconnect=False))
        master.wait_for_give_up()

        master.stop()

        give_ups = master.logger.give_ups()
        assert len(give_ups) == 1
        assert "Pump" in give_ups[0] and CONNECTION_LOST in give_ups[0].lower()

    def test_reconnect_off_sends_no_further_read_or_write(
        self, master: ModbusMasterHarness
    ) -> None:
        client = run_lost_connection_on_read(master, reconnect=False)
        master.wait_for_give_up()

        master.stop()

        assert client.requests_after_failure() == []

    def test_reconnect_off_does_not_connect_again(self, master: ModbusMasterHarness) -> None:
        client = run_lost_connection_on_read(master, reconnect=False)
        master.wait_for_give_up()

        master.stop()

        assert len(client.connect_attempts) == 1

    def test_reconnect_off_zero_fills_the_failing_set_to_zero_point_once(
        self, master: ModbusMasterHarness
    ) -> None:
        run_lost_connection_on_read(master, reconnect=False)
        master.wait_for_give_up()

        master.stop()

        assert master.sba.image[PUMP_IMAGE] == 0
        assert master.sba.zero_writes(PUMP_IMAGE) == 1

    def test_reconnect_off_zero_fills_every_set_to_zero_read_point_of_the_endpoint(
        self, master: ModbusMasterHarness
    ) -> None:
        run_lost_connection_on_read(master, reconnect=False)
        master.wait_for_give_up()

        master.stop()

        assert master.sba.image[SECOND_ZERO_IMAGE] == 0
        assert master.sba.zero_writes(SECOND_ZERO_IMAGE) == 1

    def test_reconnect_off_leaves_keep_last_value_points_untouched(
        self, master: ModbusMasterHarness
    ) -> None:
        run_lost_connection_on_read(master, reconnect=False)
        master.wait_for_give_up()

        master.stop()

        assert master.sba.image[KEEP_LAST_IMAGE] == 9
        assert master.sba.zero_writes(KEEP_LAST_IMAGE) == 0

    def test_reconnect_off_gives_up_on_a_connection_lost_while_writing(
        self, master: ModbusMasterHarness
    ) -> None:
        master.client().script(30, OkReply(), connection_reset())
        master.run(tcp_device("Pump", reconnect=False, io_points=[write_point(30, "%QW0")]))
        master.wait_for_give_up()

        master.stop()

        assert CONNECTION_LOST in master.logger.give_ups()[0].lower()

    def test_reconnect_off_sends_no_further_write_after_a_write_loses_the_connection(
        self, master: ModbusMasterHarness
    ) -> None:
        client = master.client()
        client.script(30, OkReply(), connection_reset())
        master.run(tcp_device("Pump", reconnect=False, io_points=[write_point(30, "%QW0")]))
        master.wait_for_give_up()

        master.stop()

        assert client.requests_after_failure() == []

    @pytest.mark.parametrize("reconnect", RECONNECT_ON_OR_ABSENT)
    def test_reconnect_on_or_absent_reconnects_and_polls_again(
        self, master: ModbusMasterHarness, reconnect: object
    ) -> None:
        client = run_lost_connection_on_read(master, reconnect=reconnect)

        recovered = master.wait_until(
            lambda: len(client.connect_attempts) >= 2
            and client.answered_reads_after_failure(0) >= 1
        )
        master.stop()

        assert recovered and master.logger.give_ups(None) == []


class TestFailedPollIsNotALostConnection:
    """With ``reconnect`` off, an error on a live connection is a failed poll, not a give-up."""

    @pytest.mark.parametrize("failure", FAILED_POLL_ERRORS)
    def test_reconnect_off_keeps_the_connection(
        self, master: ModbusMasterHarness, failure: Reply
    ) -> None:
        client = master.client()
        client.script(0, OkReply(7), failure)
        master.run(tcp_device("Pump", reconnect=False))
        assert master.wait_until(lambda: client.reads(0) >= 4)

        master.stop()

        assert len(client.connect_attempts) == 1

    @pytest.mark.parametrize("failure", FAILED_POLL_ERRORS)
    def test_reconnect_off_does_not_give_up(
        self, master: ModbusMasterHarness, failure: Reply
    ) -> None:
        client = master.client()
        client.script(0, OkReply(7), failure)
        master.run(tcp_device("Pump", reconnect=False))

        polled_on = master.wait_until(lambda: client.reads(0) >= 4)
        master.stop()

        assert polled_on and master.logger.give_ups(None) == []

    def test_reconnect_off_still_applies_set_to_zero_on_the_failed_poll(
        self, master: ModbusMasterHarness
    ) -> None:
        client = master.client()
        client.script(0, OkReply(7), ExceptionResponse(3, 2))
        master.run(
            tcp_device("Pump", reconnect=False, io_points=[read_point(0, "%IW0", SET_TO_ZERO)])
        )
        assert master.wait_until(lambda: client.reads(0) >= 3)

        master.stop()

        assert master.sba.zero_writes(PUMP_IMAGE) >= 1

    def test_reconnect_off_keeps_the_connection_after_an_exception_response_to_a_write(
        self, master: ModbusMasterHarness
    ) -> None:
        client = master.client()
        client.script(30, OkReply(), ExceptionResponse(6, 2))
        master.run(tcp_device("Pump", reconnect=False, io_points=[write_point(30, "%QW0")]))
        assert master.wait_until(lambda: client.writes(30) >= 4)

        master.stop()

        assert len(client.connect_attempts) == 1


class TestGatewayEndpoint:
    """Two devices behind one ``host:port`` share one connection and one give-up."""

    def test_offline_at_startup_gives_up_once_naming_every_device(
        self, master: ModbusMasterHarness
    ) -> None:
        master.client().online = False
        run_gateway(master, reconnect=False)
        master.wait_for_give_up()

        master.stop()

        give_ups = master.logger.give_ups()
        assert len(give_ups) == 1
        assert "Pump" in give_ups[0] and "Meter" in give_ups[0]
        assert FAILED_FIRST_CONNECT in give_ups[0].lower()

    def test_offline_at_startup_makes_a_single_connect_attempt(
        self, master: ModbusMasterHarness
    ) -> None:
        client = master.client()
        client.online = False
        run_gateway(master, reconnect=False)
        master.wait_for_give_up()

        master.stop()

        assert len(client.connect_attempts) == 1

    def test_lost_connection_gives_up_once_naming_every_device(
        self, master: ModbusMasterHarness
    ) -> None:
        master.client().script(0, OkReply(7), connection_reset())
        run_gateway(master, reconnect=False)
        master.wait_for_give_up()

        master.stop()

        give_ups = master.logger.give_ups()
        assert len(give_ups) == 1
        assert "Pump" in give_ups[0] and "Meter" in give_ups[0]
        assert CONNECTION_LOST in give_ups[0].lower()

    def test_lost_connection_sends_no_request_to_any_slave_of_the_endpoint(
        self, master: ModbusMasterHarness
    ) -> None:
        master.client().script(0, OkReply(7), connection_reset())
        client = run_gateway(master, reconnect=False)
        master.wait_for_give_up()

        master.stop()

        assert client.requests_after_failure() == []

    def test_reconnect_on_reconnects_after_a_drop(self, master: ModbusMasterHarness) -> None:
        master.client().script(0, OkReply(7), connection_reset())
        client = run_gateway(master, reconnect=True)

        recovered = master.wait_until(
            lambda: len(client.connect_attempts) >= 2
            and client.answered_reads_after_failure(10) >= 1
        )
        master.stop()

        assert recovered and master.logger.give_ups(None) == []


class TestStopThenStart:
    """Stop then Start runs fresh device threads, so a given-up endpoint is tried again."""

    def test_stop_then_start_after_a_give_up_connects_again(
        self, master: ModbusMasterHarness
    ) -> None:
        client = master.client()
        client.online = False
        master.run(tcp_device("Pump", reconnect=False))
        master.wait_for_give_up()
        assert master.stop()
        client.online = True

        master.run(tcp_device("Pump", reconnect=False))

        assert master.wait_until(lambda: client.answered_reads(0) >= 1)

    def test_stop_then_start_keeps_only_the_new_run_threads(
        self, master: ModbusMasterHarness
    ) -> None:
        client = master.client()
        master.run(tcp_device("Pump", reconnect=True))
        assert master.wait_until(lambda: client.answered_reads(0) >= 1)
        assert master.stop()

        master.run(tcp_device("Pump", reconnect=True))

        assert len(plugin.slave_threads) == 1

    def test_start_counts_only_the_threads_it_started_in_this_call(
        self, master: ModbusMasterHarness
    ) -> None:
        plugin.slave_threads.append(LingeringThread())

        master.run(tcp_device("Pump", reconnect=True))

        assert "Successfully started 1 device/bus thread(s)" in master.logger.messages("info")

    def test_start_with_no_new_thread_is_refused_even_with_a_leftover(
        self, master: ModbusMasterHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plugin.slave_threads.append(LingeringThread())
        monkeypatch.setattr(plugin, "ModbusSlaveDevice", failing_thread_creation)

        started = master.start(tcp_device("Pump", reconnect=True))

        assert started is False

    def test_start_with_no_new_thread_logs_that_none_started(
        self, master: ModbusMasterHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plugin.slave_threads.append(LingeringThread())
        monkeypatch.setattr(plugin, "ModbusSlaveDevice", failing_thread_creation)

        master.start(tcp_device("Pump", reconnect=True))

        assert "No device threads started" in master.logger.messages("error")

    def test_a_thread_that_outlived_stop_is_joined_again_by_the_next_stop(
        self, master: ModbusMasterHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        leftover = LingeringThread()
        plugin.slave_threads.append(leftover)
        assert master.stop() is False
        monkeypatch.setattr(plugin, "ModbusSlaveDevice", failing_thread_creation)
        master.start(tcp_device("Pump", reconnect=True))

        master.stop()

        assert leftover in plugin.slave_threads and leftover.stop_calls == 2


class LingeringThread:
    """Device thread double that never ends: it stays alive after ``stop()`` and ``join()``."""

    def __init__(self) -> None:
        self.name = "Lingering"
        self.stop_calls = 0

    def stop(self) -> None:
        """Record the stop request without ending."""
        self.stop_calls += 1

    def join(self, **_options: object) -> None:
        """Return at once, as a join that timed out does."""

    def is_alive(self) -> bool:
        """Always alive."""
        return True


def failing_thread_creation(*_args: object, **_kwargs: object) -> LingeringThread:
    """Stands in for ``ModbusSlaveDevice`` when creating the device thread fails."""
    raise RuntimeError("thread creation failed")


class TestRtuBusUnchanged:
    """RTU buses ignore ``reconnect``."""

    def test_rtu_bus_with_reconnect_off_still_reconnects_after_a_drop(
        self, master: ModbusMasterHarness
    ) -> None:
        client = master.client(SERIAL_PORT)
        client.script(0, OkReply(7), connection_reset())
        master.run(rtu_device("Drive", reconnect=False))

        recovered = master.wait_until(
            lambda: len(client.connect_attempts) >= 2
            and client.answered_reads_after_failure(0) >= 1
        )
        master.stop()

        assert recovered and master.logger.give_ups(None) == []
