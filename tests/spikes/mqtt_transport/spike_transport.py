# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Wire evidence for transport limits, TLS and lifecycle against a real Mosquitto broker."""

from __future__ import annotations

import inspect
import os
import socket
import ssl
import threading
import time
import tracemalloc
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from transport import (
    CONNECT_TIMEOUT_S,
    MAX_INBOUND_REMAINING_LENGTH,
    SHUTDOWN_BUDGET_S,
    ConnectCancelled,
    ConnectDeadlineExceeded,
    Endpoint,
    GenerationClient,
    backoff_delay,
    build_tls_context,
    load_cert_chain_from_memory,
    open_connection,
)

BROKER = os.environ.get("SPIKE_BROKER", "broker")
PKI = Path(os.environ.get("SPIKE_PKI", "/pki"))
CA_PEM = (PKI / "ca.crt").read_text()
PASSWORD = (PKI / "password").read_text()


class Proxy:
    """TCP forwarder that a test can cut to simulate network loss."""

    def __init__(self, target_port: int) -> None:
        self._target = (BROKER, target_port)
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self.accepted = 0
        self._links: list[socket.socket] = []
        self._lock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
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

    def cut(self) -> None:
        with self._lock:
            links, self._links = self._links, []
        for s in links:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def close(self) -> None:
        self._closed = True
        self.cut()
        self._server.close()


class SilentServer:
    """Accepts TCP and never answers, to stall a TLS handshake."""

    def __init__(self) -> None:
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            self._conns.append(conn)

    def close(self) -> None:
        for c in self._conns:
            c.close()
        self._server.close()


class Collector:
    """Independent subscriber (plain paho with its own loop thread)."""

    def __init__(self, topic: str) -> None:
        self.messages: list[tuple[str, bytes]] = []
        self.got = threading.Event()
        self._c = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"collector-{time.monotonic_ns()}"
        )
        self._c.on_message = self._on_message
        subscribed = threading.Event()
        self._c.on_subscribe = lambda *_a: subscribed.set()
        self._c.connect(BROKER, 1883)
        self._c.subscribe(topic, qos=1)
        self._c.loop_start()
        assert subscribed.wait(5)

    def _on_message(self, _c: mqtt.Client, _u: object, msg: mqtt.MQTTMessage) -> None:
        self.messages.append((msg.topic, bytes(msg.payload)))
        self.got.set()

    def close(self) -> None:
        self._c.loop_stop()
        self._c.disconnect()


def _gen(
    port: int, tls: ssl.SSLContext | None, host: str = BROKER, client_id: str = "spike"
) -> GenerationClient:
    return GenerationClient(client_id, Endpoint(host, port, tls), threading.Event())


def _pump(client: mqtt.Client, seconds: float) -> int:
    end = time.monotonic() + seconds
    rc = mqtt.MQTT_ERR_SUCCESS
    while time.monotonic() < end:
        rc = client.loop(timeout=0.05)
        if rc not in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_AGAIN):
            return rc
    return rc


def test_contract_constants() -> None:
    assert CONNECT_TIMEOUT_S == 10.0
    assert SHUTDOWN_BUDGET_S == 5.0


def test_no_verification_bypass_exists() -> None:
    assert list(inspect.signature(build_tls_context).parameters) == ["ca_pem", "cert_chain"]
    ctx = build_tls_context(CA_PEM)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_tls_private_ca_and_password_accepted() -> None:
    gen = _gen(8883, build_tls_context(CA_PEM))
    gen.username_pw_set("spike-user", PASSWORD)
    rc = gen.connect_and_wait()
    assert not rc.is_failure
    gen.disconnect()


def test_wrong_password_is_an_authentication_failure() -> None:
    gen = _gen(8883, build_tls_context(CA_PEM))
    gen.username_pw_set("spike-user", "MARKER-PW-wrong")
    rc = gen.connect_and_wait()
    assert rc.is_failure and rc.getName() == "Not authorized"


@pytest.mark.parametrize(
    ("port", "verify_code"),
    [(8885, 62), (8886, 10), (8887, 20)],
    ids=["hostname-mismatch", "expired", "untrusted-ca"],
)
def test_broker_certificate_rejected_before_mqtt(port: int, verify_code: int) -> None:
    gen = _gen(port, build_tls_context(CA_PEM))
    with pytest.raises(ssl.SSLCertVerificationError) as info:
        gen.connect_and_wait()
    assert info.value.verify_code == verify_code


def test_tls_below_1_2_refused() -> None:
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(PKI / "server-ok.crt"), str(PKI / "server-ok.key"))
    server_ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    server_ctx.maximum_version = ssl.TLSVersion.TLSv1_1
    server_ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        conn, _ = listener.accept()
        try:
            server_ctx.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            pass
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(ssl.SSLError) as info:
        open_connection(
            Endpoint("127.0.0.1", port, build_tls_context(CA_PEM)), threading.Event(), 3.0
        )
    assert "PROTOCOL" in str(info.value).upper()
    listener.close()


def test_client_certificate_accepted() -> None:
    chain = ((PKI / "client-ok.crt").read_bytes(), (PKI / "client-ok.key").read_bytes())
    gen = _gen(8884, build_tls_context(CA_PEM, chain))
    assert not gen.connect_and_wait().is_failure
    gen.disconnect()


@pytest.mark.parametrize("identity", [None, "client-rogue"], ids=["missing", "untrusted"])
def test_client_certificate_required_by_broker(identity: str | None) -> None:
    chain = None
    if identity is not None:
        chain = ((PKI / f"{identity}.crt").read_bytes(), (PKI / f"{identity}.key").read_bytes())
    gen = _gen(8884, build_tls_context(CA_PEM, chain))
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        gen.connect_and_wait(timeout_s=5.0)
    assert not gen.is_connected()


def test_mismatched_private_key_rejected_before_any_connection() -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with pytest.raises(ssl.SSLError) as info:
        load_cert_chain_from_memory(
            ctx, (PKI / "client-ok.crt").read_bytes(), (PKI / "client-mismatch.key").read_bytes()
        )
    assert "KEY_VALUES_MISMATCH" in str(info.value)


def test_client_identity_never_touches_a_filesystem() -> None:
    before = set(os.listdir("/proc/self/fd"))
    tmp_before = set(os.listdir("/tmp"))
    build_tls_context(
        CA_PEM, ((PKI / "client-ok.crt").read_bytes(), (PKI / "client-ok.key").read_bytes())
    )
    leftovers = [
        os.readlink(f"/proc/self/fd/{fd}")
        for fd in set(os.listdir("/proc/self/fd")) - before
        if os.path.exists(f"/proc/self/fd/{fd}")
    ]
    assert not [link for link in leftovers if "mqtt-client" in link]
    assert set(os.listdir("/tmp")) == tmp_before


def test_handshake_stall_hits_the_connect_deadline() -> None:
    silent = SilentServer()
    start = time.monotonic()
    with pytest.raises(ConnectDeadlineExceeded):
        open_connection(
            Endpoint("127.0.0.1", silent.port, build_tls_context(CA_PEM)), threading.Event()
        )
    elapsed = time.monotonic() - start
    silent.close()
    assert CONNECT_TIMEOUT_S <= elapsed < CONNECT_TIMEOUT_S + 0.5


@pytest.mark.parametrize("target", ["handshake", "tcp"])
def test_stop_cancels_a_pending_connect_quickly(target: str) -> None:
    silent = SilentServer()
    endpoint = (
        Endpoint("127.0.0.1", silent.port, build_tls_context(CA_PEM))
        if target == "handshake"
        else Endpoint("10.255.255.1", 1883, None)
    )
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    start = time.monotonic()
    with pytest.raises((ConnectCancelled, OSError)) as info:
        open_connection(endpoint, cancel)
    elapsed = time.monotonic() - start
    silent.close()
    if target == "tcp" and not isinstance(info.value, ConnectCancelled):
        pytest.skip(f"network answered the unroutable address immediately: {info.value}")
    assert isinstance(info.value, ConnectCancelled)
    assert elapsed < 0.5 + 0.25


def test_backoff_schedule_with_jitter_bounds() -> None:
    assert [backoff_delay(i, lambda: 0.0) for i in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert [round(backoff_delay(i, lambda: 1.0), 6) for i in range(7)] == [
        1.2,
        2.4,
        4.8,
        9.6,
        19.2,
        36.0,
        36.0,
    ]
    for i in range(200):
        base = [1, 2, 4, 8, 16, 30][min(i % 9, 5)]
        assert base <= backoff_delay(i % 9) <= base * 1.2


def test_paho_never_reconnects_on_its_own() -> None:
    proxy = Proxy(1883)
    gen = _gen(proxy.port, None, host="127.0.0.1")
    assert not gen.connect_and_wait().is_failure
    proxy.cut()
    rc = _pump(gen, 2.0)
    assert rc != mqtt.MQTT_ERR_SUCCESS
    _pump(gen, 2.0)
    assert proxy.accepted == 1
    proxy.close()


def test_oversized_inbound_packet_contained_before_allocation() -> None:
    gen = _gen(1883, None, client_id="spike-oversize")
    assert not gen.connect_and_wait().is_failure
    gen.subscribe("spike/ncmd", qos=0)
    _pump(gen, 0.5)
    sender = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="spike-sender")
    sender.connect(BROKER, 1883)
    sender.loop_start()
    sender.publish("spike/ncmd", b"x" * (1024 * 1024), qos=1).wait_for_publish(5)

    tracemalloc.start()
    rc = _pump(gen, 3.0)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert rc != mqtt.MQTT_ERR_SUCCESS
    assert (
        gen.bounded_socket is not None
        and gen.bounded_socket.violation == "inbound_packet_too_large"
    )
    assert peak < 4 * MAX_INBOUND_REMAINING_LENGTH

    control = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="spike-control")
    received = threading.Event()
    control.on_message = lambda *_a: received.set()
    control.connect(BROKER, 1883)
    control.subscribe("spike/ncmd", qos=0)
    _pump(control, 0.5)
    sender.publish("spike/ncmd", b"x" * (1024 * 1024), qos=1).wait_for_publish(5)
    tracemalloc.start()
    end = time.monotonic() + 5
    while not received.is_set() and time.monotonic() < end:
        control.loop(timeout=0.05)
    _, control_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert received.is_set() and control_peak > 1024 * 1024
    control.disconnect()
    sender.loop_stop()
    sender.disconnect()


def test_no_offline_replay_with_generation_discard() -> None:
    collector = Collector("spike/v/#")
    proxy = Proxy(1883)
    gen1 = _gen(proxy.port, None, host="127.0.0.1", client_id="spike-replay")
    assert not gen1.connect_and_wait().is_failure
    gen1.publish("spike/v/a", b"current-1", qos=1)
    _pump(gen1, 0.5)
    proxy.cut()
    _pump(gen1, 0.5)
    stale = gen1.publish("spike/v/a", b"stale", qos=1)
    assert stale.rc == mqtt.MQTT_ERR_NO_CONN

    gen2 = _gen(proxy.port, None, host="127.0.0.1", client_id="spike-replay")
    assert not gen2.connect_and_wait().is_failure
    gen2.publish("spike/v/a", b"current-2", qos=1)
    _pump(gen2, 1.0)
    time.sleep(0.5)
    assert [p for _, p in collector.messages] == [b"current-1", b"current-2"]

    gen2.disconnect()
    collector.messages.clear()
    assert gen1.reconnect() == mqtt.MQTT_ERR_SUCCESS
    _pump(gen1, 1.0)
    time.sleep(0.5)
    assert b"stale" in [
        p for _, p in collector.messages
    ], "negative control: reused paho client replays"
    gen1.disconnect()
    proxy.close()
    collector.close()


def test_outbound_queue_is_bounded() -> None:
    gen = _gen(1883, None, client_id="spike-queue")
    gen.max_queued_messages_set(5)
    results = [gen.publish("spike/q", b"v", qos=1).rc for _ in range(7)]
    assert results[:5] == [mqtt.MQTT_ERR_NO_CONN] * 5
    assert results[5:] == [mqtt.MQTT_ERR_QUEUE_SIZE] * 2


def test_will_published_on_connection_loss_and_death_on_stop() -> None:
    collector = Collector("spBv1.0/G/NDEATH/#")
    proxy = Proxy(1883)
    gen = _gen(proxy.port, None, host="127.0.0.1", client_id="spike-will")
    gen.will_set("spBv1.0/G/NDEATH/E", b"will-death", qos=1, retain=False)
    assert not gen.connect_and_wait().is_failure
    proxy.cut()
    assert collector.got.wait(3), "broker must publish the Will on connection loss"
    assert collector.messages[-1] == ("spBv1.0/G/NDEATH/E", b"will-death")

    collector.messages.clear()
    collector.got.clear()
    baseline = threading.active_count()
    stop = threading.Event()
    gen2 = _gen(1883, None, client_id="spike-stop")
    gen2.will_set("spBv1.0/G/NDEATH/E", b"will-death-2", qos=1, retain=False)
    assert not gen2.connect_and_wait().is_failure

    def worker() -> None:
        while not stop.is_set():
            gen2.loop(timeout=0.1)
        gen2.publish("spBv1.0/G/NDEATH/E", b"intentional-death", qos=0).wait_for_publish(1)
        gen2.disconnect()
        gen2.loop(timeout=0.1)

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.5)
    start = time.monotonic()
    stop.set()
    thread.join(SHUTDOWN_BUDGET_S)
    elapsed = time.monotonic() - start
    assert not thread.is_alive() and elapsed < SHUTDOWN_BUDGET_S
    assert threading.active_count() == baseline
    assert collector.got.wait(3)
    time.sleep(0.5)
    assert [p for _, p in collector.messages] == [b"intentional-death"]
    proxy.close()
    collector.close()
