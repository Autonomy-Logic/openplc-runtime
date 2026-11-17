# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""HMI on the runtime's web server: the sealed browser client and the viewer channel.

* ``GET /hmi/`` and ``/hmi/<file>`` serve ``build/hmi/client/``; every file is checked
  against its line in ``build/hmi/hmi.seal`` before it is served.
* Socket.IO namespace ``/api/hmi``: one viewer per socket, authenticated with the account's
  access token. Viewer events are rebuilt from whitelisted fields, put back in the client's
  order and forwarded to the HMI host in ``plc_main`` over one Unix socket connection, with
  the binary protocol of ``docs/HMI.md``. Display lists come back per viewer; only the newest
  one is kept and one emitter thread sends it once the browser acknowledged the previous one.
"""

import hashlib
import hmac
import math
import os
import re
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from flask import Blueprint, Flask, Response, redirect, request
from flask_jwt_extended import current_user, decode_token, verify_jwt_in_request
from flask_jwt_extended.exceptions import JWTExtendedException
from flask_socketio import ConnectionRefusedError as SocketRefused
from flask_socketio import SocketIO
import jwt
from jwt import PyJWTError
from sqlalchemy.exc import SQLAlchemyError

from webserver import sessions
from webserver.database import db
from webserver.access import ADMIN_ROLE, ENGINEER_ROLE, OPERATOR_ROLE, VIEWER_ROLE, may
from webserver.logger import get_logger
from webserver.restapi import check_if_token_revoked, user_lookup_callback

logger, _ = get_logger("hmi", use_buffer=True)

NAMESPACE = "/api/hmi"
POLICY_KEY = "hmi"

_REPO_ROOT = Path(__file__).resolve().parent.parent
# The host's socket, in a folder only the runtime's user enters; OPENPLC_HMI_SOCKET overrides it.
HMI_SOCKET_PATH = os.environ.get("OPENPLC_HMI_SOCKET", "/run/runtime/hmi/hmi.socket")
# build/hmi: plugin, seal and client; OPENPLC_HMI_DIR overrides it for tests.
HMI_DIR = Path(os.environ.get("OPENPLC_HMI_DIR", str(_REPO_ROOT / "build" / "hmi")))
PLUGIN_NAME = "libhmi_plugin.so"
SEAL_NAME = "hmi.seal"
CLIENT_DIR = "client"

# Wire protocol, web server to host.
MSG_OPEN = 0x01
MSG_CLOSE = 0x02
MSG_VIEWPORT = 0x03
MSG_POINTER = 0x04
MSG_KEY = 0x05
# Wire protocol, host to web server.
MSG_FRAME = 0x81
MSG_STATUS = 0x82
MSG_OPENED = 0x83
MSG_BUSY = 0x84
MSG_FAULT = 0x85
MSG_SIGN_OUT = 0x86
MAX_MESSAGE = 4 * 1024 * 1024
HOST_STATUS_LIVE = 0
HOST_STATUS_OUT_OF_DATE = 1
ROLE_CODES: dict[str, int] = {ADMIN_ROLE: 0, ENGINEER_ROLE: 1, OPERATOR_ROLE: 2, VIEWER_ROLE: 3}
MAX_NAME_BYTES = 64

# Viewer-facing states of the hmi_status event.
STATE_LIVE = "live"
STATE_STOPPED = "stopped"
STATE_OUT_OF_DATE = "out_of_date"
STATE_BUSY = "busy"
STATE_FAULT = "fault"
STATE_UNAVAILABLE = "unavailable"

# Ranges of the client events.
MAX_VIEWPORT_PX = 8192
MIN_DENSITY_X100 = 25
MAX_DENSITY_X100 = 800
MAX_POINTER_PX = 100_000.0
MAX_KEY_CODE = 0x10FFFF
MAX_SEQUENCE = 2**53
POINTER_KINDS = (0, 1, 2)

# Events held back per viewer while an earlier sequence number is missing.
MAX_PENDING = 64
REVERIFY_INTERVAL_S = 5.0
RETRY_INTERVAL_S = 1.0
HOST_SEND_TIMEOUT_S = 2.0
# A display list not acknowledged within this time no longer holds back the next one.
ACK_TIMEOUT_S = 1.0

ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".wasm": "application/wasm",
    ".txt": "text/plain; charset=utf-8",
    ".ttf": "font/ttf",
}
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; "
        "img-src 'self' data:; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}
NO_HMI_PAGE = (
    "<!doctype html><html><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width, initial-scale=1'>"
    "<meta http-equiv=refresh content=5><title>OpenPLC HMI</title></head>"
    "<body><p>No HMI installed.</p></body></html>"
)

hmi_bp = Blueprint("hmi_blueprint", __name__)


# ---- client assets ----------------------------------------------------------------------


def _with_headers(response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def _not_found() -> Response:
    return _with_headers(Response("Not found", status=404, mimetype="text/plain"))


def hmi_installed() -> bool:
    """Whether a built HMI is present: its plugin and its seal."""
    return (HMI_DIR / PLUGIN_NAME).is_file() and (HMI_DIR / SEAL_NAME).is_file()


def sealed_digest(relative: str) -> str | None:
    """The sha256 the seal records for ``relative`` (a path under build/hmi), or None."""
    try:
        text = (HMI_DIR / SEAL_NAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("HMI seal unreadable: %s", e)
        return None
    for line in text.splitlines():
        digest, sep, path = line.partition("  ")
        if sep and path == relative and re.fullmatch(r"[0-9a-f]{64}", digest):
            return digest
    return None


def read_sealed_client_file(name: str) -> bytes | None:
    """The bytes of client file ``name`` when they match the seal; None (and a log line) if not."""
    client_dir = (HMI_DIR / CLIENT_DIR).resolve()
    path = client_dir / name
    try:
        if path.resolve().parent != client_dir or not path.is_file():
            logger.warning("HMI client file %s refused: not found", name)
            return None
        data = path.read_bytes()
    except OSError as e:
        logger.warning("HMI client file %s refused: %s", name, e)
        return None
    expected = sealed_digest(f"{CLIENT_DIR}/{name}")
    if expected is None:
        logger.warning("HMI client file %s refused: not in the seal", name)
        return None
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
        logger.warning("HMI client file %s refused: does not match the seal", name)
        return None
    return data


@hmi_bp.route("/hmi", strict_slashes=True)
def hmi_redirect() -> Response:
    """Sends ``/hmi`` to ``/hmi/``, so the client's relative paths resolve."""
    return _with_headers(redirect("/hmi/", code=302))


@hmi_bp.route("/hmi/")
def hmi_index() -> Response:
    """The client's page."""
    return hmi_asset("index.html")


@hmi_bp.route("/hmi/<name>")
def hmi_asset(name: str) -> Response:
    """A whitelisted, sealed client file; the self-refreshing notice when no HMI is installed."""
    suffix = os.path.splitext(name)[1]
    if not ASSET_NAME.match(name) or name.startswith(".") or suffix not in MIME_TYPES:
        return _not_found()
    if not hmi_installed():
        if name != "index.html":
            return _not_found()
        return _with_headers(Response(NO_HMI_PAGE, status=503, mimetype="text/html"))
    data = read_sealed_client_file(name)
    if data is None:
        return _not_found()
    return _with_headers(Response(data, status=200, content_type=MIME_TYPES[suffix]))


# ---- client events ----------------------------------------------------------------------


def _number(data: dict[object, object], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _integer(data: dict[object, object], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def sequence_of(data: object) -> int | None:
    """The event's sequence number ``n``, or None when it is missing or out of range."""
    if not isinstance(data, dict):
        return None
    n = _integer(data, "n")
    if n is None or not 0 <= n < MAX_SEQUENCE:
        return None
    return n


@dataclass(frozen=True)
class Viewport:
    """A viewer's viewport: CSS pixels and density x100."""

    width: int
    height: int
    density: int

    def pack(self) -> bytes:
        """The viewport fields of OPEN and VIEWPORT."""
        return struct.pack("<HHH", self.width, self.height, self.density)


@dataclass(frozen=True)
class PointerEvent:
    """A pointer event: kind and CSS pixel position x100."""

    kind: int
    x: int
    y: int


@dataclass(frozen=True)
class KeyEvent:
    """A key event: its code point."""

    code: int


ClientEvent = Viewport | PointerEvent | KeyEvent


def parse_event(data: object) -> ClientEvent | None:
    """Rebuilds a client event from its whitelisted, range-checked fields; None when invalid."""
    if not isinstance(data, dict):
        return None
    kind = data.get("t")
    if kind == "v":
        w, h, d = _number(data, "w"), _number(data, "h"), _number(data, "d")
        if w is None or h is None or d is None:
            return None
        width, height, density = round(w), round(h), round(d * 100)
        if not (1 <= width <= MAX_VIEWPORT_PX and 1 <= height <= MAX_VIEWPORT_PX):
            return None
        if not MIN_DENSITY_X100 <= density <= MAX_DENSITY_X100:
            return None
        return Viewport(width, height, density)
    if kind == "p":
        k, x, y = _integer(data, "k"), _number(data, "x"), _number(data, "y")
        if k not in POINTER_KINDS or x is None or y is None:
            return None
        if abs(x) > MAX_POINTER_PX or abs(y) > MAX_POINTER_PX:
            return None
        return PointerEvent(k, round(x * 100), round(y * 100))
    if kind == "k":
        c = _integer(data, "c")
        if c is None or not 1 <= c <= MAX_KEY_CODE:
            return None
        return KeyEvent(c)
    return None


# ---- wire protocol ----------------------------------------------------------------------


class ProtocolError(Exception):
    """The host sent a malformed message."""


def encode(msg_type: int, payload: bytes) -> bytes:
    """One message: ``u32 length | u8 type | payload``."""
    return struct.pack("<IB", len(payload) + 1, msg_type) + payload


def wire_name(name: str) -> str:
    """``name`` as the host accepts it: C0, DEL, C1 and lone surrogate code points become '?'."""
    return "".join(
        "?" if ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF else c
        for c in name
    )


def encode_open(viewer: int, role: int, viewport: Viewport, name: str) -> bytes:
    """An OPEN message; ``name`` through ``wire_name``, cut to 64 bytes of UTF-8 on a character
    boundary."""
    raw = wire_name(name).encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", "ignore").encode("utf-8")
    payload = struct.pack("<IB", viewer, role) + viewport.pack() + bytes([len(raw)]) + raw
    return encode(MSG_OPEN, payload)


def encode_event(viewer: int, event: ClientEvent) -> bytes:
    """The VIEWPORT, POINTER or KEY message of a client event."""
    if isinstance(event, Viewport):
        return encode(MSG_VIEWPORT, struct.pack("<I", viewer) + event.pack())
    if isinstance(event, PointerEvent):
        return encode(MSG_POINTER, struct.pack("<IBii", viewer, event.kind, event.x, event.y))
    return encode(MSG_KEY, struct.pack("<II", viewer, event.code))


def _recv_exact(sock: socket.socket, size: int, stop: threading.Event) -> bytes | None:
    buf = bytearray()
    while len(buf) < size:
        try:
            chunk = sock.recv(size - len(buf))
        except TimeoutError:
            if stop.is_set():
                return None
            continue
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_message(sock: socket.socket, stop: threading.Event) -> tuple[int, bytes] | None:
    """The next message from the host; None at end of stream or once ``stop`` is set."""
    head = _recv_exact(sock, 5, stop)
    if head is None:
        return None
    length, msg_type = struct.unpack("<IB", head)
    if length < 1 or length > MAX_MESSAGE:
        raise ProtocolError(f"message length {length}")
    payload = _recv_exact(sock, length - 1, stop)
    if payload is None:
        return None
    return msg_type, payload


# ---- viewers ----------------------------------------------------------------------------


@dataclass
class Viewer:
    """One browser socket on /api/hmi."""

    sid: str
    viewer_id: int
    username: str
    role: str
    token: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    viewport: Viewport | None = None
    # Host connection epoch the viewer's OPEN went out on; None when not open on the host.
    open_epoch: int | None = None
    # Set under ``lock`` once the socket left; nothing is sent to the host for it afterwards.
    closed: bool = False
    opened: bool = False
    status: str | None = None
    next_n: int = 0
    pending: dict[int, ClientEvent | None] = field(default_factory=dict)
    frame: bytes | None = None
    frame_seq: int = 0
    in_flight: int | None = None
    sent_at: float = 0.0

    def order(self, n: int, event: ClientEvent | None) -> list[ClientEvent]:
        """Events ready to forward, in ``n`` order; call with ``lock`` held.

        An invalid event still takes its number, so the ones after it are not held back.
        """
        if n < self.next_n or n in self.pending:
            return []
        self.pending[n] = event
        if len(self.pending) > MAX_PENDING:
            skipped_to = min(self.pending)
            logger.warning(
                "HMI viewer %s: events %d-%d never arrived, skipped",
                self.username,
                self.next_n,
                skipped_to - 1,
            )
            self.next_n = skipped_to
        ready: list[ClientEvent] = []
        while self.next_n in self.pending:
            item = self.pending.pop(self.next_n)
            self.next_n += 1
            if item is not None:
                ready.append(item)
        return ready

    def offer_frame(self, frame: bytes) -> None:
        """Keeps ``frame`` as the newest display list, dropping an unsent older one."""
        self.frame = frame
        self.frame_seq += 1

    def take_frame(self, now: float) -> tuple[int, bytes] | None:
        """The newest display list when none is in flight (or the last one timed out)."""
        if self.frame is None:
            return None
        if self.in_flight is not None and now - self.sent_at < ACK_TIMEOUT_S:
            return None
        frame, self.frame = self.frame, None
        self.in_flight = self.frame_seq
        self.sent_at = now
        return self.frame_seq, frame

    def acknowledge(self, seq: int) -> bool:
        """Clears the frame in flight when ``seq`` is it; True when a pending frame may go."""
        if self.in_flight != seq:
            return False
        self.in_flight = None
        return self.frame is not None


EmitFn = Callable[[str, object, str, Callable[..., None] | None], None]
DisconnectFn = Callable[[str], None]


class HmiProxy:
    """The viewers, the host connection and the frame emitter of /api/hmi."""

    def __init__(self, app: Flask, emit: EmitFn, disconnect: DisconnectFn) -> None:
        self._app = app
        self._emit = emit
        self._disconnect = disconnect
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._frames = threading.Condition(self._lock)
        self._viewers: dict[str, Viewer] = {}
        self._by_id: dict[int, Viewer] = {}
        self._next_id = 1
        self._host: socket.socket | None = None
        # Counts host connections; changed with ``_host`` under both ``_send_lock`` and ``_lock``.
        self._epoch = 0
        self._host_state: str | None = None
        self._running = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle --

    def start(self) -> None:
        """Starts the host connection, emitter and re-verification threads."""
        if self._running:
            return
        self._running = True
        self._stop.clear()
        for target, name in (
            (self._host_loop, "hmi-host"),
            (self._emit_loop, "hmi-emitter"),
            (self._verify_loop, "hmi-verify"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def shutdown(self) -> None:
        """Stops the threads and closes the host connection."""
        self._running = False
        self._stop.set()
        with self._lock:
            self._frames.notify_all()
        self._close_host()
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads.clear()

    # -- viewers --

    def viewer(self, sid: str) -> Viewer | None:
        """The viewer of socket ``sid``."""
        with self._lock:
            return self._viewers.get(sid)

    def viewers(self) -> list[Viewer]:
        """Every connected viewer."""
        with self._lock:
            return list(self._viewers.values())

    def add_viewer(self, sid: str, username: str, role: str, token: str) -> Viewer:
        """Registers a new viewer and sends it the current state."""
        with self._lock:
            while self._next_id in self._by_id or self._next_id == 0:
                self._next_id = (self._next_id + 1) & 0xFFFFFFFF
            viewer = Viewer(sid, self._next_id, username, role, token)
            self._next_id = (self._next_id + 1) & 0xFFFFFFFF
            self._viewers[sid] = viewer
            self._by_id[viewer.viewer_id] = viewer
            count = len(self._viewers)
            connected = self._host is not None
            host_state = self._host_state
        logger.info("HMI viewer connected: %s (%s), %d viewers", username, role, count)
        if not connected:
            self._set_status(viewer, self._offline_state())
        elif host_state == STATE_OUT_OF_DATE:
            self._set_status(viewer, STATE_OUT_OF_DATE)
        return viewer

    def remove_viewer(self, sid: str) -> None:
        """Drops the viewer of socket ``sid`` and closes it on the host."""
        with self._lock:
            viewer = self._viewers.pop(sid, None)
            if viewer is None:
                return
            self._by_id.pop(viewer.viewer_id, None)
            count = len(self._viewers)
        with viewer.lock:
            viewer.closed = True
            if self._open_on_host(viewer):
                self._send_host(encode(MSG_CLOSE, struct.pack("<I", viewer.viewer_id)))
            viewer.open_epoch = None
            viewer.opened = False
        logger.info("HMI viewer left: %s, %d viewers", viewer.username, count)

    def follow_rotation(self, old_jti: str, new_token: str) -> None:
        """Re-keys every viewer holding the renewed token to its successor."""
        for viewer in self.viewers():
            try:
                held = decode_token(viewer.token, allow_expired=True).get("jti")
            except (PyJWTError, JWTExtendedException):
                continue
            if held == old_jti:
                viewer.token = new_token
                logger.info("HMI viewer %s: token followed a renewal", viewer.username)

    def token_in_force(self, viewer: Viewer) -> bool:
        """Re-checks the viewer's token: signature, revocation, session, account and role.

        Expiry is checked only at connect, as on /api/debug: an open viewer stays open until its
        session ends; renewals reach it through ``follow_rotation``.
        """
        try:
            payload: dict[str, object] = decode_token(viewer.token, allow_expired=True)
        except (PyJWTError, JWTExtendedException) as e:
            logger.warning("HMI viewer %s: token did not verify: %s", viewer.username, e)
            return False
        if payload.get("type") != "access" or check_if_token_revoked({}, payload):
            logger.info("HMI viewer %s: token no longer in force", viewer.username)
            return False
        user = user_lookup_callback({}, payload)
        if user is None or not may(user.role, POLICY_KEY):
            logger.info("HMI viewer %s: account gone or role refused", viewer.username)
            return False
        if user.role != viewer.role:
            logger.info("HMI viewer %s: role changed, closing the viewer", viewer.username)
            return False
        return True

    def reverify_all(self) -> None:
        """Re-checks every viewer's token; a refused one is disconnected."""
        refused: list[str] = []
        with self._app.app_context():
            for viewer in self.viewers():
                if not self.token_in_force(viewer):
                    refused.append(viewer.sid)
        for sid in refused:
            self._disconnect(sid)

    def handle_input(self, sid: str, data: object) -> None:
        """Validates, orders and forwards one client event of socket ``sid``."""
        viewer = self.viewer(sid)
        if viewer is None:
            return
        if not self.token_in_force(viewer):
            self._disconnect(sid)
            return
        n = sequence_of(data)
        if n is None:
            logger.warning("HMI viewer %s: event without a valid sequence dropped", viewer.username)
            return
        event = parse_event(data)
        if event is None:
            logger.warning("HMI viewer %s: invalid event %d dropped", viewer.username, n)
        with viewer.lock:
            for ready in viewer.order(n, event):
                self._forward(viewer, ready)

    def _open_on_host(self, viewer: Viewer) -> bool:
        """Whether the viewer's OPEN went out on the current host connection."""
        return viewer.open_epoch is not None and viewer.open_epoch == self._epoch

    def _forward(self, viewer: Viewer, event: ClientEvent) -> None:
        """Sends one ordered event to the host; call with ``viewer.lock`` held."""
        if viewer.closed:
            return
        if isinstance(event, Viewport):
            viewer.viewport = event
            if not self._open_on_host(viewer):
                self._open(viewer)
                return
        if self._open_on_host(viewer):
            self._send_host(encode_event(viewer.viewer_id, event))

    def _open(self, viewer: Viewer) -> None:
        """Sends OPEN with the viewer's last viewport; call with ``viewer.lock`` held."""
        if viewer.closed or viewer.viewport is None:
            return
        message = encode_open(
            viewer.viewer_id, ROLE_CODES[viewer.role], viewer.viewport, viewer.username
        )
        viewer.open_epoch = self._send_host_on(message)

    # -- statuses and frames --

    def _offline_state(self) -> str:
        return STATE_STOPPED if hmi_installed() else STATE_UNAVAILABLE

    def _set_status(self, viewer: Viewer, state: str) -> None:
        with self._lock:
            if viewer.status == state:
                return
            viewer.status = state
        self._emit("hmi_status", {"state": state}, viewer.sid, None)

    def _emit_loop(self) -> None:
        while not self._stop.is_set():
            batch: list[tuple[Viewer, int, bytes]] = []
            with self._frames:
                now = time.monotonic()
                wait = ACK_TIMEOUT_S
                for viewer in self._viewers.values():
                    taken = viewer.take_frame(now)
                    if taken is not None:
                        batch.append((viewer, taken[0], taken[1]))
                    elif viewer.frame is not None:
                        wait = min(wait, max(0.01, ACK_TIMEOUT_S - (now - viewer.sent_at)))
                if not batch:
                    self._frames.wait(timeout=wait)
                    continue
            for viewer, seq, frame in batch:
                self._emit("dl", frame, viewer.sid, self._ack_callback(viewer, seq))

    def _ack_callback(self, viewer: Viewer, seq: int) -> Callable[..., None]:
        def acked(*_args: object) -> None:
            with self._frames:
                if viewer.acknowledge(seq):
                    self._frames.notify_all()

        return acked

    # -- host connection --

    def _send_host(self, message: bytes) -> bool:
        """Sends one message to the host; False when it is not connected or the send failed."""
        return self._send_host_on(message) is not None

    def _send_host_on(self, message: bytes) -> int | None:
        """Sends one message to the host; the epoch of the connection it went out on, or None."""
        with self._send_lock:
            host, epoch = self._host, self._epoch
            if host is None:
                return None
            try:
                host.sendall(message)
                return epoch
            except OSError as e:
                logger.warning("HMI host send failed: %s", e)
        self._close_host()
        return None

    def _close_host(self) -> None:
        with self._lock:
            host, self._host = self._host, None
        if host is None:
            return
        try:
            host.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        host.close()

    def _connect_host(self) -> socket.socket | None:
        try:
            info = os.lstat(HMI_SOCKET_PATH)
            folder = os.lstat(os.path.dirname(HMI_SOCKET_PATH))
        except OSError:
            return None
        if not stat.S_ISSOCK(info.st_mode):
            return None
        if (
            info.st_uid != os.geteuid()
            or not stat.S_ISDIR(folder.st_mode)
            or folder.st_uid != os.geteuid()
            or folder.st_mode & 0o077
        ):
            logger.warning("HMI host socket %s is not owner-only, not used", HMI_SOCKET_PATH)
            return None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(HMI_SOCKET_PATH)
        except OSError:
            sock.close()
            return None
        sock.settimeout(HOST_SEND_TIMEOUT_S)
        return sock

    def _host_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._connect_host()
            if sock is None:
                self._host_gone()
                self._stop.wait(RETRY_INTERVAL_S)
                continue
            logger.info("HMI host connected")
            self._host_up(sock)
            try:
                self._read_host(sock)
            except ProtocolError as e:
                logger.warning("HMI host sent a malformed message (%s), reconnecting", e)
            except OSError as e:
                logger.info("HMI host connection lost: %s", e)
            except Exception:
                logger.exception("HMI host message could not be handled, reconnecting")
            self._close_host()
            self._host_gone()
            self._stop.wait(RETRY_INTERVAL_S)

    def _host_up(self, sock: socket.socket) -> None:
        with self._send_lock, self._lock:
            self._host = sock
            self._epoch += 1
            self._host_state = None
            viewers = list(self._viewers.values())
        for viewer in viewers:
            with viewer.lock:
                if not self._open_on_host(viewer):
                    viewer.opened = False
                    self._open(viewer)

    def _host_gone(self) -> None:
        state = self._offline_state()
        with self._lock:
            if self._host_state == state:
                return
            self._host_state = state
            viewers = list(self._viewers.values())
        for viewer in viewers:
            with viewer.lock:
                viewer.open_epoch = None
                viewer.opened = False
            with self._frames:
                viewer.frame = None
            self._set_status(viewer, state)

    def _read_host(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            message = read_message(sock, self._stop)
            if message is None:
                logger.info("HMI host closed the connection")
                return
            self._dispatch(*message)

    def _dispatch(self, msg_type: int, payload: bytes) -> None:
        if msg_type == MSG_FRAME:
            if len(payload) < 4:
                raise ProtocolError("FRAME too short")
            viewer_id = struct.unpack_from("<I", payload)[0]
            with self._frames:
                viewer = self._by_id.get(viewer_id)
                if viewer is not None:
                    viewer.offer_frame(payload[4:])
                    self._frames.notify_all()
        elif msg_type == MSG_STATUS:
            if len(payload) != 1 or payload[0] not in (HOST_STATUS_LIVE, HOST_STATUS_OUT_OF_DATE):
                raise ProtocolError("bad STATUS")
            self._host_status(payload[0])
        elif msg_type == MSG_SIGN_OUT:
            if len(payload) != 4:
                raise ProtocolError("bad SIGN_OUT")
            with self._lock:
                viewer = self._by_id.get(struct.unpack("<I", payload)[0])
            if viewer is not None:
                self._sign_out(viewer)
        elif msg_type in (MSG_OPENED, MSG_BUSY, MSG_FAULT):
            if len(payload) != 4:
                raise ProtocolError("bad OPENED/BUSY/FAULT")
            with self._lock:
                viewer = self._by_id.get(struct.unpack("<I", payload)[0])
            if viewer is None:
                return
            with viewer.lock:
                viewer.opened = msg_type == MSG_OPENED
                if msg_type != MSG_OPENED:
                    viewer.open_epoch = None
            if msg_type == MSG_OPENED:
                self._set_status(viewer, STATE_LIVE)
            elif msg_type == MSG_BUSY:
                logger.warning("HMI host busy, viewer %s not opened", viewer.username)
                self._set_status(viewer, STATE_BUSY)
            else:
                logger.warning("HMI screen fault, viewer %s closed", viewer.username)
                with self._frames:
                    viewer.frame = None
                self._set_status(viewer, STATE_FAULT)
        else:
            raise ProtocolError(f"unknown type 0x{msg_type:02x}")

    def _sign_out(self, viewer: Viewer) -> None:
        """Ends the viewer's session as /api/logout does, tells its browser and disconnects it.

        The browser is signed out and disconnected even when the session could not be ended; the
        log says which happened.
        """
        with self._app.app_context():
            try:
                ended = self._end_session(viewer)
            except SQLAlchemyError as e:
                db.session.rollback()
                ended = False
                logger.error(
                    "HMI viewer %s: the session could not be ended on sign-out: %s",
                    viewer.username,
                    e,
                )
        if ended:
            logger.info("HMI viewer %s signed out from its screen", viewer.username)
        self._emit("hmi_signed_out", {}, viewer.sid, None)
        self._disconnect(viewer.sid)

    @staticmethod
    def _end_session(viewer: Viewer) -> bool:
        """Ends the session of the viewer's token, which verified when the viewer connected.

        When it no longer verifies (the signing key changed), the session is ended by its id.
        """
        try:
            payload: dict[str, object] = decode_token(viewer.token, allow_expired=True)
        except (PyJWTError, JWTExtendedException) as e:
            try:
                claims: object = jwt.decode(viewer.token, options={"verify_signature": False})
            except PyJWTError:
                claims = None
            sid = claims.get(sessions.SID_CLAIM) if isinstance(claims, dict) else None
            if not isinstance(sid, str):
                logger.error(
                    "HMI viewer %s: sign-out token unreadable (%s); session not ended",
                    viewer.username,
                    e,
                )
                return False
            sessions.end_session(sid, "signed out from the screen")
            logger.warning(
                "HMI viewer %s: sign-out token did not verify (%s); session ended by its id",
                viewer.username,
                e,
            )
            return True
        sessions.sign_out(payload)
        return True

    def _host_status(self, code: int) -> None:
        state = STATE_LIVE if code == HOST_STATUS_LIVE else STATE_OUT_OF_DATE
        with self._lock:
            changed = self._host_state != state
            self._host_state = state
            viewers = list(self._viewers.values())
        if changed and state == STATE_OUT_OF_DATE:
            logger.warning("HMI is out of date: its program MD5 does not match the PLC program")
        for viewer in viewers:
            if state == STATE_OUT_OF_DATE:
                with viewer.lock:
                    viewer.opened = False
                self._set_status(viewer, state)
            elif viewer.opened:
                self._set_status(viewer, state)

    def _verify_loop(self) -> None:
        while not self._stop.wait(REVERIFY_INTERVAL_S):
            self.reverify_all()


_proxy: HmiProxy | None = None  # pylint: disable=invalid-name


def get_proxy() -> HmiProxy | None:
    """The running proxy, once ``init_hmi_proxy`` ran."""
    return _proxy


def _follow_rotation(old_jti: str, new_token: str) -> None:
    if _proxy is not None:
        _proxy.follow_rotation(old_jti, new_token)


sessions.on_rotation(_follow_rotation)


def init_hmi_proxy(app: Flask, socketio: SocketIO) -> HmiProxy:
    """Registers the /api/hmi namespace on ``socketio`` and starts the proxy threads."""
    global _proxy  # pylint: disable=global-statement
    if _proxy is not None:
        _proxy.shutdown()

    def emit(event: str, data: object, sid: str, callback: Callable[..., None] | None) -> None:
        socketio.emit(event, data, to=sid, namespace=NAMESPACE, callback=callback)

    def disconnect(sid: str) -> None:
        socketio.server.disconnect(sid, namespace=NAMESPACE)

    proxy = HmiProxy(app, emit, disconnect)
    _proxy = proxy

    @socketio.on("connect", namespace=NAMESPACE)
    def hmi_connect(auth: object) -> bool:
        token = auth.get("token") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not token:
            logger.warning("HMI viewer refused: no token")
            raise SocketRefused("unauthorized")
        try:
            request.environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
            verify_jwt_in_request()
        except (PyJWTError, JWTExtendedException) as e:
            logger.warning("HMI viewer refused: %s", e)
            raise SocketRefused("unauthorized") from e
        role = current_user.role if current_user else None
        if not may(role, POLICY_KEY) or role not in ROLE_CODES:
            logger.warning("HMI viewer refused for role %s", role or "none")
            raise SocketRefused("unauthorized")
        proxy.add_viewer(request.sid, current_user.username, role, token)
        return True

    @socketio.on("disconnect", namespace=NAMESPACE)
    def hmi_disconnect(*_args: object) -> None:
        proxy.remove_viewer(request.sid)

    @socketio.on("in", namespace=NAMESPACE)
    def hmi_input(data: object) -> None:
        proxy.handle_input(request.sid, data)

    proxy.start()
    logger.info("HMI endpoint initialized at /hmi/ and %s", NAMESPACE)
    return proxy
