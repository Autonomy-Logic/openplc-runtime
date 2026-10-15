# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Local channel between the plugin inside ``plc_main`` and the webserver.

While the plugin runs, it serves a Unix socket next to its installed
configuration. A client sends one request line and reads one reply:
``STATUS`` answers the sanitized status document, ``DROP_CREDENTIAL`` stops a
session that uses a credential the webserver has just removed. The socket is
owner-only and lives only between ``start_loop`` and ``stop_loop``; nothing is
written to disk besides the socket itself.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
from collections.abc import Callable
from typing import Final

STATUS_SOCKET_NAME: Final = "mqtt_status.socket"
REQUEST_STATUS: Final = "STATUS"
REQUEST_DROP_CREDENTIAL: Final = "DROP_CREDENTIAL"
MAX_REQUEST_BYTES: Final = 64
MAX_REPLY_BYTES: Final = 65_536
# sun_path is 104 bytes on macOS and 108 on Linux, including the terminator.
MAX_SOCKET_PATH_BYTES: Final = 103
ACCEPT_POLL_S: Final = 0.1
CLIENT_IO_TIMEOUT_S: Final = 1.0

Handler = Callable[[str], dict[str, object]]


class ChannelError(RuntimeError):
    """The socket could not be served or reached; the message never carries data."""


def status_socket_path(config_path: str) -> str:
    """Next to the installed configuration, relative when it is (the runtime root is the cwd)."""
    return os.path.join(os.path.dirname(config_path), STATUS_SOCKET_NAME)


def _check_path(path: str) -> None:
    if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
        raise ChannelError("the status socket path is too long for a Unix socket")


def _encode(reply: dict[str, object]) -> bytes:
    data = json.dumps(reply, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    if len(data) > MAX_REPLY_BYTES:
        data = b'{"error":"reply_too_large"}\n'
    return data


def _read_line(conn: socket.socket, limit: int) -> bytes | None:
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(limit + 1 - len(data))
        if not chunk:
            return None
        data += chunk
        if len(data) > limit:
            return None
    return data.split(b"\n", 1)[0]


class StatusServer:
    """Serves ``handler`` on a Unix socket from one daemon thread, one client at a time."""

    def __init__(self, path: str, handler: Handler, log: Callable[[str, str], None]) -> None:
        self._path = path
        self._handler = handler
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    @property
    def path(self) -> str:
        return self._path

    def start(self) -> bool:
        try:
            _check_path(self._path)
            self._remove_stale()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            previous = os.umask(0o077)
            try:
                sock.bind(self._path)
            finally:
                os.umask(previous)
            os.chmod(self._path, 0o600)
            sock.listen(4)
            sock.settimeout(ACCEPT_POLL_S)
        except (OSError, ChannelError) as exc:
            reason = str(exc) if isinstance(exc, ChannelError) else type(exc).__name__
            self._log("error", f"the status channel is not available ({reason})")
            return False
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="mqtt-status", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout_s: float = 2.0) -> bool:
        thread, self._thread = self._thread, None
        self._stop.set()
        stopped = True
        if thread is not None:
            thread.join(timeout_s)
            stopped = not thread.is_alive()
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()
            self._unlink()
        return stopped

    def _remove_stale(self) -> None:
        try:
            mode = os.lstat(self._path).st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(mode):
            raise ChannelError("the status socket path is taken by another file")
        os.unlink(self._path)

    def _unlink(self) -> None:
        try:
            if stat.S_ISSOCK(os.lstat(self._path).st_mode):
                os.unlink(self._path)
        except FileNotFoundError:
            pass

    def _serve(self) -> None:
        sock = self._sock
        while sock is not None and not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                self._answer(conn)

    def _answer(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(CLIENT_IO_TIMEOUT_S)
            line = _read_line(conn, MAX_REQUEST_BYTES)
            if line is None:
                return
            asked = line.decode("ascii", errors="replace").strip()
            if asked not in (REQUEST_STATUS, REQUEST_DROP_CREDENTIAL):
                conn.sendall(_encode({"error": "unknown_request"}))
                return
            try:
                reply = _encode(self._handler(asked))
            except (TypeError, ValueError):
                self._log("error", "the status channel could not encode its reply")
                reply = _encode({"error": "internal_error"})
            conn.sendall(reply)
        except OSError:
            return


def request(path: str, line: str, timeout_s: float) -> dict[str, object]:
    """Send one request line and return the decoded reply; raise ``ChannelError`` otherwise."""
    _check_path(path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_s)
            sock.connect(path)
            sock.sendall(line.encode("ascii") + b"\n")
            data = b""
            while len(data) <= MAX_REPLY_BYTES:
                chunk = sock.recv(MAX_REPLY_BYTES + 1 - len(data))
                if not chunk:
                    break
                data += chunk
    except OSError as exc:
        raise ChannelError(f"the status channel is not reachable ({type(exc).__name__})") from exc
    if len(data) > MAX_REPLY_BYTES or not data.endswith(b"\n"):
        raise ChannelError("the status channel answered an incomplete reply")
    try:
        reply = json.loads(data)
    except ValueError as exc:
        raise ChannelError("the status channel answered an invalid reply") from exc
    if not isinstance(reply, dict):
        raise ChannelError("the status channel answered an invalid reply")
    return reply
