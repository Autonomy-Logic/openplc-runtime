# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""paho-mqtt session against an in-process broker: limits, failures, cancellation, no replay."""

from __future__ import annotations

import errno
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from fake_broker import FakeBroker, packet, publish_packet
from mqtt_pki import Pair, Pki
from mqtt_publisher import transport
from mqtt_publisher.connection import (
    ConnectSpec,
    PublishResult,
    TransportCancelled,
    TransportError,
)
from mqtt_publisher.strategy import OutboundMessage
from mqtt_publisher.transport import (
    MAX_INBOUND_REMAINING_LENGTH,
    BoundedSocket,
    ConnectCancelled,
    DnsDeadlineExceeded,
    PahoConnection,
    classify,
    resolve,
)

PASSWORD = b"MARKER-PW-transport-5e1a"


@pytest.fixture(scope="module")
def pki() -> Pki:
    return Pki()


@pytest.fixture
def broker() -> Iterator[FakeBroker]:
    server = FakeBroker()
    yield server
    server.close()


def _spec(port: int, **extra: object) -> ConnectSpec:
    base: dict[str, object] = {
        "host": "127.0.0.1",
        "port": port,
        "client_id": "plc-linha1",
        "tls_context": None,
    }
    base.update(extra)
    return ConnectSpec(**base)  # type: ignore[arg-type]


def _open(port: int, timeout_s: float = 5.0, **extra: object) -> PahoConnection:
    connection = PahoConnection(_spec(port, **extra))
    connection.open(threading.Event(), timeout_s)
    return connection


def _pump(connection: PahoConnection, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        connection.service(0.02)


def _wait_for(predicate: object, seconds: float = 5.0) -> bool:
    assert callable(predicate)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _message(
    topic: str = "openplc/plc-linha1/v", payload: bytes = b'{"value":1}', qos: int = 0
) -> OutboundMessage:
    return OutboundMessage(topic, payload, qos, False)  # type: ignore[arg-type]


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


# Session basics


def test_clean_session_credentials_and_will_reach_the_broker(broker: FakeBroker) -> None:
    will = OutboundMessage("spBv1.0/g/NDEATH/e", b"death", 1, False)
    connection = _open(broker.port, username="plc", password=PASSWORD, will=will, keepalive_s=7)
    connect = broker.connects[0]
    assert connect.clean_session and connect.keepalive == 7
    assert (connect.client_id, connect.username, connect.password) == (
        "plc-linha1",
        "plc",
        PASSWORD,
    )
    assert (connect.will_topic, connect.will_payload, connect.will_qos) == (will.topic, b"death", 1)
    assert PASSWORD.decode() not in repr(_spec(1, password=PASSWORD))
    connection.close((), 2.0)


def test_qos0_counts_as_forwarded_once_written(broker: FakeBroker) -> None:
    connection = _open(broker.port)
    assert connection.publish(_message()) is PublishResult.ACCEPTED
    _pump(connection, 0.1)
    assert connection.drain().forwarded == 1
    assert _wait_for(lambda: len(broker.publishes) == 1)
    assert broker.publishes[0].qos == 0 and broker.publishes[0].payload == b'{"value":1}'
    assert connection.close((), 2.0) == 0


def test_qos1_is_forwarded_only_after_the_broker_acknowledges() -> None:
    silent = FakeBroker(puback=False)
    acking = FakeBroker(puback=True)
    try:
        unacked = _open(silent.port)
        acked = _open(acking.port)
        for connection in (unacked, acked):
            assert connection.publish(_message(qos=1)) is PublishResult.ACCEPTED
            _pump(connection, 0.3)
        assert unacked.drain().forwarded == 0
        assert acked.drain().forwarded == 1
        assert unacked.abort() == 1
        assert acked.close((), 2.0) == 0
    finally:
        silent.close()
        acking.close()


def test_lost_session_reports_unforwarded_messages_and_never_replays_them() -> None:
    server = FakeBroker(puback=False)
    try:
        first = _open(server.port)
        for value in range(3):
            first.publish(_message(payload=f'{{"value":{value}}}'.encode(), qos=1))
        _pump(first, 0.2)
        assert _wait_for(lambda: len(server.publishes) == 3)
        server.cut()
        with pytest.raises(TransportError) as info:
            _pump(first, 2.0)
        assert info.value.code == "network_connection_lost"
        assert first.abort() == 3
        second = _open(server.port)
        _pump(second, 0.3)
        assert len(server.publishes) == 3
        assert all(not p.payload.endswith(b"}") or p.packet_id for p in server.publishes)
        second.close((), 2.0)
    finally:
        server.close()


def test_close_flushes_final_messages_then_disconnects(broker: FakeBroker) -> None:
    connection = _open(broker.port)
    start = time.monotonic()
    lost = connection.close((_message("state", b"offline", 1),), 2.0)
    assert lost == 0 and time.monotonic() - start < 2.0
    assert _wait_for(lambda: broker.disconnects == 1)
    assert [p.topic for p in broker.publishes] == ["state"]
    assert connection.publish(_message()) is PublishResult.NOT_CONNECTED


def test_subscription_and_bounded_inbound_queue() -> None:
    flood = b"".join(
        publish_packet("ctl/in", b"x") for _ in range(transport.MAX_INBOUND_MESSAGES + 4)
    )
    server = FakeBroker(after_connack=flood)
    try:
        connection = _open(server.port)
        connection.subscribe("ctl/in", 0)
        _pump(connection, 0.3)
        drained = connection.drain()
        assert len(drained.inbound) == transport.MAX_INBOUND_MESSAGES
        assert drained.inbound_dropped == 4
        assert drained.inbound[0].topic == "ctl/in"
        assert server.subscriptions == [("ctl/in", 0)]
        connection.close((), 2.0)
    finally:
        server.close()


def test_payload_above_the_contract_limit_is_refused(broker: FakeBroker) -> None:
    connection = _open(broker.port)
    assert connection.publish(_message(payload=b"x" * 262_145)) is PublishResult.TOO_LARGE
    assert connection.publish(_message(payload=b"x" * 262_144)) is PublishResult.ACCEPTED
    connection.close((), 2.0)


def test_slow_broker_fills_a_bounded_backlog() -> None:
    server = FakeBroker(read_after_connack=False)
    try:
        connection = PahoConnection(
            _spec(server.port), max_backlog_messages=50, max_backlog_bytes=10**9
        )
        connection.open(threading.Event(), 5.0)
        payload = b"x" * 60_000
        results = [connection.publish(_message(payload=payload)) for _ in range(400)]
        assert results.count(PublishResult.BACKLOG_FULL) > 0
        assert results.count(PublishResult.ACCEPTED) <= 50 + 60
        assert connection.backlog_full()
        assert connection.abort() > 0
    finally:
        server.close()


def test_backlog_is_bounded_by_bytes_too() -> None:
    server = FakeBroker(read_after_connack=False)
    try:
        connection = PahoConnection(
            _spec(server.port), max_backlog_messages=10**6, max_backlog_bytes=300_000
        )
        connection.open(threading.Event(), 5.0)
        results = [connection.publish(_message(payload=b"y" * 100_000)) for _ in range(200)]
        accepted = results.count(PublishResult.ACCEPTED)
        assert results.count(PublishResult.BACKLOG_FULL) == 200 - accepted
        assert connection.backlog_full()
        connection.abort()
    finally:
        server.close()


# Failures map to catalog codes


@pytest.mark.parametrize(
    ("rc", "code"),
    [
        (1, "auth_broker_unavailable"),
        (2, "auth_client_id_rejected"),
        (3, "auth_broker_unavailable"),
        (4, "auth_rejected"),
        (5, "auth_rejected"),
    ],
)
def test_refused_connack_maps_to_an_authentication_code(rc: int, code: str) -> None:
    server = FakeBroker(connack_rc=rc)
    try:
        connection = PahoConnection(_spec(server.port, username="u", password=PASSWORD))
        with pytest.raises(TransportError) as info:
            connection.open(threading.Event(), 5.0)
        assert info.value.code == code
        assert PASSWORD.decode() not in str(info.value)
        connection.abort()
        time.sleep(0.2)
        assert server.accepted == 1 and len(server.connects) == 1
    finally:
        server.close()


def test_refused_tcp_connection() -> None:
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]
    connection = PahoConnection(_spec(port))
    with pytest.raises(TransportError) as info:
        connection.open(threading.Event(), 5.0)
    assert info.value.code == "network_connection_refused"
    connection.abort()


def test_silent_broker_hits_the_connect_deadline() -> None:
    server = FakeBroker(connack_rc=None)
    try:
        connection = PahoConnection(_spec(server.port))
        start = time.monotonic()
        with pytest.raises(TransportError) as info:
            connection.open(threading.Event(), 0.6)
        assert info.value.code == "network_connect_timeout"
        assert 0.6 <= time.monotonic() - start < 1.2
        connection.abort()
    finally:
        server.close()


@pytest.mark.parametrize("stage", ["connack", "handshake"])
def test_stop_cancels_a_pending_connection_quickly(stage: str, pki: Pki) -> None:
    if stage == "connack":
        server = FakeBroker(connack_rc=None)
        spec = _spec(server.port)
        stall: socket.socket | None = None
    else:
        server = FakeBroker()
        stall = socket.create_server(("127.0.0.1", 0))
        context = ssl.create_default_context(cadata=pki.broker_ca_pem.decode())
        spec = _spec(stall.getsockname()[1], tls_context=context)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    connection = PahoConnection(spec)
    start = time.monotonic()
    try:
        with pytest.raises(TransportCancelled):
            connection.open(cancel, 10.0)
        assert time.monotonic() - start < 0.3 + 0.2
    finally:
        connection.abort()
        server.close()
        if stall is not None:
            stall.close()


def test_keepalive_without_answer_is_a_keepalive_timeout() -> None:
    server = FakeBroker(answer_ping=False)
    try:
        connection = _open(server.port, keepalive_s=1)
        with pytest.raises(TransportError) as info:
            _pump(connection, 4.0)
        assert info.value.code == "network_keepalive_timeout"
        assert server.pings >= 1
        connection.abort()
    finally:
        server.close()


def test_oversized_inbound_packet_is_refused_before_its_payload_is_read() -> None:
    oversized = bytes([0x30]) + bytes([0x80, 0x80, 0x10])
    server = FakeBroker(after_connack=oversized)
    try:
        connection = _open(server.port)
        with pytest.raises(TransportError) as info:
            _pump(connection, 2.0)
        assert info.value.code == "inbound_packet_too_large"
        connection.abort()
    finally:
        server.close()


def test_bounded_socket_limits_reads_and_rejects_malformed_lengths() -> None:
    left, right = socket.socketpair()
    try:
        bounded = BoundedSocket(left, MAX_INBOUND_REMAINING_LENGTH)
        right.sendall(packet(0x30, b"\x00\x01t" + b"p" * 20_000)[:4])
        with pytest.raises(OSError):
            while True:
                bounded.recv(65_536)
        assert bounded.failure is not None
        malformed_left, malformed_right = socket.socketpair()
        malformed = BoundedSocket(malformed_left, MAX_INBOUND_REMAINING_LENGTH)
        malformed_right.sendall(bytes([0x30, 0xFF, 0xFF, 0xFF, 0xFF, 0x01]))
        with pytest.raises(OSError):
            while True:
                malformed.recv(16)
        malformed_left.close()
        malformed_right.close()
    finally:
        left.close()
        right.close()


# TLS failures


@contextmanager
def _tls_broker(
    pki: Pki,
    server: Pair,
    client_ca: bytes | None = None,
    max_version: ssl.TLSVersion | None = None,
) -> Iterator[FakeBroker]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory() as directory:
        cert = pki.write(Path(directory), "server.crt", server.cert_pem)
        key = pki.write(Path(directory), "server.key", server.key_pem)
        context.load_cert_chain(cert, key)
    if client_ca is not None:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=client_ca.decode())
    if max_version is not None:
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        context.maximum_version = max_version
    fake = FakeBroker(tls=context)
    try:
        yield fake
    finally:
        fake.close()


def _client_context(pki: Pki, identity: Pair | None = None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=pki.broker_ca_pem.decode())
    if identity is not None:
        with tempfile.TemporaryDirectory() as directory:
            cert = pki.write(Path(directory), "c.crt", identity.cert_pem)
            key = pki.write(Path(directory), "c.key", identity.key_pem)
            context.load_cert_chain(cert, key)
    return context


def _tls_failure(port: int, context: ssl.SSLContext) -> str:
    connection = PahoConnection(_spec(port, host="localhost", tls_context=context))
    try:
        connection.open(threading.Event(), 5.0)
    except TransportError as exc:
        return exc.code
    finally:
        connection.abort()
    return "connected"


@pytest.mark.parametrize(
    ("server", "code"),
    [
        ("server_ok", "connected"),
        ("server_wrong_host", "broker_certificate_hostname_mismatch"),
        ("server_expired", "broker_certificate_expired"),
        ("server_untrusted", "broker_certificate_untrusted"),
    ],
)
def test_broker_certificate_failures_are_classified(pki: Pki, server: str, code: str) -> None:
    with _tls_broker(pki, getattr(pki, server)) as fake:
        assert _tls_failure(fake.port, _client_context(pki)) == code


def test_tls_below_1_2_is_classified(pki: Pki) -> None:
    with _tls_broker(pki, pki.server_ok, max_version=ssl.TLSVersion.TLSv1_1) as fake:
        assert _tls_failure(fake.port, _client_context(pki)) == "tls_protocol_unsupported"


@pytest.mark.parametrize(
    ("identity", "code"),
    [
        (None, "client_certificate_rejected"),
        ("client_rogue", "client_certificate_rejected"),
        ("client_ok", "connected"),
    ],
)
def test_client_certificate_required_by_the_broker(
    pki: Pki, identity: str | None, code: str
) -> None:
    pair = None if identity is None else getattr(pki, identity)
    with _tls_broker(pki, pki.server_ok, client_ca=pki.client_ca_pem) as fake:
        assert _tls_failure(fake.port, _client_context(pki, pair)) == code


def test_classify_covers_the_remaining_failures() -> None:
    assert classify(socket.gaierror(socket.EAI_NONAME, "x")).code == "network_dns_failure"
    assert classify(DnsDeadlineExceeded(errno.ETIMEDOUT, "x")).code == "network_dns_failure"
    assert classify(OSError(errno.EHOSTUNREACH, "x")).code == "network_unreachable"
    assert classify(OSError(errno.ENETUNREACH, "x")).code == "network_unreachable"
    assert classify(OSError(errno.ETIMEDOUT, "x")).code == "network_connect_timeout"
    assert classify(ConnectionResetError(errno.ECONNRESET, "x")).code == "network_connection_lost"
    assert classify(ssl.SSLEOFError(8, "x")).code == "network_connection_lost"
    assert classify(ssl.SSLZeroReturnError(6, "x")).code == "network_connection_lost"
    assert classify(ValueError("x")).code == "internal_error"
    for exc in (OSError(errno.EHOSTUNREACH, "secret-host"), socket.gaierror(-2, "secret-host")):
        assert "secret-host" not in classify(exc).message


# Name resolution


def test_resolution_runs_aside_and_stop_abandons_it() -> None:
    release = threading.Event()
    calls: list[str] = []

    def blocking_lookup(host: str, port: int, **_kw: object) -> list[object]:
        calls.append(host)
        release.wait(5)
        return [("late",)]

    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    start = time.monotonic()
    with pytest.raises(ConnectCancelled):
        resolve("broker.example.net", 1883, cancel, time.monotonic() + 10, blocking_lookup)  # type: ignore[arg-type]
    assert time.monotonic() - start < 0.2 + 0.15
    release.set()
    assert calls == ["broker.example.net"]


def test_resolution_deadline_and_failures() -> None:
    hang = threading.Event()

    def never(host: str, port: int, **_kw: object) -> list[object]:
        hang.wait(5)
        return []

    with pytest.raises(DnsDeadlineExceeded):
        resolve("slow.example", 1, threading.Event(), time.monotonic() + 0.2, never)  # type: ignore[arg-type]
    hang.set()

    def failing(host: str, port: int, **_kw: object) -> list[object]:
        raise socket.gaierror(socket.EAI_NONAME, "unknown")

    with pytest.raises(socket.gaierror):
        resolve("nx.example", 1, threading.Event(), time.monotonic() + 1, failing)  # type: ignore[arg-type]

    def bad_idna(host: str, port: int, **_kw: object) -> list[object]:
        raise UnicodeError("label too long")

    with pytest.raises(socket.gaierror):
        resolve("x" * 70 + ".example", 1, threading.Event(), time.monotonic() + 1, bad_idna)  # type: ignore[arg-type]


def test_ip_literals_skip_the_resolver_thread() -> None:
    before = threading.active_count()
    result = resolve("127.0.0.1", 1883, threading.Event(), time.monotonic() + 1)
    assert result[0][4][0] == "127.0.0.1" and threading.active_count() == before


# Late callbacks and resources


def test_callbacks_from_a_discarded_session_have_no_effect(broker: FakeBroker) -> None:
    connection = _open(broker.port)
    client = connection.client
    assert client is not None
    connection.abort()
    message = mqtt.MQTTMessage(topic=b"ctl/in")
    message.payload = b"late"
    assert client.on_message is not None and client.on_disconnect is not None
    client.on_message(client, None, message)
    client.on_disconnect(
        client,
        None,
        mqtt.DisconnectFlags(False),
        mqtt.ReasonCode(mqtt.PacketTypes.DISCONNECT, "Keep alive timeout"),
        None,
    )
    assert connection.drain().inbound == ()
    assert connection.publish(_message()) is PublishResult.NOT_CONNECTED
    with pytest.raises(TransportError):
        connection.service(0.01)


def test_sessions_release_their_sockets(broker: FakeBroker) -> None:
    _open(broker.port).abort()
    before = _open_fds()
    for _ in range(20):
        _open(broker.port).abort()
        _open(broker.port).close((), 1.0)
    assert _open_fds() <= before + 2


def _client_threads(before: set[threading.Thread]) -> set[str]:
    """New threads started by paho or the transport; unrelated test-process threads are ignored."""
    owners = ("paho", "mqtt_publisher")
    return {
        t.name
        for t in set(threading.enumerate()) - before
        if t.name.startswith("paho")
        or getattr(getattr(t, "_target", None), "__module__", "").startswith(owners)
    }


def test_paho_is_never_left_to_reconnect_or_run_threads(broker: FakeBroker) -> None:
    before = set(threading.enumerate())
    connection = _open(broker.port)
    broker.cut()
    with pytest.raises(TransportError):
        _pump(connection, 2.0)
    connection.abort()
    time.sleep(0.3)
    assert broker.accepted == 1
    assert _client_threads(before) <= {"mqtt-dns"}


def test_import_refuses_another_paho_version() -> None:
    plugin_dir = Path(transport.__file__).resolve().parents[1]
    probe = (
        "import sys; sys.path[:0] = [sys.argv[1], sys.argv[2]];"
        "import paho.mqtt as p; p.__version__ = '2.1.1';"
        "\ntry:\n    import mqtt_publisher.transport\nexcept ImportError as e:\n    print('refused', e)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(plugin_dir), str(plugin_dir.parent)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("refused")
