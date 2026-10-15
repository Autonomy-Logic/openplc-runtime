# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Product publisher against a real Mosquitto: lifecycle, outage, recovery and scan continuity.

Runs through ``run.sh`` with the plugin sources mounted at ``SPIKE_PLUGINS``.
The program is a stand-in whose scan thread increments a counter every 10 ms
under a real image lock, so a frozen or stale value and any lock contention
caused by the publisher are observable.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

PLUGINS = Path(os.environ.get("SPIKE_PLUGINS", "/plugins"))
sys.path[:0] = [str(PLUGINS / "mqtt"), str(PLUGINS)]

# pylint: disable=wrong-import-position
from contract.runtime_config import Leaf, decode_runtime_config
from mqtt_publisher.backoff import Backoff
from mqtt_publisher.clock import SystemClock
from mqtt_publisher.connection import ConnectSpec, TransportError
from mqtt_publisher.status import StatusTracker
from mqtt_publisher.strategy import (
    InboundMessage,
    OutboundMessage,
    PublishOutcome,
)
from mqtt_publisher.supervisor import (
    Publisher,
    Supervisor,
    SupervisorSettings,
)
from mqtt_publisher.transport import PahoConnection, paho_connection
from mqtt_reader.reader import SampleResult, bind_leaves

BROKER = os.environ.get("SPIKE_BROKER", "broker")
PKI = Path(os.environ.get("SPIKE_PKI", "/pki"))
CA_PEM = (PKI / "ca.crt").read_text()
PASSWORD = (PKI / "password").read_text()
TOPIC = "r4/probe/counter"
STATUS_CONFIG = decode_runtime_config(
    (
        PLUGINS
        / "mqtt/contract/fixtures/runtime-config/valid/generic-on-change-anonymous-plaintext.json"
    ).read_bytes(),
    None,
)
SCAN_PERIOD_S = 0.010


class ScanProgram:
    """DebugAccess over one DINT counter that a 10 ms scan thread increments under the image lock."""

    def __init__(self) -> None:
        self._image = threading.Lock()
        self.counter = 0
        self._stop = threading.Event()
        self.scans = 0
        self.max_lock_wait = 0.0
        self.max_scan_gap = 0.0
        self.max_reader_hold = 0.0
        self._held_since = 0.0
        self._thread = threading.Thread(target=self._scan, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)

    def _scan(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            wanted = time.monotonic()
            with self._image:
                self.max_lock_wait = max(self.max_lock_wait, time.monotonic() - wanted)
                self.counter += 1
            self.scans += 1
            now = time.monotonic()
            self.max_scan_gap = max(self.max_scan_gap, now - last)
            last = now
            time.sleep(SCAN_PERIOD_S)

    def lock(self) -> None:
        self._image.acquire()
        self._held_since = time.monotonic()

    def unlock(self) -> None:
        self.max_reader_hold = max(self.max_reader_hold, time.monotonic() - self._held_since)
        self._image.release()

    def array_count(self) -> int:
        return 1

    def elem_count(self, arr: int) -> int:
        return 1

    def size(self, arr: int, elem: int) -> int:
        return 4

    def read(self, arr: int, elem: int) -> bytes:
        return struct.pack("<i", self.counter)


class ProbeStrategy:
    """Test probe: one topic carrying the counter on connect and on every tick."""

    def __init__(self, qos: int = 1) -> None:
        self._qos = qos
        self.dropped = 0

    @property
    def tick_interval_ms(self) -> int:
        return 100

    def will(self) -> OutboundMessage | None:
        return OutboundMessage("r4/probe/will", b"lost", 1, False)

    def subscriptions(self) -> Sequence[tuple[str, int]]:
        return ()

    def _message(self, sample: SampleResult) -> list[OutboundMessage]:
        text = sample.values[0].json_text or "null"
        return [OutboundMessage(TOPIC, text.encode(), self._qos, False)]  # type: ignore[arg-type]

    def on_connected(self, sample: SampleResult, now_ms: int) -> Sequence[OutboundMessage]:
        return self._message(sample)

    def on_sample(self, sample: SampleResult, now_ms: int) -> Sequence[OutboundMessage]:
        return self._message(sample)

    def on_inbound(self, message: InboundMessage, now_ms: int) -> Sequence[OutboundMessage]:
        return ()

    def after_publish(self, outcome: PublishOutcome) -> None:
        self.dropped += len(outcome.dropped)

    def on_disconnected(self) -> None:
        pass

    def on_shutdown(self, now_ms: int) -> Sequence[OutboundMessage]:
        return (OutboundMessage("r4/probe/state", b"offline", 1, False),)


class Gate:
    """TCP forwarder to the broker that a test can cut and close to simulate an outage."""

    def __init__(self, target_port: int) -> None:
        self._target = (BROKER, target_port)
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self.open = True
        self.accepted = 0
        self._links: list[socket.socket] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            if not self.open:
                client.close()
                continue
            upstream = socket.create_connection(self._target)
            with self._lock:
                self.accepted += 1
                self._links += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def _pipe(a: socket.socket, b: socket.socket) -> None:
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def outage(self) -> None:
        self.open = False
        with self._lock:
            links, self._links = self._links, []
        for s in links:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def restore(self) -> None:
        self.open = True

    def close(self) -> None:
        self.outage()
        self._server.close()


class Collector:
    def __init__(self, topic: str) -> None:
        self.values: list[tuple[float, bytes]] = []
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"r4-collector-{time.monotonic_ns()}"
        )
        self._client.on_message = lambda _c, _u, m: self.values.append(
            (time.monotonic(), bytes(m.payload))
        )
        subscribed = threading.Event()
        self._client.on_subscribe = lambda *_a: subscribed.set()
        self._client.connect(BROKER, 1883)
        self._client.subscribe(topic, qos=1)
        self._client.loop_start()
        assert subscribed.wait(5)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def _publisher(
    port: int, program: ScanProgram, strategy: ProbeStrategy, host: str = "127.0.0.1"
) -> tuple[Publisher, StatusTracker]:
    leaf = Leaf((), "COUNTER", 0, 0, "DINT", 4, TOPIC)
    reader = bind_leaves(program, [leaf])
    assert not isinstance(reader, Exception) and hasattr(reader, "sample")
    tracker = StatusTracker(SystemClock())
    supervisor = Supervisor(
        ConnectSpec(host, port, "r4-probe", None),
        paho_connection,
        strategy,
        reader,  # type: ignore[arg-type]
        tracker.reset("connecting", STATUS_CONFIG),
        SystemClock(),
        Backoff(lambda: 0.0),
        SupervisorSettings(tick_interval_s=0.1),
        lambda level, message: print(f"[{level}] {message}"),
    )
    return Publisher(supervisor, "mqtt-publisher-r4"), tracker


@pytest.fixture(autouse=True)
def _settle_threads() -> Iterator[None]:
    """Let helper threads of a test end so later thread-count checks start from a quiet process."""
    yield
    end = time.monotonic() + 3.0
    previous = -1
    while time.monotonic() < end:
        current = threading.active_count()
        if current == previous and not any(t.name == "mqtt-dns" for t in threading.enumerate()):
            return
        previous = current
        time.sleep(0.3)


def _wait(predicate: object, seconds: float) -> bool:
    assert callable(predicate)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_outage_keeps_the_scan_running_and_recovery_publishes_only_current_values() -> None:
    program = ScanProgram()
    program.start()
    gate = Gate(1883)
    collector = Collector(TOPIC)
    strategy = ProbeStrategy()
    publisher, tracker = _publisher(gate.port, program, strategy)
    states: list[str] = []
    stop_watch = threading.Event()

    def watch() -> None:
        while not stop_watch.is_set():
            state = str(tracker.snapshot()["state"])
            if not states or states[-1] != state:
                states.append(state)
            time.sleep(0.02)

    threading.Thread(target=watch, daemon=True).start()
    publisher.start()
    started = time.monotonic()
    assert _wait(lambda: len(collector.values) >= 10, 5)
    cut_counter = program.counter
    cut_at = time.monotonic()
    gate.outage()
    assert _wait(lambda: tracker.snapshot()["state"] == "retry_wait", 3)
    time.sleep(4.0)
    outage_values = [v for t, v in collector.values if t > cut_at + 0.5]
    restore_counter = program.counter
    restored_at = time.monotonic()
    gate.restore()
    assert _wait(lambda: any(t > restored_at for t, _ in collector.values), 12)
    first_after = next(int(v) for t, v in collector.values if t > restored_at)
    time.sleep(1.0)
    stop_started = time.monotonic()
    assert publisher.stop() is True
    stop_elapsed = time.monotonic() - stop_started
    stop_watch.set()
    elapsed = time.monotonic() - started
    program.stop()
    collector.close()
    gate.close()

    expected_scans = elapsed / SCAN_PERIOD_S
    report = {
        "states": states,
        "scans": program.scans,
        "expected_scans_upper_bound": round(expected_scans),
        "max_scan_lock_wait_ms": round(program.max_lock_wait * 1000, 3),
        "max_scan_gap_ms": round(program.max_scan_gap * 1000, 3),
        "max_publisher_lock_hold_ms": round(program.max_reader_hold * 1000, 3),
        "counter_at_cut": cut_counter,
        "counter_at_restore": restore_counter,
        "first_value_after_restore": first_after,
        "messages_during_outage": len(outage_values),
        "stop_s": round(stop_elapsed, 3),
        "status_after_stop": tracker.snapshot()["stats"],
    }
    print("R4-E2E " + json.dumps(report, sort_keys=True))
    assert states[:2] == ["connecting", "connected"] and "retry_wait" in states
    assert states[-1] == "connected"
    assert outage_values == []
    assert first_after >= restore_counter
    assert all(int(v) <= cut_counter + 20 or int(v) >= restore_counter for _, v in collector.values)
    assert program.max_lock_wait < 0.005
    assert program.max_reader_hold < 0.005
    assert program.scans >= 0.5 * expected_scans
    assert stop_elapsed < 5.0
    assert not publisher.alive
    assert not [t for t in threading.enumerate() if t.name.startswith("mqtt-publisher")]


def test_qos1_forwarded_count_matches_what_the_broker_delivered() -> None:
    program = ScanProgram()
    program.start()
    collector = Collector(TOPIC)
    publisher, tracker = _publisher(1883, program, ProbeStrategy(qos=1), BROKER)
    publisher.start()
    time.sleep(2.0)
    assert publisher.stop() is True
    program.stop()
    time.sleep(0.5)
    collector.close()
    stats = tracker.snapshot()
    print("R4-QOS1 " + json.dumps(stats, sort_keys=True))
    published = stats["stats"]["published"]  # type: ignore[index]
    assert published >= 15
    # the final message of the intentional stop goes to a topic the collector does not follow
    assert len(collector.values) == published - 1


def _tls(identity: tuple[Path, Path] | None = None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=CA_PEM)
    if identity is not None:
        context.load_cert_chain(str(identity[0]), str(identity[1]))
    return context


@pytest.mark.parametrize(
    ("port", "kwargs", "expected"),
    [
        (8883, {"username": "spike-user", "password": PASSWORD.encode()}, "connected"),
        (8883, {"username": "spike-user", "password": b"wrong"}, "auth_rejected"),
        (8884, {}, "client_certificate_rejected"),
        (8884, {"identity": "client-ok"}, "connected"),
        (8884, {"identity": "client-rogue"}, "client_certificate_rejected"),
        (8885, {}, "broker_certificate_hostname_mismatch"),
        (8886, {}, "broker_certificate_expired"),
        (8887, {}, "broker_certificate_untrusted"),
    ],
)
def test_real_broker_failures_map_to_catalog_codes(
    port: int, kwargs: dict[str, object], expected: str
) -> None:
    identity = kwargs.pop("identity", None)
    pair = None if identity is None else (PKI / f"{identity}.crt", PKI / f"{identity}.key")
    spec = ConnectSpec(BROKER, port, f"r4-{port}", _tls(pair), **kwargs)  # type: ignore[arg-type]
    connection = PahoConnection(spec)
    try:
        connection.open(threading.Event(), 5.0)
        for _ in range(10):
            connection.service(0.05)
        outcome = "connected"
    except TransportError as exc:
        outcome = exc.code
        assert PASSWORD not in exc.message
    finally:
        connection.abort()
    client = connection.client
    cause = (
        None if client is None or client.bounded_socket is None else client.bounded_socket.failure
    )
    assert outcome == expected, repr(cause)
