# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""paho-mqtt 2.1.0 session for the supervisor: bounded, cancellable, never reused.

One paho client per session, driven only by ``loop`` from the supervisor
thread, with paho's own reconnection disabled. The socket comes from
``open_connection``: name resolution on a helper thread, non-blocking TCP
connect and TLS handshake under one deadline, cancellable within
``POLL_S``. ``BoundedSocket`` follows the MQTT framing and refuses a packet
whose remaining length is above the inbound limit before paho reads it.
Two private paho hooks are used (``_create_socket``, ``_reset_sockets``); the
pinned version is checked on import.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import select
import socket
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Final

import paho.mqtt as paho_mqtt
import paho.mqtt.client as mqtt
from contract.constants import MAX_PAYLOAD_BYTES

from .connection import (
    ConnectSpec,
    Drained,
    PublishResult,
    TransportCancelled,
    TransportError,
    transport_error,
)
from .strategy import InboundMessage, OutboundMessage

PAHO_VERSION_PINNED: Final = "2.1.0"
if getattr(paho_mqtt, "__version__", None) != PAHO_VERSION_PINNED:
    raise ImportError("paho-mqtt differs from the pinned, wire-tested version")
if not all(hasattr(mqtt.Client, name) for name in ("_create_socket", "_reset_sockets")):
    raise ImportError("paho-mqtt lacks the socket hooks the transport relies on")

MAX_INBOUND_REMAINING_LENGTH: Final = 16_384
MAX_RECV_CHUNK: Final = 16_384
MAX_QUEUED_MESSAGES: Final = 1_000
MAX_INFLIGHT_MESSAGES: Final = 20
MAX_BACKLOG_MESSAGES: Final = 1_000
MAX_BACKLOG_BYTES: Final = 2 * 1024 * 1024
MAX_INBOUND_MESSAGES: Final = 16
POLL_S: Final = 0.05

AddrInfo = tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]
Lookup = Callable[..., list[AddrInfo]]

_RETRYABLE_IO: Final = (
    ssl.SSLWantReadError,
    ssl.SSLWantWriteError,
    BlockingIOError,
    InterruptedError,
)
_VERIFY_CODES: Final[dict[int, str]] = {
    10: "broker_certificate_expired",
    62: "broker_certificate_hostname_mismatch",
    64: "broker_certificate_hostname_mismatch",
    2: "broker_certificate_untrusted",
    18: "broker_certificate_untrusted",
    19: "broker_certificate_untrusted",
    20: "broker_certificate_untrusted",
    21: "broker_certificate_untrusted",
    27: "broker_certificate_untrusted",
}
_TLS_REASONS: Final[tuple[tuple[str, str], ...]] = (
    ("CERTIFICATE_EXPIRED", "client_certificate_expired"),
    ("CERTIFICATE_REQUIRED", "client_certificate_rejected"),
    ("BAD_CERTIFICATE", "client_certificate_rejected"),
    ("UNKNOWN_CA", "client_certificate_rejected"),
    ("CERTIFICATE_UNKNOWN", "client_certificate_rejected"),
    ("CERTIFICATE_REVOKED", "client_certificate_rejected"),
    ("ACCESS_DENIED", "client_certificate_rejected"),
)
_CONNACK_CODES: Final[dict[str, str]] = {
    "Unsupported protocol version": "auth_broker_unavailable",
    "Client identifier not valid": "auth_client_id_rejected",
    "Server unavailable": "auth_broker_unavailable",
    "Bad user name or password": "auth_rejected",
    "Not authorized": "auth_rejected",
}
_TLS_CLOSED: Final = (ssl.SSLEOFError, ssl.SSLZeroReturnError, ssl.SSLSyscallError)
_UNREACHABLE: Final = frozenset({errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL})


class ConnectCancelled(OSError):
    pass


class ConnectDeadlineExceeded(OSError):
    pass


class DnsDeadlineExceeded(OSError):
    pass


class InboundPacketTooLarge(OSError):
    pass


def classify(exc: BaseException) -> TransportError:
    """Map a connection failure to a catalog code; the message never carries exception text."""
    if isinstance(exc, InboundPacketTooLarge):
        return transport_error("inbound_packet_too_large")
    if isinstance(exc, (socket.gaierror, DnsDeadlineExceeded)):
        return transport_error("network_dns_failure")
    if isinstance(exc, ConnectDeadlineExceeded):
        return transport_error("network_connect_timeout")
    if isinstance(exc, ssl.SSLCertVerificationError):
        return transport_error(_VERIFY_CODES.get(exc.verify_code, "broker_certificate_invalid"))
    if isinstance(exc, ssl.SSLError):
        reason = str(getattr(exc, "reason", None) or "")
        for marker, code in _TLS_REASONS:
            if marker in reason:
                return transport_error(code)
        if isinstance(exc, _TLS_CLOSED) or "UNEXPECTED_EOF" in reason:
            return transport_error("network_connection_lost")
        return transport_error("tls_protocol_unsupported")
    if isinstance(exc, ConnectionRefusedError):
        return transport_error("network_connection_refused")
    if isinstance(exc, OSError):
        if exc.errno in _UNREACHABLE:
            return transport_error("network_unreachable")
        if exc.errno == errno.ETIMEDOUT:
            return transport_error("network_connect_timeout")
        return transport_error("network_connection_lost")
    return transport_error("internal_error")


def resolve(
    host: str,
    port: int,
    cancel: threading.Event,
    deadline: float,
    lookup: Lookup = socket.getaddrinfo,
) -> list[AddrInfo]:
    """Resolve on a helper thread; an abandoned lookup finishes alone and its result is dropped."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return lookup(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    box: list[list[AddrInfo] | OSError] = []
    done = threading.Event()

    def work() -> None:
        try:
            box.append(lookup(host, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            box.append(exc)
        except UnicodeError:
            box.append(socket.gaierror(socket.EAI_NONAME, "invalid host name"))
        finally:
            done.set()

    threading.Thread(target=work, name="mqtt-dns", daemon=True).start()
    while not done.wait(POLL_S):
        if cancel.is_set():
            raise ConnectCancelled(errno.ECANCELED, "connect cancelled")
        if time.monotonic() >= deadline:
            raise DnsDeadlineExceeded(errno.ETIMEDOUT, "name resolution deadline exceeded")
    result = box[0]
    if isinstance(result, OSError):
        raise result
    return result


class BoundedSocket:
    """Socket for paho that caps reads and refuses oversized inbound packets before reading them."""

    def __init__(self, sock: socket.socket | ssl.SSLSocket, max_remaining: int) -> None:
        self._sock = sock
        self._max_remaining = max_remaining
        self._state = "command"
        self._length = 0
        self._multiplier = 1
        self._length_bytes = 0
        self._payload_left = 0
        self.failure: OSError | None = None

    def _violation(self, number: int, text: str) -> InboundPacketTooLarge:
        error = InboundPacketTooLarge(number, text)
        self.failure = error
        return error

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
                        raise self._violation(errno.EMSGSIZE, "inbound packet above limit")
                    self._payload_left = self._length
                    self._state = "payload" if self._length > 0 else "command"
                elif self._length_bytes >= 4:
                    raise self._violation(errno.EPROTO, "malformed remaining length")
            else:
                self._payload_left -= 1
                if self._payload_left == 0:
                    self._state = "command"

    def recv(self, bufsize: int) -> bytes:
        if isinstance(self.failure, InboundPacketTooLarge):
            raise self.failure
        limit = min(bufsize, MAX_RECV_CHUNK)
        if self._state == "payload":
            limit = min(limit, self._payload_left)
        try:
            data = self._sock.recv(limit)
        except _RETRYABLE_IO:
            raise
        except OSError as exc:
            self.failure = exc
            raise
        self._feed(data)
        return data

    def send(self, data: bytes) -> int:
        try:
            return self._sock.send(data)
        except _RETRYABLE_IO:
            raise
        except OSError as exc:
            self.failure = self._peer_alert() or exc
            raise

    def _peer_alert(self) -> ssl.SSLError | None:
        """A TLS alert the peer sent before closing, which a failed write would otherwise hide."""
        if not isinstance(self._sock, ssl.SSLSocket):
            return None
        try:
            self._sock.recv(1)
        except ssl.SSLError as exc:
            if "ALERT" in str(getattr(exc, "reason", None) or ""):
                return exc
        except (OSError, ValueError):
            pass
        return None

    def close(self) -> None:
        self._sock.close()

    def fileno(self) -> int:
        return self._sock.fileno()

    def setblocking(self, flag: bool) -> None:
        self._sock.setblocking(flag)

    def pending(self) -> int:
        return self._sock.pending() if isinstance(self._sock, ssl.SSLSocket) else 0


def _wait(
    sock: socket.socket, readable: bool, writable: bool, deadline: float, cancel: threading.Event
) -> None:
    while True:
        if cancel.is_set():
            raise ConnectCancelled(errno.ECANCELED, "connect cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectDeadlineExceeded(errno.ETIMEDOUT, "connect deadline exceeded")
        ready = select.select(
            [sock] if readable else [], [sock] if writable else [], [], min(POLL_S, remaining)
        )
        if ready[0] or ready[1]:
            return


def _handshake(tls_sock: ssl.SSLSocket, deadline: float, cancel: threading.Event) -> None:
    while True:
        try:
            tls_sock.do_handshake()
            return
        except ssl.SSLWantReadError:
            _wait(tls_sock, True, False, deadline, cancel)
        except ssl.SSLWantWriteError:
            _wait(tls_sock, False, True, deadline, cancel)


def open_connection(
    host: str,
    port: int,
    tls: ssl.SSLContext | None,
    cancel: threading.Event,
    deadline: float,
) -> BoundedSocket:
    """TCP connect plus TLS handshake under one deadline, abortable within ``POLL_S``."""
    last_error: OSError | None = None
    for family, kind, proto, _canon, address in resolve(host, port, cancel, deadline):
        sock = socket.socket(family, kind, proto)
        try:
            sock.setblocking(False)
            code = sock.connect_ex(address)
            if code not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK):
                raise OSError(code, os.strerror(code))
            _wait(sock, False, True, deadline, cancel)
            code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if code != 0:
                raise OSError(code, os.strerror(code))
            if tls is None:
                return BoundedSocket(sock, MAX_INBOUND_REMAINING_LENGTH)
            tls_sock = tls.wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
            _handshake(tls_sock, deadline, cancel)
            return BoundedSocket(tls_sock, MAX_INBOUND_REMAINING_LENGTH)
        except (ConnectCancelled, ConnectDeadlineExceeded, ssl.SSLError):
            sock.close()
            raise
        except OSError as exc:
            sock.close()
            last_error = exc
    raise last_error if last_error is not None else OSError(errno.EHOSTUNREACH, "no address")


class _SessionClient(mqtt.Client):
    def __init__(self, spec: ConnectSpec, cancel: threading.Event, deadline: float) -> None:
        super().__init__(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=spec.client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=False,
        )
        self._target = (spec.host, spec.port, spec.tls_context)
        self._cancel = cancel
        self._deadline = deadline
        self.bounded_socket: BoundedSocket | None = None
        self.max_inflight_messages_set(MAX_INFLIGHT_MESSAGES)
        self.max_queued_messages_set(MAX_QUEUED_MESSAGES)

    def _create_socket(self) -> BoundedSocket:  # type: ignore[override]
        host, port, tls = self._target
        self.bounded_socket = open_connection(host, port, tls, self._cancel, self._deadline)
        return self.bounded_socket


class PahoConnection:
    """One broker session; implements ``Connection``."""

    # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        spec: ConnectSpec,
        max_backlog_messages: int = MAX_BACKLOG_MESSAGES,
        max_backlog_bytes: int = MAX_BACKLOG_BYTES,
    ) -> None:
        self._spec = spec
        self._max_messages = max_backlog_messages
        self._max_bytes = max_backlog_bytes
        self._client: _SessionClient | None = None
        self._pending: deque[tuple[mqtt.MQTTMessageInfo, int]] = deque()
        self._pending_bytes = 0
        self._forwarded = 0
        self._inbound: deque[InboundMessage] = deque()
        self._inbound_dropped = 0
        self._connack: mqtt.ReasonCode | None = None
        self._disconnect: mqtt.ReasonCode | None = None
        self._closed = False

    @property
    def client(self) -> mqtt.Client | None:
        return self._client

    def _current(self, client: mqtt.Client) -> bool:
        return not self._closed and client is self._client

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: object,
        _flags: mqtt.ConnectFlags,
        reason: mqtt.ReasonCode,
        _properties: object,
    ) -> None:
        if self._current(client):
            self._connack = reason

    def _on_disconnect(
        self,
        client: mqtt.Client,
        _userdata: object,
        _flags: mqtt.DisconnectFlags,
        reason: mqtt.ReasonCode,
        _properties: object,
    ) -> None:
        if self._current(client):
            self._disconnect = reason

    def _on_message(self, client: mqtt.Client, _userdata: object, msg: mqtt.MQTTMessage) -> None:
        if not self._current(client):
            return
        try:
            topic = msg.topic
        except UnicodeDecodeError:
            self._inbound_dropped += 1
            return
        if len(self._inbound) >= MAX_INBOUND_MESSAGES:
            self._inbound_dropped += 1
            return
        self._inbound.append(InboundMessage(topic, bytes(msg.payload)))

    def open(self, cancel: threading.Event, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        spec = self._spec
        client = _SessionClient(spec, cancel, deadline)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        if spec.username is not None:
            client.username_pw_set(spec.username, spec.password)  # type: ignore[arg-type]
        if spec.will is not None:
            will = spec.will
            client.will_set(will.topic, will.payload, will.qos, will.retain)
        try:
            code = client.connect(spec.host, spec.port, keepalive=spec.keepalive_s)
        except ConnectCancelled as exc:
            raise TransportCancelled() from exc
        except OSError as exc:
            raise classify(exc) from None
        if code != mqtt.MQTT_ERR_SUCCESS:
            raise self._loss_error()
        while self._connack is None:
            if cancel.is_set():
                raise TransportCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise transport_error("network_connect_timeout")
            code = client.loop(timeout=min(POLL_S, remaining))
            if code == mqtt.MQTT_ERR_PROTOCOL:
                # paho reports a 3.1.1 protocol-version refusal this way once downgrade is off
                raise transport_error("auth_broker_unavailable")
            if self._connack is None and code not in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_AGAIN):
                raise self._loss_error()
        if self._connack.is_failure:
            raise transport_error(_CONNACK_CODES.get(str(self._connack), "auth_rejected"))

    def _loss_error(self) -> TransportError:
        client = self._client
        failure = (
            None
            if client is None or client.bounded_socket is None
            else client.bounded_socket.failure
        )
        if failure is not None:
            return classify(failure)
        if self._disconnect is not None and str(self._disconnect) == "Keep alive timeout":
            return transport_error("network_keepalive_timeout")
        return transport_error("network_connection_lost")

    def _live(self) -> _SessionClient:
        client = self._client
        if (
            client is None
            or self._closed
            or client.socket() is None
            or self._disconnect is not None
        ):
            raise self._loss_error()
        return client

    def subscribe(self, topic: str, qos: int) -> None:
        code, _mid = self._live().subscribe(topic, qos)
        if code != mqtt.MQTT_ERR_SUCCESS:
            raise self._loss_error()

    def _prune(self) -> None:
        kept: deque[tuple[mqtt.MQTTMessageInfo, int]] = deque()
        for info, size in self._pending:
            if info.is_published():
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    self._forwarded += 1
                self._pending_bytes -= size
            else:
                kept.append((info, size))
        self._pending = kept

    def backlog_full(self) -> bool:
        self._prune()
        return len(self._pending) >= self._max_messages or self._pending_bytes >= self._max_bytes

    def _send(self, message: OutboundMessage, bounded: bool) -> PublishResult:
        client = self._client
        if client is None or self._closed or not client.is_connected():
            return PublishResult.NOT_CONNECTED
        if len(message.payload) > MAX_PAYLOAD_BYTES:
            return PublishResult.TOO_LARGE
        if bounded and self.backlog_full():
            return PublishResult.BACKLOG_FULL
        info = client.publish(message.topic, message.payload, message.qos, message.retain)
        if info.rc == mqtt.MQTT_ERR_QUEUE_SIZE:
            return PublishResult.BACKLOG_FULL
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return PublishResult.NOT_CONNECTED
        self._pending.append((info, len(message.payload)))
        self._pending_bytes += len(message.payload)
        return PublishResult.ACCEPTED

    def publish(self, message: OutboundMessage) -> PublishResult:
        return self._send(message, bounded=True)

    def service(self, timeout_s: float) -> None:
        client = self._live()
        code = client.loop(timeout=timeout_s)
        if code not in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_AGAIN):
            raise self._loss_error()
        self._live()

    def drain(self) -> Drained:
        self._prune()
        drained = Drained(self._forwarded, tuple(self._inbound), self._inbound_dropped)
        self._forwarded = 0
        self._inbound.clear()
        self._inbound_dropped = 0
        return drained

    def close(self, final: Sequence[OutboundMessage], timeout_s: float) -> int:
        deadline = time.monotonic() + timeout_s
        client = self._client
        if client is not None and not self._closed and client.is_connected():
            infos = []
            for message in final:
                if self._send(message, bounded=False) is PublishResult.ACCEPTED:
                    infos.append(self._pending[-1][0])
            self._pump(client, deadline, lambda: all(info.is_published() for info in infos))
            if client.socket() is not None:
                client.disconnect()
                self._pump(client, deadline, lambda: client.socket() is None)
        return self._teardown()

    @staticmethod
    def _pump(client: mqtt.Client, deadline: float, done: Callable[[], bool]) -> None:
        while not done():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or client.socket() is None:
                return
            if client.loop(timeout=min(POLL_S, remaining)) not in (
                mqtt.MQTT_ERR_SUCCESS,
                mqtt.MQTT_ERR_AGAIN,
            ):
                return

    def abort(self) -> int:
        return self._teardown()

    def _teardown(self) -> int:
        self._prune()
        lost = len(self._pending)
        self._pending.clear()
        self._pending_bytes = 0
        self._inbound.clear()
        self._closed = True
        client = self._client
        if client is not None:
            client._reset_sockets()  # pylint: disable=protected-access
        return lost


def paho_connection(spec: ConnectSpec) -> PahoConnection:
    return PahoConnection(spec)
