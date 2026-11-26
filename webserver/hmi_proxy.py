# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""HMI on the runtime's web server: the sealed browser client and the viewer channel.

* ``GET /hmi/`` and ``/hmi/<file>`` serve ``build/hmi/client/``; every file is checked
  against its line in ``build/hmi/hmi.seal`` before it is served. The client runs the screens.
* Socket.IO namespace ``/api/hmi``: one viewer per socket, authenticated with the account's
  access token. Viewer events (open, show a screen, write a tag) are rebuilt from whitelisted
  fields, put back in the client's order and forwarded to the HMI host in ``plc_main`` over one
  Unix socket connection, with the binary protocol of ``docs/HMI.md``. The host decides what
  each viewer may read and write. Values come back per viewer; changes not yet delivered are
  merged, and one emitter thread sends them once the browser acknowledged the previous batch.
"""

import hashlib
import hmac
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
from jwt import PyJWTError

from webserver import sessions
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
# The screens module; its sealed sha256 is the HMI build ID.
CLIENT_MODULE = "hmi.wasm"
# Response header carrying the build ID of the served screens module.
BUILD_HEADER = "X-HMI-Build"
BUILD_ID = re.compile(r"^[0-9a-f]{64}$")

# Wire protocol, web server to host.
MSG_OPEN = 0x01
MSG_CLOSE = 0x02
MSG_SHOW = 0x03
MSG_WRITE = 0x04
MSG_BIT = 0x05
# Wire protocol, host to web server.
MSG_STATUS = 0x81
MSG_OPENED = 0x82
MSG_BUSY = 0x83
MSG_VALUES = 0x84
MSG_REFUSED = 0x85
MAX_MESSAGE = 4 * 1024 * 1024
# A tag or screen id no manifest has: what an invalid client request is forwarded as, so the
# host refuses it and its count of writes stays the client's.
NO_ID = 0xFFFF
REFUSED_WHAT = {0: "show", 1: "write"}
HOST_STATUS_LIVE = 0
HOST_STATUS_OUT_OF_DATE = 1
ROLE_CODES: dict[str, int] = {ADMIN_ROLE: 0, ENGINEER_ROLE: 1, OPERATOR_ROLE: 2, VIEWER_ROLE: 3}
MAX_NAME_BYTES = 64

# Viewer-facing states of the hmi_status event.
STATE_LIVE = "live"
STATE_STOPPED = "stopped"
STATE_OUT_OF_DATE = "out_of_date"
STATE_BUSY = "busy"
STATE_UNAVAILABLE = "unavailable"

# Ranges of the client events.
MAX_ID = 0xFFFE
MAX_VALUE_BYTES = 1024
MAX_BIT = 63
MAX_SEQUENCE = 2**53

# Events held back per viewer while an earlier sequence number is missing.
MAX_PENDING = 64
REVERIFY_INTERVAL_S = 5.0
RETRY_INTERVAL_S = 1.0
HOST_SEND_TIMEOUT_S = 2.0
# Values not acknowledged within this time no longer hold back the next batch.
ACK_TIMEOUT_S = 1.0

ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# The client files any visitor may load: the sign-in page and its static code, style and licences,
# none of them derived from the project. Every other client file (hmi.wasm, which holds the
# project's screens) is served only with a token that may open the HMI.
PUBLIC_ASSETS: frozenset[str] = frozenset(
    {
        "index.html",
        "hmi.css",
        "hmi-client.js",
        "socket.io.min.js",
        "socket.io-LICENSE.txt",
        "fonts-LICENSE.txt",
    }
)
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


def hmi_build_id() -> str | None:
    """The HMI build ID: the sha256 the seal records for the screens module, or None."""
    return sealed_digest(f"{CLIENT_DIR}/{CLIENT_MODULE}")


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


def hmi_access_refusal() -> str | None:
    """Why the current request's access token may not use the HMI, or None when it may.

    The same checks as every endpoint (signature, expiry, revocation, session, account), plus a
    role that may open the HMI. Call inside a request whose Authorization header carries the token.
    """
    try:
        verify_jwt_in_request()
    except (PyJWTError, JWTExtendedException) as e:
        return str(e) or type(e).__name__
    role = current_user.role if current_user else None
    if not may(role, POLICY_KEY) or role not in ROLE_CODES:
        return f"role {role or 'none'} may not open the HMI"
    return None


def _is_navigation() -> bool:
    """Whether the request is a browser loading the URL as a page, not a script's fetch."""
    mode = request.headers.get("Sec-Fetch-Mode")
    if mode is not None:
        return mode == "navigate"
    return "text/html" in request.headers.get("Accept", "")


@hmi_bp.route("/hmi/<name>")
def hmi_asset(name: str) -> Response:
    """A whitelisted, sealed client file; the self-refreshing notice when no HMI is installed.

    A file outside PUBLIC_ASSETS needs a token that may open the HMI (Authorization: Bearer):
    without one a fetch gets 401 and a page load is sent to the sign-in page.
    """
    suffix = os.path.splitext(name)[1]
    if not ASSET_NAME.match(name) or name.startswith(".") or suffix not in MIME_TYPES:
        return _not_found()
    if not hmi_installed():
        if name != "index.html":
            return _not_found()
        return _with_headers(Response(NO_HMI_PAGE, status=503, mimetype="text/html"))
    if name not in PUBLIC_ASSETS:
        refusal = hmi_access_refusal()
        if refusal is not None:
            logger.warning("HMI client file %s refused: %s", name, refusal)
            if _is_navigation():
                return _with_headers(redirect("/hmi/", code=302))
            return _with_headers(
                Response("Sign in to load the HMI", status=401, mimetype="text/plain")
            )
    data = read_sealed_client_file(name)
    if data is None:
        return _not_found()
    response = Response(data, status=200, content_type=MIME_TYPES[suffix])
    if name == CLIENT_MODULE:
        response.headers[BUILD_HEADER] = hashlib.sha256(data).hexdigest()
    return _with_headers(response)


# ---- client events ----------------------------------------------------------------------


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
class OpenEvent:
    """The viewer asks to be opened on the host."""


@dataclass(frozen=True)
class ShowEvent:
    """The viewer shows screen ``page``; NO_ID when the event was invalid. ``build``: the HMI
    build ID the client runs (``h``), None when missing or malformed."""

    page: int
    build: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class WriteEvent:
    """A screen's write of tag ``tag`` with ``value``; NO_ID and no value when invalid."""

    tag: int
    value: bytes
    build: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class BitEvent:
    """A screen's assignment of bit ``bit`` of tag ``tag`` to ``value`` (0 or 1)."""

    tag: int
    bit: int
    value: int
    build: str | None = field(default=None, compare=False)


ClientEvent = OpenEvent | ShowEvent | WriteEvent | BitEvent


def build_of(data: dict[object, object]) -> str | None:
    """The HMI build ID an event names in ``h``, or None when it is missing or malformed."""
    build = data.get("h")
    return build if isinstance(build, str) and BUILD_ID.fullmatch(build) else None


def parse_event(data: object) -> ClientEvent | None:
    """Rebuilds a client event from its whitelisted, range-checked fields.

    A show, write or bit write that does not check out is kept as one naming NO_ID, so the host refuses it
    in its place; None only for an event that is none of the three. Shows and writes keep the
    build ID they name.
    """
    if not isinstance(data, dict):
        return None
    kind = data.get("t")
    if kind == "o":
        return OpenEvent()
    build = build_of(data)
    if kind == "s":
        page = _integer(data, "p")
        return ShowEvent(page if page is not None and 0 <= page <= MAX_ID else NO_ID, build)
    if kind == "w":
        tag = _integer(data, "g")
        value = data.get("v")
        if (
            tag is None
            or not 0 <= tag <= MAX_ID
            or not isinstance(value, (bytes, bytearray))
            or len(value) > MAX_VALUE_BYTES
        ):
            return WriteEvent(NO_ID, b"", build)
        return WriteEvent(tag, bytes(value), build)
    if kind == "b":
        tag, bit, on = _integer(data, "g"), _integer(data, "b"), _integer(data, "v")
        if (
            tag is None
            or not 0 <= tag <= MAX_ID
            or bit is None
            or not 0 <= bit <= MAX_BIT
            or on not in (0, 1)
        ):
            return WriteEvent(NO_ID, b"", build)
        return BitEvent(tag, bit, on, build)
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


def encode_open(viewer: int, role: int, name: str) -> bytes:
    """An OPEN message; ``name`` through ``wire_name``, cut to 64 bytes of UTF-8 on a character
    boundary."""
    raw = wire_name(name).encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", "ignore").encode("utf-8")
    payload = struct.pack("<IB", viewer, role) + bytes([len(raw)]) + raw
    return encode(MSG_OPEN, payload)


def encode_event(viewer: int, event: ShowEvent | WriteEvent | BitEvent) -> bytes:
    """The SHOW, WRITE or BIT message of a client event."""
    if isinstance(event, ShowEvent):
        return encode(MSG_SHOW, struct.pack("<IH", viewer, event.page))
    if isinstance(event, BitEvent):
        return encode(MSG_BIT, struct.pack("<IHBB", viewer, event.tag, event.bit, event.value))
    return encode(MSG_WRITE, struct.pack("<IHH", viewer, event.tag, len(event.value)) + event.value)


@dataclass
class ValuesBatch:
    """Values for one viewer not yet delivered: the screen they are for, the host's count of
    the viewer's writes, and the newest value of each tag."""

    page: int
    writes: int
    values: dict[int, bytes]

    def pack(self) -> bytes:
        """The ``hmi_values`` payload: ``u16 page, u32 writes, u16 count`` and the records."""
        out = bytearray(struct.pack("<HIH", self.page, self.writes, len(self.values)))
        for tag in sorted(self.values):
            value = self.values[tag]
            out += struct.pack("<HH", tag, len(value)) + value
        return bytes(out)


def parse_values(payload: bytes) -> tuple[int, ValuesBatch]:
    """The viewer and batch of a VALUES payload; ProtocolError when it is malformed."""
    if len(payload) < 12:
        raise ProtocolError("VALUES too short")
    viewer, page, writes, count = struct.unpack_from("<IHIH", payload)
    values: dict[int, bytes] = {}
    off = 12
    for _ in range(count):
        if len(payload) - off < 4:
            raise ProtocolError("VALUES record cut short")
        tag, size = struct.unpack_from("<HH", payload, off)
        off += 4
        if len(payload) - off < size:
            raise ProtocolError("VALUES value cut short")
        values[tag] = payload[off : off + size]
        off += size
    if off != len(payload):
        raise ProtocolError("VALUES with trailing bytes")
    return viewer, ValuesBatch(page, writes, values)


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
    # The browser asked to be opened; it is opened again whenever the host comes back.
    wants_open: bool = False
    # Host connection epoch the viewer's OPEN went out on; None when not open on the host.
    open_epoch: int | None = None
    # Set under ``lock`` once the socket left; nothing is sent to the host for it afterwards.
    closed: bool = False
    opened: bool = False
    status: str | None = None
    next_n: int = 0
    pending: dict[int, ClientEvent | None] = field(default_factory=dict)
    batch: ValuesBatch | None = None
    batch_seq: int = 0
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

    def offer_values(self, batch: ValuesBatch) -> None:
        """Merges a batch into the values not yet delivered: newer values win; values of
        another screen are dropped."""
        if self.batch is None or self.batch.page != batch.page:
            self.batch = batch
        else:
            self.batch.values.update(batch.values)
            self.batch.writes = batch.writes
        self.batch_seq += 1

    def take_values(self, now: float) -> tuple[int, bytes] | None:
        """The values to deliver when none are in flight (or the last ones timed out)."""
        if self.batch is None:
            return None
        if self.in_flight is not None and now - self.sent_at < ACK_TIMEOUT_S:
            return None
        batch, self.batch = self.batch, None
        self.in_flight = self.batch_seq
        self.sent_at = now
        return self.batch_seq, batch.pack()

    def acknowledge(self, seq: int) -> bool:
        """Clears the batch in flight when ``seq`` is it; True when another may go."""
        if self.in_flight != seq:
            return False
        self.in_flight = None
        return self.batch is not None


EmitFn = Callable[[str, object, str, Callable[..., None] | None], None]
DisconnectFn = Callable[[str], None]


class HmiProxy:
    """The viewers, the host connection and the values emitter of /api/hmi.

    ``_epoch`` counts host connections and changes with ``_host`` under both ``_send_lock`` and
    ``_lock``. ``_build`` is the HMI build ID read from the seal when the host connected: the build the
    running host serves, sent with ``live``; shows and writes tagged with another build are
    dropped.
    """

    def __init__(self, app: Flask, emit: EmitFn, disconnect: DisconnectFn) -> None:
        self._app = app
        self._emit = emit
        self._disconnect = disconnect
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._values = threading.Condition(self._lock)
        self._viewers: dict[str, Viewer] = {}
        self._by_id: dict[int, Viewer] = {}
        self._next_id = 1
        self._host: socket.socket | None = None
        self._epoch = 0
        self._build: str | None = None
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
            self._values.notify_all()
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
        elif (isinstance(event, ShowEvent) and event.page == NO_ID) or (
            isinstance(event, WriteEvent) and event.tag == NO_ID
        ):
            logger.warning(
                "HMI viewer %s: invalid event %d forwarded as a request to refuse",
                viewer.username,
                n,
            )
        with viewer.lock:
            for ready in viewer.order(n, event):
                self._forward(viewer, ready)

    def _open_on_host(self, viewer: Viewer) -> bool:
        """Whether the viewer's OPEN went out on the current host connection."""
        return viewer.open_epoch is not None and viewer.open_epoch == self._epoch

    def _forward(self, viewer: Viewer, event: ClientEvent) -> None:
        """Sends one ordered event to the host; call with ``viewer.lock`` held.

        A show or write goes only on the connection the viewer was opened on and only when it
        names the build that connection serves; otherwise it is dropped.
        """
        if viewer.closed:
            return
        if isinstance(event, OpenEvent):
            viewer.wants_open = True
            if self._open_on_host(viewer):
                self._send_host(encode(MSG_CLOSE, struct.pack("<I", viewer.viewer_id)))
                viewer.open_epoch = None
                viewer.opened = False
            self._open(viewer)
            return
        with self._lock:
            epoch, build = self._epoch, self._build
        if viewer.open_epoch is None or viewer.open_epoch != epoch:
            return
        if build is None or event.build != build:
            logger.warning(
                "HMI viewer %s: %s of HMI build %s dropped, the host serves %s",
                viewer.username,
                "show" if isinstance(event, ShowEvent) else "write",
                (event.build or "none")[:12],
                (build or "none")[:12],
            )
            return
        self._send_host_on(encode_event(viewer.viewer_id, event), epoch)

    def _open(self, viewer: Viewer) -> None:
        """Sends OPEN; call with ``viewer.lock`` held."""
        if viewer.closed or not viewer.wants_open:
            return
        with self._values:
            viewer.batch = None
        message = encode_open(viewer.viewer_id, ROLE_CODES[viewer.role], viewer.username)
        viewer.open_epoch = self._send_host_on(message)

    # -- statuses and values --

    def _offline_state(self) -> str:
        return STATE_STOPPED if hmi_installed() else STATE_UNAVAILABLE

    def _set_status(self, viewer: Viewer, state: str, force: bool = False) -> None:
        with self._lock:
            if viewer.status == state and not force:
                return
            viewer.status = state
            build = self._build
        payload: dict[str, object] = {"state": state}
        if state == STATE_LIVE:
            payload["role"] = ROLE_CODES[viewer.role]
            payload["build"] = build
        self._emit("hmi_status", payload, viewer.sid, None)

    def _emit_loop(self) -> None:
        while not self._stop.is_set():
            batch: list[tuple[Viewer, int, bytes]] = []
            with self._values:
                now = time.monotonic()
                wait = ACK_TIMEOUT_S
                for viewer in self._viewers.values():
                    taken = viewer.take_values(now)
                    if taken is not None:
                        batch.append((viewer, taken[0], taken[1]))
                    elif viewer.batch is not None:
                        wait = min(wait, max(0.01, ACK_TIMEOUT_S - (now - viewer.sent_at)))
                if not batch:
                    self._values.wait(timeout=wait)
                    continue
            for viewer, seq, payload in batch:
                self._emit("hmi_values", payload, viewer.sid, self._ack_callback(viewer, seq))

    def _ack_callback(self, viewer: Viewer, seq: int) -> Callable[..., None]:
        def acked(*_args: object) -> None:
            with self._values:
                if viewer.acknowledge(seq):
                    self._values.notify_all()

        return acked

    # -- host connection --

    def _send_host(self, message: bytes) -> bool:
        """Sends one message to the host; False when it is not connected or the send failed."""
        return self._send_host_on(message) is not None

    def _send_host_on(self, message: bytes, only_epoch: int | None = None) -> int | None:
        """Sends one message to the host; the epoch of the connection it went out on, or None.

        With ``only_epoch``, sends only while that connection is the current one.
        """
        with self._send_lock:
            host, epoch = self._host, self._epoch
            if host is None or (only_epoch is not None and only_epoch != epoch):
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
        build = hmi_build_id()
        with self._send_lock, self._lock:
            self._host = sock
            self._epoch += 1
            self._build = build
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
            with self._values:
                viewer.batch = None
            self._set_status(viewer, state)

    def _read_host(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            message = read_message(sock, self._stop)
            if message is None:
                logger.info("HMI host closed the connection")
                return
            self._dispatch(*message)

    def _viewer_by_id(self, payload: bytes) -> Viewer | None:
        with self._lock:
            return self._by_id.get(struct.unpack_from("<I", payload)[0])

    def _dispatch(self, msg_type: int, payload: bytes) -> None:
        if msg_type == MSG_VALUES:
            viewer_id, batch = parse_values(payload)
            with self._values:
                viewer = self._by_id.get(viewer_id)
                if viewer is not None:
                    viewer.offer_values(batch)
                    self._values.notify_all()
        elif msg_type == MSG_STATUS:
            if len(payload) != 1 or payload[0] not in (HOST_STATUS_LIVE, HOST_STATUS_OUT_OF_DATE):
                raise ProtocolError("bad STATUS")
            self._host_status(payload[0])
        elif msg_type == MSG_REFUSED:
            if len(payload) != 7 or payload[4] not in REFUSED_WHAT:
                raise ProtocolError("bad REFUSED")
            viewer = self._viewer_by_id(payload)
            if viewer is not None:
                what, which = REFUSED_WHAT[payload[4]], struct.unpack_from("<H", payload, 5)[0]
                logger.warning(
                    "HMI viewer %s: the host refused %s %d", viewer.username, what, which
                )
                self._emit("hmi_refused", {"what": what, "id": which}, viewer.sid, None)
        elif msg_type in (MSG_OPENED, MSG_BUSY):
            if len(payload) != 4:
                raise ProtocolError("bad OPENED/BUSY")
            viewer = self._viewer_by_id(payload)
            if viewer is None:
                return
            with viewer.lock:
                viewer.opened = msg_type == MSG_OPENED
                if msg_type != MSG_OPENED:
                    viewer.open_epoch = None
                    viewer.wants_open = False
            if msg_type == MSG_OPENED:
                self._set_status(viewer, STATE_LIVE, force=True)
            else:
                logger.warning("HMI host busy, viewer %s not served", viewer.username)
                with self._values:
                    viewer.batch = None
                self._set_status(viewer, STATE_BUSY, force=True)
        else:
            raise ProtocolError(f"unknown type 0x{msg_type:02x}")

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
        request.environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        refusal = hmi_access_refusal()
        if refusal is not None:
            logger.warning("HMI viewer refused: %s", refusal)
            raise SocketRefused("unauthorized")
        role = current_user.role
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
