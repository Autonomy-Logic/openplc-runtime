# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Minimal in-process MQTT 3.1.1 broker for transport tests.

Each behaviour a test needs (refused CONNACK, silence, withheld PUBACK, no
PINGRESP, no reading, closing on a large PUBLISH, an oversized inbound packet)
is a constructor switch. It records what the client sent, never more.
"""

from __future__ import annotations

import socket
import ssl
import struct
import threading
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Connect:
    client_id: str
    clean_session: bool
    keepalive: int
    will_topic: str | None
    will_payload: bytes | None
    will_qos: int
    username: str | None
    password: bytes | None = field(repr=False)
    will_retain: bool = False


@dataclass(frozen=True)
class Publish:
    topic: str
    payload: bytes
    qos: int
    retain: bool
    packet_id: int | None


def _remaining_length(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value % 128
        value //= 128
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def packet(header: int, body: bytes) -> bytes:
    return bytes([header]) + _remaining_length(len(body)) + body


def publish_packet(topic: str, payload: bytes) -> bytes:
    name = topic.encode()
    return packet(0x30, struct.pack("!H", len(name)) + name + payload)


class _Reader:
    def __init__(self, conn: socket.socket | ssl.SSLSocket) -> None:
        self._conn = conn

    def exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self._conn.recv(size - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return bytes(data)

    def packet(self) -> tuple[int, bytes]:
        header = self.exact(1)[0]
        value, multiplier = 0, 1
        while True:
            byte = self.exact(1)[0]
            value += (byte & 0x7F) * multiplier
            multiplier *= 128
            if not byte & 0x80:
                break
        return header, self.exact(value)


def _string(body: bytes, offset: int) -> tuple[bytes, int]:
    (size,) = struct.unpack_from("!H", body, offset)
    start = offset + 2
    return body[start : start + size], start + size


def parse_connect(body: bytes) -> Connect:
    _name, offset = _string(body, 0)
    flags = body[offset + 1]
    (keepalive,) = struct.unpack_from("!H", body, offset + 2)
    offset += 4
    client_id, offset = _string(body, offset)
    will_topic = will_payload = None
    if flags & 0x04:
        raw_topic, offset = _string(body, offset)
        will_topic = raw_topic.decode()
        will_payload, offset = _string(body, offset)
    username = password = None
    if flags & 0x80:
        raw_user, offset = _string(body, offset)
        username = raw_user.decode()
    if flags & 0x40:
        password, offset = _string(body, offset)
    return Connect(
        client_id.decode(),
        bool(flags & 0x02),
        keepalive,
        will_topic,
        will_payload,
        (flags >> 3) & 0x03,
        username,
        password,
        bool(flags & 0x20),
    )


class FakeBroker:
    # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        *,
        tls: ssl.SSLContext | None = None,
        connack_rc: int | None = 0,
        puback: bool = True,
        answer_ping: bool = True,
        read_after_connack: bool = True,
        close_on_payload_over: int | None = None,
        after_connack: bytes = b"",
    ) -> None:
        self._tls = tls
        self._connack_rc = connack_rc
        self._puback = puback
        self._answer_ping = answer_ping
        self._read = read_after_connack
        self._close_over = close_on_payload_over
        self._after_connack = after_connack
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        self.connects: list[Connect] = []
        self.publishes: list[Publish] = []
        self.subscriptions: list[tuple[str, int]] = []
        self.disconnects = 0
        self.pings = 0
        self.tls_failures: list[str] = []
        self.accepted = 0
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self.accepted += 1
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, raw: socket.socket) -> None:
        conn: socket.socket | ssl.SSLSocket = raw
        try:
            if self._tls is not None:
                try:
                    conn = self._tls.wrap_socket(raw, server_side=True)
                except (ssl.SSLError, OSError) as exc:
                    self.tls_failures.append(type(exc).__name__)
                    return
            self._session(conn)
        except (ConnectionError, OSError, struct.error, IndexError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _session(self, conn: socket.socket | ssl.SSLSocket) -> None:
        reader = _Reader(conn)
        while not self._closed:
            header, body = reader.packet()
            kind = header >> 4
            if kind == 1:
                self.connects.append(parse_connect(body))
                if self._connack_rc is None:
                    continue
                conn.sendall(packet(0x20, bytes([0, self._connack_rc])))
                if self._connack_rc != 0:
                    return
                if self._after_connack:
                    conn.sendall(self._after_connack)
                if not self._read:
                    while not self._closed:
                        threading.Event().wait(0.05)
                    return
            elif kind == 3:
                if not self._publish(conn, header, body):
                    return
            elif kind == 8:
                (packet_id,) = struct.unpack_from("!H", body, 0)
                topic, offset = _string(body, 2)
                self.subscriptions.append((topic.decode(), body[offset]))
                conn.sendall(packet(0x90, struct.pack("!HB", packet_id, body[offset])))
            elif kind == 12:
                self.pings += 1
                if self._answer_ping:
                    conn.sendall(packet(0xD0, b""))
            elif kind == 14:
                self.disconnects += 1
                return

    def _publish(self, conn: socket.socket | ssl.SSLSocket, header: int, body: bytes) -> bool:
        qos = (header >> 1) & 0x03
        topic, offset = _string(body, 0)
        packet_id = None
        if qos:
            (packet_id,) = struct.unpack_from("!H", body, offset)
            offset += 2
        payload = body[offset:]
        if self._close_over is not None and len(payload) > self._close_over:
            return False
        self.publishes.append(Publish(topic.decode(), payload, qos, bool(header & 1), packet_id))
        if qos == 1 and self._puback and packet_id is not None:
            conn.sendall(packet(0x40, struct.pack("!H", packet_id)))
        return True

    def inject(self, topic: str, payload: bytes) -> int:
        """Send a QoS 0 PUBLISH to every open client connection; returns how many got it."""
        with self._lock:
            conns = list(self._conns)
        sent = 0
        for conn in conns:
            try:
                conn.sendall(publish_packet(topic, payload))
                sent += 1
            except OSError:
                pass
        return sent

    def cut(self) -> None:
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def close(self) -> None:
        self._closed = True
        self._listener.close()
        self.cut()
