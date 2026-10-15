# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Spike: bounded, cancellable transport around paho-mqtt 2.1.0.

Evidence for the transport decisions only; the product publisher is built
separately. One private paho hook is used (``Client._create_socket``); the
pinned version and the wire tests in ``spike_transport.py`` guard it.
"""

from __future__ import annotations

import errno
import os
import random
import select
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import paho.mqtt as paho_mqtt
import paho.mqtt.client as mqtt

PAHO_VERSION_PINNED = "2.1.0"
CONNECT_TIMEOUT_S = 10.0
KEEPALIVE_S = 30
SHUTDOWN_BUDGET_S = 5.0
MAX_INBOUND_REMAINING_LENGTH = 16_384
MAX_RECV_CHUNK = 16_384
MAX_OUTBOUND_PAYLOAD = 262_144
MAX_QUEUED_MESSAGES = 1_000
MAX_INFLIGHT_MESSAGES = 20
BACKOFF_BASES_S = (1, 2, 4, 8, 16, 30)
BACKOFF_JITTER = 0.20
BACKOFF_RESET_AFTER_S = 60.0

if paho_mqtt.__version__ != PAHO_VERSION_PINNED:
    raise ImportError("paho-mqtt version differs from the pinned, wire-tested one")


class ConnectCancelled(OSError):
    pass


class ConnectDeadlineExceeded(OSError):
    pass


class InboundPacketTooLarge(OSError):
    pass


def backoff_delay(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Delay before retry ``attempt`` (0-based): base plus 0-20% jitter."""
    base = BACKOFF_BASES_S[min(attempt, len(BACKOFF_BASES_S) - 1)]
    return base * (1.0 + BACKOFF_JITTER * rng())


def build_tls_context(
    ca_pem: str | None, cert_chain: tuple[bytes, bytes] | None = None
) -> ssl.SSLContext:
    """TLS 1.2+, chain and hostname verification always on; no bypass parameter exists."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if ca_pem is None:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    else:
        ctx.load_verify_locations(cadata=ca_pem)
    if cert_chain is not None:
        load_cert_chain_from_memory(ctx, cert_chain[0], cert_chain[1])
    return ctx


def load_cert_chain_from_memory(ctx: ssl.SSLContext, cert_pem: bytes, key_pem: bytes) -> None:
    """Load client identity via anonymous memfds; nothing is written to a filesystem."""
    cert_fd = os.memfd_create("mqtt-client-cert", os.MFD_CLOEXEC)
    key_fd = os.memfd_create("mqtt-client-key", os.MFD_CLOEXEC)
    try:
        os.write(cert_fd, cert_pem)
        os.write(key_fd, key_pem)
        ctx.load_cert_chain(f"/proc/self/fd/{cert_fd}", f"/proc/self/fd/{key_fd}")
    finally:
        os.close(cert_fd)
        os.close(key_fd)


class BoundedSocket:
    """Socket-like object for paho that caps recv sizes and rejects oversized inbound packets.

    Tracks MQTT framing on the inbound byte stream and raises before paho
    allocates or reads the payload of a packet whose remaining length is
    above the limit.
    """

    def __init__(self, sock: socket.socket | ssl.SSLSocket, max_remaining: int) -> None:
        self._sock = sock
        self._max_remaining = max_remaining
        self._state = "command"
        self._length = 0
        self._multiplier = 1
        self._length_bytes = 0
        self._payload_left = 0
        self.violation: str | None = None

    def _feed(self, data: bytes) -> None:
        for byte in data:
            if self._state == "command":
                self._state = "length"
                self._length = 0
                self._multiplier = 1
                self._length_bytes = 0
            elif self._state == "length":
                self._length += (byte & 0x7F) * self._multiplier
                self._multiplier *= 128
                self._length_bytes += 1
                if byte & 0x80 == 0:
                    if self._length > self._max_remaining:
                        self.violation = "inbound_packet_too_large"
                        raise InboundPacketTooLarge(errno.EMSGSIZE, "inbound packet above limit")
                    self._payload_left = self._length
                    self._state = "payload" if self._length > 0 else "command"
                elif self._length_bytes >= 4:
                    self.violation = "inbound_malformed_length"
                    raise InboundPacketTooLarge(errno.EPROTO, "malformed remaining length")
            else:
                self._payload_left -= 1
                if self._payload_left == 0:
                    self._state = "command"

    def recv(self, bufsize: int) -> bytes:
        if self.violation is not None:
            raise InboundPacketTooLarge(errno.EMSGSIZE, self.violation)
        limit = min(bufsize, MAX_RECV_CHUNK)
        if self._state == "payload":
            limit = min(limit, self._payload_left)
        data = self._sock.recv(limit)
        self._feed(data)
        return data

    def send(self, data: bytes) -> int:
        return self._sock.send(data)

    def close(self) -> None:
        self._sock.close()

    def fileno(self) -> int:
        return self._sock.fileno()

    def setblocking(self, flag: bool) -> None:
        self._sock.setblocking(flag)

    def pending(self) -> int:
        return self._sock.pending() if isinstance(self._sock, ssl.SSLSocket) else 0


@dataclass
class Endpoint:
    host: str
    port: int
    tls: ssl.SSLContext | None


def _wait(
    sock: socket.socket, readable: bool, writable: bool, deadline: float, cancel: threading.Event
) -> None:
    while True:
        if cancel.is_set():
            raise ConnectCancelled(errno.ECANCELED, "connect cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectDeadlineExceeded(errno.ETIMEDOUT, "connect deadline exceeded")
        r, w, _ = select.select(
            [sock] if readable else [], [sock] if writable else [], [], min(0.1, remaining)
        )
        if r or w:
            return


def open_connection(
    endpoint: Endpoint, cancel: threading.Event, timeout_s: float = CONNECT_TIMEOUT_S
) -> BoundedSocket:
    """TCP connect plus TLS handshake under one deadline, abortable within 100 ms."""
    deadline = time.monotonic() + timeout_s
    infos = socket.getaddrinfo(endpoint.host, endpoint.port, type=socket.SOCK_STREAM)
    last_error: OSError | None = None
    for family, socktype, proto, _canon, addr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setblocking(False)
            rc = sock.connect_ex(addr)
            if rc not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK):
                raise OSError(rc, os.strerror(rc))
            _wait(sock, False, True, deadline, cancel)
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err != 0:
                raise OSError(err, os.strerror(err))
            if endpoint.tls is None:
                return BoundedSocket(sock, MAX_INBOUND_REMAINING_LENGTH)
            tls_sock = endpoint.tls.wrap_socket(
                sock, server_hostname=endpoint.host, do_handshake_on_connect=False
            )
            while True:
                try:
                    tls_sock.do_handshake()
                    return BoundedSocket(tls_sock, MAX_INBOUND_REMAINING_LENGTH)
                except ssl.SSLWantReadError:
                    _wait(tls_sock, True, False, deadline, cancel)
                except ssl.SSLWantWriteError:
                    _wait(tls_sock, False, True, deadline, cancel)
        except (ConnectCancelled, ConnectDeadlineExceeded, ssl.SSLError):
            sock.close()
            raise
        except OSError as exc:
            sock.close()
            last_error = exc
    raise last_error if last_error is not None else OSError(errno.EHOSTUNREACH, "no address")


class GenerationClient(mqtt.Client):
    """One paho client per connection generation; discarded on any disconnect, so nothing replays."""

    def __init__(self, client_id: str, endpoint: Endpoint, cancel: threading.Event) -> None:
        super().__init__(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=False,
        )
        self._endpoint = endpoint
        self._cancel = cancel
        self.bounded_socket: BoundedSocket | None = None
        self.max_inflight_messages_set(MAX_INFLIGHT_MESSAGES)
        self.max_queued_messages_set(MAX_QUEUED_MESSAGES)

    def _create_socket(self) -> BoundedSocket:  # type: ignore[override]
        self.bounded_socket = open_connection(self._endpoint, self._cancel)
        return self.bounded_socket

    def connect_and_wait(self, timeout_s: float = CONNECT_TIMEOUT_S) -> mqtt.ReasonCode:
        """Connect and wait for CONNACK; raises on transport errors and timeout."""
        acked = threading.Event()
        result: list[mqtt.ReasonCode] = []

        def on_connect(
            _c: mqtt.Client, _u: object, _f: mqtt.ConnectFlags, rc: mqtt.ReasonCode, _p: object
        ) -> None:
            result.append(rc)
            acked.set()

        self.on_connect = on_connect
        deadline = time.monotonic() + timeout_s
        self.connect(self._endpoint.host, self._endpoint.port, keepalive=KEEPALIVE_S)
        while not acked.is_set():
            if self._cancel.is_set():
                raise ConnectCancelled(errno.ECANCELED, "connect cancelled")
            if time.monotonic() > deadline:
                raise ConnectDeadlineExceeded(errno.ETIMEDOUT, "CONNACK deadline exceeded")
            rc = self.loop(timeout=0.1)
            if acked.is_set():
                break
            if rc not in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_AGAIN):
                raise ConnectionError(f"connection lost before CONNACK: {mqtt.error_string(rc)}")
        return result[0]
