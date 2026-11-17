# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The HMI on the web server (webserver/hmi_proxy.py), against a fake host.

The fake host listens on an owner-only Unix socket and speaks the binary protocol of
docs/HMI.md. Pinned here: the sealed client files and their headers, the viewer token
rules at connect and while connected, the rebuilt and ordered client events, latest-frame
delivery, and the statuses a viewer sees as the host comes and goes.
"""

import hashlib
import math
import os
import random
import shutil
import socket
import struct
import tempfile
import threading
import time
from datetime import timedelta

import pytest
from jwt import InvalidSignatureError
from sqlalchemy.exc import OperationalError

flask_socketio = pytest.importorskip("flask_socketio", reason="flask_socketio not installed")

from conftest import auth, create_user, token_for
from flask_jwt_extended import create_access_token, decode_token

from webserver import hmi_proxy as hp
from webserver import restapi

NS = hp.NAMESPACE
ROLES = ("admin", "engineer", "operator", "viewer")
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; "
        "img-src 'self' data:; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---- fake host ------------------------------------------------------------------------


class FakeHost:
    """The HMI host side of hmi.socket: one connection at a time, messages recorded."""

    def __init__(self, path, reply="opened", mode=0o600):
        self.path = path
        self.reply = reply
        self.mode = mode
        self.messages = []
        self.connections = 0
        self.conn = None
        self.lock = threading.Lock()
        self.srv = None
        self.thread = None
        self.running = False

    def start(self):
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        os.chmod(self.path, self.mode)
        self.srv.listen(1)
        self.srv.settimeout(0.05)
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        """The PLC stopped: the socket goes away with its connection."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        with self.lock:
            if self.conn:
                try:
                    self.conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.conn.close()
                self.conn = None
        self.srv.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def send(self, msg_type, payload=b""):
        with self.lock:
            self.conn.sendall(struct.pack("<IB", len(payload) + 1, msg_type) + payload)

    def send_raw(self, raw):
        with self.lock:
            self.conn.sendall(raw)

    def of_type(self, msg_type):
        return [payload for t, payload in list(self.messages) if t == msg_type]

    def _serve(self):
        while self.running:
            try:
                conn, _ = self.srv.accept()
            except (TimeoutError, OSError):
                continue
            conn.settimeout(0.05)
            with self.lock:
                self.conn = conn
                self.connections += 1
            self._read(conn)

    def _read(self, conn):
        buf = b""
        while self.running:
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while len(buf) >= 5:
                length, msg_type = struct.unpack_from("<IB", buf)
                if len(buf) < 4 + length:
                    break
                payload = buf[5 : 4 + length]
                buf = buf[4 + length :]
                self.messages.append((msg_type, payload))
                if msg_type == hp.MSG_OPEN:
                    self._answer_open(payload)

    def _answer_open(self, payload):
        viewer = payload[:4]
        if self.reply == "opened":
            self.send(hp.MSG_OPENED, viewer)
        elif self.reply == "busy":
            self.send(hp.MSG_BUSY, viewer)
        elif self.reply == "out_of_date":
            self.send(hp.MSG_STATUS, bytes([1]))


def decode_open(payload):
    viewer, role, w, h, d, name_len = struct.unpack_from("<IBHHHB", payload)
    name = payload[12 : 12 + name_len].decode()
    assert len(payload) == 12 + name_len
    return {"viewer": viewer, "role": role, "w": w, "h": h, "d": d, "name": name}


# ---- fixtures -------------------------------------------------------------------------


@pytest.fixture()
def hmi_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(hp, "HMI_DIR", tmp_path / "hmi")
    return tmp_path / "hmi"


def install(hmi_dir, files=None, seal=None, plugin=True):
    """A built HMI: plugin, client files and a seal over all of them."""
    files = {"index.html": b"<!doctype html><title>hmi</title>"} if files is None else files
    client = hmi_dir / "client"
    client.mkdir(parents=True, exist_ok=True)
    lines = []
    if plugin:
        elf = b"\x7fELF"
        (hmi_dir / "libhmi_plugin.so").write_bytes(elf)
        lines.append(f"{hashlib.sha256(elf).hexdigest()}  libhmi_plugin.so")
    for name, data in files.items():
        (client / name).write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  client/{name}")
    (hmi_dir / "hmi.seal").write_text("\n".join(lines if seal is None else seal) + "\n")


@pytest.fixture()
def sock_path(monkeypatch):
    # AF_UNIX paths are short (104 bytes on macOS), so not under pytest's tmp_path.
    directory = tempfile.mkdtemp(prefix="hmi", dir="/tmp")
    path = os.path.join(directory, "hmi.socket")
    monkeypatch.setattr(hp, "HMI_SOCKET_PATH", path)
    yield path
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture()
def fast(monkeypatch):
    monkeypatch.setattr(hp, "RETRY_INTERVAL_S", 0.02)
    monkeypatch.setattr(hp, "ACK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(hp, "REVERIFY_INTERVAL_S", 3600.0)


@pytest.fixture()
def sio(app, fast, sock_path, hmi_dir):
    socketio = flask_socketio.SocketIO(app, async_mode="threading")
    proxy = hp.init_hmi_proxy(app, socketio)
    yield socketio
    proxy.shutdown()


@pytest.fixture()
def host(sock_path, hmi_dir):
    install(hmi_dir)
    fake = FakeHost(sock_path).start()
    yield fake
    if fake.running:
        fake.stop()


@pytest.fixture()
def tokens(client, admin_token):
    out = {"admin": admin_token}
    for role in ROLES[1:]:
        assert create_user(client, role, "pw", token=admin_token, role=role).status_code == 201
        out[role] = token_for(client, role, "pw")
    return out


@pytest.fixture()
def warnings(monkeypatch):
    lines = []
    monkeypatch.setattr(hp.logger, "warning", lambda msg, *a: lines.append(msg % a))
    return lines


def viewer_client(sio, app, token):
    return sio.test_client(app, namespace=NS, auth={"token": token})


def statuses(ws):
    return [e["args"][0]["state"] for e in ws.get_received(NS) if e["name"] == "hmi_status"]


def wait_status(ws, state, seen=None):
    seen = [] if seen is None else seen

    def check():
        seen.extend(statuses(ws))
        return state in seen

    assert wait_for(check), seen
    return seen


def frames(ws):
    return [e["args"][0] for e in ws.get_received(NS) if e["name"] == "dl"]


def open_viewer(sio, app, token, host, w=800, h=600, d=1.5):
    ws = viewer_client(sio, app, token)
    assert ws.is_connected(NS)
    assert wait_for(lambda: host.connections >= 1)
    before = len(host.of_type(hp.MSG_OPEN))
    ws.emit("in", {"t": "v", "w": w, "h": h, "d": d, "n": 0}, namespace=NS)
    assert wait_for(lambda: len(host.of_type(hp.MSG_OPEN)) > before)
    return ws, decode_open(host.of_type(hp.MSG_OPEN)[-1])


# ---- client files ---------------------------------------------------------------------


def test_hmi_redirects_to_the_client_folder(client, hmi_dir):
    resp = client.get("/hmi")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/hmi/")


def test_index_is_served_with_the_security_headers(client, hmi_dir):
    install(hmi_dir)
    resp = client.get("/hmi/")
    assert resp.status_code == 200
    assert resp.data == b"<!doctype html><title>hmi</title>"
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
    for name, value in HEADERS.items():
        assert resp.headers[name] == value


@pytest.mark.parametrize(
    "name,ctype",
    [
        ("app.js", "text/javascript; charset=utf-8"),
        ("style.css", "text/css; charset=utf-8"),
        ("raster.wasm", "application/wasm"),
        ("licenses.txt", "text/plain; charset=utf-8"),
        ("font-1.ttf", "font/ttf"),
        ("index.html", "text/html; charset=utf-8"),
    ],
)
def test_each_allowed_type_is_served_with_its_mime_type(client, hmi_dir, name, ctype):
    install(hmi_dir, {name: b"body"})
    resp = client.get(f"/hmi/{name}")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == ctype
    assert resp.data == b"body"


@pytest.mark.parametrize(
    "path",
    [
        "/hmi/evil.php",
        "/hmi/.hidden.js",
        "/hmi/noext",
        "/hmi/a%20b.js",
        "/hmi/" + "a" * 62 + ".js",
        "/hmi/sub/app.js",
        "/hmi/..%2Fhmi.seal",
        "/hmi/missing.js",
    ],
)
def test_names_outside_the_whitelist_are_not_found(client, hmi_dir, path):
    install(hmi_dir, {"app.js": b"x", "evil.php": b"x", "noext": b"x"})
    resp = client.get(path)
    assert resp.status_code == 404
    if resp.headers.get("Content-Security-Policy") is not None:
        assert resp.headers["Content-Security-Policy"] == HEADERS["Content-Security-Policy"]


def test_a_file_that_does_not_match_the_seal_is_refused_and_logged(client, hmi_dir, warnings):
    install(hmi_dir, {"index.html": b"good", "app.js": b"good"})
    (hmi_dir / "client" / "app.js").write_bytes(b"tampered")
    assert client.get("/hmi/app.js").status_code == 404
    assert any("does not match the seal" in line for line in warnings)
    assert client.get("/hmi/").status_code == 200


def test_a_file_missing_from_the_seal_is_refused_and_logged(client, hmi_dir, warnings):
    install(hmi_dir, {"index.html": b"good"})
    (hmi_dir / "client" / "extra.js").write_bytes(b"unsealed")
    assert client.get("/hmi/extra.js").status_code == 404
    assert any("not in the seal" in line for line in warnings)


def test_malformed_seal_lines_seal_nothing(client, hmi_dir):
    digest = hashlib.sha256(b"x").hexdigest()
    install(
        hmi_dir,
        {"a.js": b"x", "b.js": b"x", "c.js": b"x"},
        seal=[f"{digest} client/a.js", f"{digest.upper()}  client/b.js", f"{digest}  c.js"],
    )
    for name in ("a.js", "b.js", "c.js"):
        assert client.get(f"/hmi/{name}").status_code == 404


def test_an_unreadable_seal_refuses_every_file(client, hmi_dir, warnings):
    install(hmi_dir, {"index.html": b"x"})
    (hmi_dir / "hmi.seal").write_bytes(b"\xff\xfe")
    assert client.get("/hmi/").status_code == 404
    assert any("seal unreadable" in line for line in warnings)


def test_a_symlink_out_of_the_client_folder_is_refused(client, hmi_dir, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    install(hmi_dir, {"index.html": b"x"})
    (hmi_dir / "client" / "secret.txt").symlink_to(outside)
    assert client.get("/hmi/secret.txt").status_code == 404


@pytest.mark.parametrize("missing", ["plugin", "seal"])
def test_without_an_installed_hmi_the_index_is_a_self_refreshing_notice(client, hmi_dir, missing):
    install(hmi_dir, plugin=missing != "plugin")
    if missing == "seal":
        (hmi_dir / "hmi.seal").unlink()
    resp = client.get("/hmi/")
    assert resp.status_code == 503
    assert b"No HMI installed" in resp.data
    assert b"http-equiv=refresh" in resp.data
    for name, value in HEADERS.items():
        assert resp.headers[name] == value
    assert client.get("/hmi/app.js").status_code == 404


def test_the_client_files_need_no_account(client, hmi_dir):
    install(hmi_dir)
    assert client.get("/hmi/").status_code == 200


# ---- client events (pure) -------------------------------------------------------------


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"t": "v", "w": 800, "h": 600, "d": 1.5}, hp.Viewport(800, 600, 150)),
        ({"t": "v", "w": 1.4, "h": 8192, "d": 0.25}, hp.Viewport(1, 8192, 25)),
        ({"t": "v", "w": 10, "h": 10, "d": 8}, hp.Viewport(10, 10, 800)),
        ({"t": "p", "k": 0, "x": -1.5, "y": 2.25}, hp.PointerEvent(0, -150, 225)),
        ({"t": "p", "k": 2, "x": 100000, "y": 0}, hp.PointerEvent(2, 10000000, 0)),
        ({"t": "k", "c": 13, "extra": "ignored"}, hp.KeyEvent(13)),
        ({"t": "k", "c": 0x10FFFF}, hp.KeyEvent(0x10FFFF)),
    ],
)
def test_valid_events_are_rebuilt(data, expected):
    assert hp.parse_event(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        None,
        [1, 2],
        "v",
        {"t": "x"},
        {"w": 1, "h": 1, "d": 1},
        {"t": "v", "w": True, "h": 600, "d": 1},
        {"t": "v", "w": math.nan, "h": 600, "d": 1},
        {"t": "v", "w": math.inf, "h": 600, "d": 1},
        {"t": "v", "w": 0, "h": 600, "d": 1},
        {"t": "v", "w": 8193, "h": 600, "d": 1},
        {"t": "v", "w": 800, "h": -1, "d": 1},
        {"t": "v", "w": 800, "h": 600, "d": 0.2},
        {"t": "v", "w": 800, "h": 600, "d": 8.01},
        {"t": "v", "w": "800", "h": 600, "d": 1},
        {"t": "v", "w": 800, "h": 600},
        {"t": "p", "k": 3, "x": 1, "y": 1},
        {"t": "p", "k": True, "x": 1, "y": 1},
        {"t": "p", "k": 1.0, "x": 1, "y": 1},
        {"t": "p", "k": 1, "x": math.nan, "y": 1},
        {"t": "p", "k": 1, "x": 1, "y": 100000.5},
        {"t": "p", "k": 1, "x": 1},
        {"t": "k", "c": 0},
        {"t": "k", "c": 0x110000},
        {"t": "k", "c": "a"},
        {"t": "k", "c": False},
        {"t": "k", "c": 13.0},
    ],
)
def test_invalid_events_are_refused(data):
    assert hp.parse_event(data) is None


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"n": 0}, 0),
        ({"n": 2**53 - 1}, 2**53 - 1),
        ({"n": 2**53}, None),
        ({"n": -1}, None),
        ({"n": True}, None),
        ({"n": 1.0}, None),
        ({"n": "1"}, None),
        ({}, None),
        ([], None),
    ],
)
def test_sequence_numbers_are_range_checked(data, expected):
    assert hp.sequence_of(data) == expected


def test_messages_are_encoded_as_the_wire_protocol():
    assert hp.encode_event(7, hp.Viewport(800, 600, 150)) == struct.pack(
        "<IBIHHH", 11, 0x03, 7, 800, 600, 150
    )
    assert hp.encode_event(7, hp.PointerEvent(1, -150, 225)) == struct.pack(
        "<IBIBii", 14, 0x04, 7, 1, -150, 225
    )
    assert hp.encode_event(7, hp.KeyEvent(65)) == struct.pack("<IBII", 9, 0x05, 7, 65)
    opened = hp.encode_open(9, 2, hp.Viewport(1, 2, 100), "op")
    assert opened[:5] == struct.pack("<IB", len(opened) - 4, 0x01)
    assert decode_open(opened[5:]) == {
        "viewer": 9,
        "role": 2,
        "w": 1,
        "h": 2,
        "d": 100,
        "name": "op",
    }


def test_a_long_name_is_cut_to_64_bytes_on_a_character_boundary():
    name = "a" + "é" * 40
    payload = hp.encode_open(1, 0, hp.Viewport(1, 1, 100), name)[5:]
    sent = decode_open(payload)["name"]
    assert len(sent.encode()) <= 64
    assert name.startswith(sent) and len(sent.encode()) == 63


# ---- ordering (pure) ------------------------------------------------------------------


def _viewer():
    return hp.Viewer("sid", 1, "admin", "admin", "token")


def test_events_are_released_in_sequence_order():
    v = _viewer()
    a, b, c = hp.KeyEvent(1), hp.KeyEvent(2), hp.KeyEvent(3)
    assert v.order(1, b) == []
    assert v.order(2, c) == []
    assert v.order(0, a) == [a, b, c]
    assert v.order(1, b) == []
    assert v.order(3, None) == []
    assert v.order(4, a) == [a]
    assert v.order(6, a) == []
    assert v.order(6, b) == []


def test_a_full_pending_window_skips_the_missing_event_and_logs(warnings):
    v = _viewer()
    events = [hp.KeyEvent(i + 1) for i in range(hp.MAX_PENDING + 1)]
    released = []
    for i, event in enumerate(events):
        released += v.order(i + 1, event)
    assert released == events
    assert v.next_n == hp.MAX_PENDING + 2
    assert any("never arrived" in line for line in warnings)


# ---- latest-frame coalescing (pure proxy, fake emitter) --------------------------------


class SlowBrowser:
    """An emitter that records each dl and acknowledges only when told to."""

    def __init__(self):
        self.sent = []
        self.statuses = []
        self.callbacks = []

    def emit(self, event, data, sid, callback):
        if event == "dl":
            self.sent.append(data)
            self.callbacks.append(callback)
        else:
            self.statuses.append(data["state"])

    def ack(self):
        self.callbacks[-1]()


def test_a_slow_browser_only_ever_gets_the_newest_frame(app, monkeypatch, sock_path, hmi_dir):
    monkeypatch.setattr(hp, "ACK_TIMEOUT_S", 30.0)
    monkeypatch.setattr(hp, "RETRY_INTERVAL_S", 0.02)
    browser = SlowBrowser()
    proxy = hp.HmiProxy(app, browser.emit, lambda sid: None)
    proxy.start()
    assert wait_for(lambda: proxy._host_state is not None)
    try:
        viewer = proxy.add_viewer("s1", "admin", "admin", "t")
        other = proxy.add_viewer("s2", "eng", "engineer", "t")

        def frame(vid, i):
            return struct.pack("<I", vid) + bytes([i])

        proxy._dispatch(hp.MSG_FRAME, frame(viewer.viewer_id, 1))
        assert wait_for(lambda: browser.sent == [b"\x01"])
        for i in range(2, 50):
            proxy._dispatch(hp.MSG_FRAME, frame(viewer.viewer_id, i))
        proxy._dispatch(hp.MSG_FRAME, frame(other.viewer_id, 99))
        assert wait_for(lambda: b"\x63" in browser.sent)
        time.sleep(0.1)
        assert browser.sent == [b"\x01", b"\x63"]
        browser.callbacks[0]()
        assert wait_for(lambda: len(browser.sent) == 3)
        assert browser.sent[-1] == b"\x31"
        browser.callbacks[0]()
        time.sleep(0.1)
        assert len(browser.sent) == 3
        proxy._dispatch(hp.MSG_FRAME, frame(12345, 1))
        time.sleep(0.05)
        assert len(browser.sent) == 3
    finally:
        proxy.shutdown()


def test_an_unacknowledged_frame_stops_holding_back_after_the_timeout(
    app, monkeypatch, sock_path, hmi_dir
):
    monkeypatch.setattr(hp, "ACK_TIMEOUT_S", 0.1)
    browser = SlowBrowser()
    proxy = hp.HmiProxy(app, browser.emit, lambda sid: None)
    proxy.start()
    assert wait_for(lambda: proxy._host_state is not None)
    try:
        viewer = proxy.add_viewer("s1", "admin", "admin", "t")
        proxy._dispatch(hp.MSG_FRAME, struct.pack("<I", viewer.viewer_id) + b"a")
        assert wait_for(lambda: len(browser.sent) == 1)
        proxy._dispatch(hp.MSG_FRAME, struct.pack("<I", viewer.viewer_id) + b"b")
        assert wait_for(lambda: len(browser.sent) == 2)
        assert browser.sent == [b"a", b"b"]
        assert browser.statuses == ["unavailable"]
    finally:
        proxy.shutdown()


# ---- viewer authentication ------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_every_role_may_view_and_the_host_learns_role_and_account(sio, app, tokens, host, role):
    ws, opened = open_viewer(sio, app, tokens[role], host)
    assert opened["role"] == hp.ROLE_CODES[role]
    assert opened["name"] == role
    assert (opened["w"], opened["h"], opened["d"]) == (800, 600, 150)
    wait_status(ws, "live")


def _expired(app, token):
    with app.app_context():
        claims = decode_token(token, allow_expired=True)
        user = restapi.User.query.filter_by(id=claims["sub"]).one()
        session = {key: claims[key] for key in ("sid", "iat_ms")}
        return create_access_token(
            identity=user, expires_delta=timedelta(seconds=-30), additional_claims=session
        )


def test_refused_tokens_cannot_connect(sio, app, client, tokens, hmi_dir):
    assert not sio.test_client(app, namespace=NS).is_connected(NS)
    assert not viewer_client(sio, app, "").is_connected(NS)
    assert not viewer_client(sio, app, "garbage").is_connected(NS)
    assert not viewer_client(sio, app, _expired(app, tokens["operator"])).is_connected(NS)

    assert client.post("/api/logout", headers=auth(tokens["viewer"])).status_code == 200
    assert not viewer_client(sio, app, tokens["viewer"]).is_connected(NS)

    eng_id = restapi.User.query.filter_by(username="engineer").one().id
    resp = client.delete(f"/api/delete-user/{eng_id}", headers=auth(tokens["admin"]))
    assert resp.status_code == 200
    assert not viewer_client(sio, app, tokens["engineer"]).is_connected(NS)


def test_an_unknown_role_cannot_connect(sio, app, tokens, hmi_dir):
    user = restapi.User.query.filter_by(username="operator").one()
    user.role = "user"
    restapi.db.session.commit()
    assert not viewer_client(sio, app, tokens["operator"]).is_connected(NS)


def test_a_revoked_session_is_disconnected_by_the_periodic_check(sio, app, client, tokens, host):
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    keep, _ = open_viewer(sio, app, tokens["viewer"], host)
    hp.get_proxy().reverify_all()
    assert ws.is_connected(NS)

    assert client.post("/api/logout", headers=auth(tokens["operator"])).status_code == 200
    hp.get_proxy().reverify_all()
    assert not ws.is_connected(NS)
    assert keep.is_connected(NS)
    assert wait_for(lambda: len(host.of_type(hp.MSG_CLOSE)) == 1)


def test_the_periodic_check_runs_on_its_own(app, client, tokens, host, sock_path, monkeypatch):
    monkeypatch.setattr(hp, "REVERIFY_INTERVAL_S", 0.05)
    monkeypatch.setattr(hp, "RETRY_INTERVAL_S", 0.02)
    socketio = flask_socketio.SocketIO(app, async_mode="threading")
    proxy = hp.init_hmi_proxy(app, socketio)
    try:
        ws = viewer_client(socketio, app, tokens["viewer"])
        assert ws.is_connected(NS)
        assert client.post("/api/logout", headers=auth(tokens["viewer"])).status_code == 200
        assert wait_for(lambda: not ws.is_connected(NS))
    finally:
        proxy.shutdown()


def test_input_after_the_account_is_deleted_disconnects(sio, app, client, tokens, host):
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    op_id = restapi.User.query.filter_by(username="operator").one().id
    assert client.delete(f"/api/delete-user/{op_id}", headers=auth(tokens["admin"])).status_code
    ws.emit("in", {"t": "k", "c": 65, "n": 1}, namespace=NS)
    assert not ws.is_connected(NS)
    assert host.of_type(hp.MSG_KEY) == []


def test_a_role_change_closes_the_viewer(sio, app, client, tokens, host):
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    user = restapi.User.query.filter_by(username="operator").one()
    user.role = "viewer"
    restapi.db.session.commit()
    hp.get_proxy().reverify_all()
    assert not ws.is_connected(NS)


def test_a_renewed_token_keeps_the_viewer(sio, app, client, tokens, host, monkeypatch):
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    resp = client.post("/api/renew", headers=auth(tokens["operator"]))
    assert resp.status_code == 200
    hp.get_proxy().reverify_all()
    assert ws.is_connected(NS)
    assert hp.get_proxy().viewers()[0].token == resp.get_json()["access_token"]


def test_an_expired_token_on_an_open_viewer_is_not_refused(sio, app, tokens, host):
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    hp.get_proxy().viewers()[0].token = _expired(app, tokens["operator"])
    hp.get_proxy().reverify_all()
    assert ws.is_connected(NS)


def test_a_token_that_does_not_verify_disconnects(sio, app, tokens, host):
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    hp.get_proxy().viewers()[0].token = tokens["operator"][:-4] + "AAAA"
    hp.get_proxy().reverify_all()
    assert not ws.is_connected(NS)


# ---- events through the proxy ---------------------------------------------------------


def test_events_reach_the_host_rebuilt_and_in_order(sio, app, tokens, host):
    ws, opened = open_viewer(sio, app, tokens["engineer"], host)
    vid = opened["viewer"]
    ws.emit("in", {"t": "k", "c": 66, "n": 3}, namespace=NS)
    ws.emit("in", {"t": "p", "k": 0, "x": 1.25, "y": 2, "n": 1, "evil": 1}, namespace=NS)
    ws.emit("in", {"t": "p", "k": 7, "x": 1, "y": 1, "n": 2}, namespace=NS)
    ws.emit("in", {"t": "v", "w": 1024, "h": 768, "d": 2, "n": 4}, namespace=NS)
    ws.emit("in", {"t": "k", "c": 67}, namespace=NS)
    ws.emit("in", "nonsense", namespace=NS)
    assert wait_for(lambda: len(host.messages) >= 4)
    time.sleep(0.05)
    after_open = [m for m in host.messages if m[0] != hp.MSG_OPEN]
    assert after_open == [
        (hp.MSG_POINTER, struct.pack("<IBii", vid, 0, 125, 200)),
        (hp.MSG_KEY, struct.pack("<II", vid, 66)),
        (hp.MSG_VIEWPORT, struct.pack("<IHHH", vid, 1024, 768, 200)),
    ]


def test_tc15_a_thousand_press_release_pairs_arrive_in_order(sio, app, tokens, host):
    ws, opened = open_viewer(sio, app, tokens["operator"], host)
    events = []
    for i in range(1000):
        events.append({"t": "p", "k": 0, "x": i, "y": 0})
        events.append({"t": "p", "k": 2, "x": i, "y": 0})
    numbered = [dict(e, n=i + 1) for i, e in enumerate(events)]
    rng = random.Random(15)
    for start in range(0, len(numbered), 16):
        chunk = numbered[start : start + 16]
        rng.shuffle(chunk)
        numbered[start : start + 16] = chunk
    for event in numbered:
        ws.emit("in", event, namespace=NS)
    assert wait_for(lambda: len(host.of_type(hp.MSG_POINTER)) == 2000, timeout=20)
    expected = [struct.pack("<IBii", opened["viewer"], e["k"], e["x"] * 100, 0) for e in events]
    assert host.of_type(hp.MSG_POINTER) == expected
    assert ws.is_connected(NS)


def test_input_before_a_viewport_is_not_forwarded(sio, app, tokens, host):
    ws = viewer_client(sio, app, tokens["admin"])
    assert wait_for(lambda: host.connections == 1)
    ws.emit("in", {"t": "k", "c": 65, "n": 0}, namespace=NS)
    ws.emit("in", {"t": "p", "k": 0, "x": 1, "y": 1, "n": 1}, namespace=NS)
    time.sleep(0.1)
    assert host.messages == []


def test_disconnecting_closes_the_viewer_on_the_host(sio, app, tokens, host):
    ws, opened = open_viewer(sio, app, tokens["viewer"], host)
    ws.disconnect(namespace=NS)
    assert wait_for(lambda: host.of_type(hp.MSG_CLOSE) == [struct.pack("<I", opened["viewer"])])
    assert not hp.get_proxy().viewers()


def test_each_socket_gets_its_own_viewer_id(sio, app, tokens, host):
    _, first = open_viewer(sio, app, tokens["admin"], host)
    _, second = open_viewer(sio, app, tokens["admin"], host)
    assert first["viewer"] != second["viewer"]


# ---- frames and statuses through the proxy --------------------------------------------


def test_display_lists_reach_their_viewer_only(sio, app, tokens, host):
    ws, opened = open_viewer(sio, app, tokens["operator"], host)
    other, _ = open_viewer(sio, app, tokens["viewer"], host)
    other.get_received(NS)
    host.send(hp.MSG_FRAME, struct.pack("<I", opened["viewer"]) + b"HMDL-frame")
    got = []
    assert wait_for(lambda: got.extend(frames(ws)) or got)
    assert got == [b"HMDL-frame"]
    assert frames(other) == []


def test_tc11_stop_and_start_keeps_viewers_and_reopens_them(sio, app, tokens, host, sock_path):
    ws, first = open_viewer(sio, app, tokens["operator"], host, w=1280, h=720, d=2)
    ws.emit("in", {"t": "v", "w": 640, "h": 480, "d": 1, "n": 1}, namespace=NS)
    assert wait_for(lambda: len(host.of_type(hp.MSG_VIEWPORT)) == 1)
    seen = wait_status(ws, "live")

    host.stop()
    seen = wait_status(ws, "stopped", seen)
    assert ws.is_connected(NS)
    ws.emit("in", {"t": "k", "c": 65, "n": 2}, namespace=NS)

    restarted = FakeHost(sock_path).start()
    try:
        assert wait_for(lambda: len(restarted.of_type(hp.MSG_OPEN)) == 1)
        reopened = decode_open(restarted.of_type(hp.MSG_OPEN)[0])
        assert reopened == dict(first, w=640, h=480, d=100)
        assert restarted.of_type(hp.MSG_KEY) == []
        seen.clear()
        wait_status(ws, "live", seen)
    finally:
        restarted.stop()


def test_without_an_hmi_a_viewer_sees_unavailable(sio, app, tokens, hmi_dir):
    ws = viewer_client(sio, app, tokens["viewer"])
    assert ws.is_connected(NS)
    wait_status(ws, "unavailable")


def test_installed_but_stopped_reads_stopped(sio, app, tokens, hmi_dir):
    install(hmi_dir)
    ws = viewer_client(sio, app, tokens["viewer"])
    wait_status(ws, "stopped")


def test_an_out_of_date_hmi_reports_out_of_date(sio, app, tokens, host):
    host.reply = "out_of_date"
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    wait_status(ws, "out_of_date")
    late = viewer_client(sio, app, tokens["viewer"])
    wait_status(late, "out_of_date")


def test_status_live_after_out_of_date_reaches_open_viewers(sio, app, tokens, host):
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    seen = wait_status(ws, "live")
    host.send(hp.MSG_STATUS, b"\x01")
    seen = wait_status(ws, "out_of_date", seen)
    host.send(hp.MSG_STATUS, b"\x00")
    time.sleep(0.1)
    assert statuses(ws) == []
    ws.emit("in", {"t": "v", "w": 10, "h": 10, "d": 1, "n": 1}, namespace=NS)
    assert wait_for(lambda: len(host.of_type(hp.MSG_VIEWPORT)) == 1)
    host.send(hp.MSG_OPENED, host.of_type(hp.MSG_OPEN)[-1][:4])
    seen.clear()
    seen = wait_status(ws, "live", seen)
    host.send(hp.MSG_STATUS, b"\x01")
    seen = wait_status(ws, "out_of_date", seen)
    host.send(hp.MSG_OPENED, host.of_type(hp.MSG_OPEN)[-1][:4])
    host.send(hp.MSG_STATUS, b"\x00")
    seen.clear()
    wait_status(ws, "live", seen)


def test_a_busy_host_reports_busy_and_the_next_viewport_retries(sio, app, tokens, host):
    host.reply = "busy"
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    wait_status(ws, "busy")
    host.reply = "opened"
    ws.emit("in", {"t": "v", "w": 10, "h": 10, "d": 1, "n": 1}, namespace=NS)
    assert wait_for(lambda: len(host.of_type(hp.MSG_OPEN)) == 2)
    wait_status(ws, "live")
    host.send(hp.MSG_OPENED, struct.pack("<I", 4242))
    host.send(hp.MSG_BUSY, struct.pack("<I", 4242))


def test_a_screen_fault_reports_fault_to_that_viewer_and_the_next_viewport_reopens(
    sio, app, tokens, host, warnings
):
    ws, opened = open_viewer(sio, app, tokens["operator"], host)
    other, _ = open_viewer(sio, app, tokens["viewer"], host)
    seen = wait_status(ws, "live")
    wait_status(other, "live")
    viewer = struct.pack("<I", opened["viewer"])
    host.send(hp.MSG_FRAME, viewer + b"HMDL-before")
    got = []
    assert wait_for(lambda: got.extend(frames(ws)) or got)
    assert got == [b"HMDL-before"]

    host.send(hp.MSG_FAULT, viewer)
    seen.clear()
    wait_status(ws, "fault", seen)
    assert "HMI screen fault, viewer operator closed" in warnings
    time.sleep(0.1)
    assert statuses(other) == []

    ws.emit("in", {"t": "k", "c": 65, "n": 1}, namespace=NS)
    time.sleep(0.1)
    assert host.of_type(hp.MSG_KEY) == []
    ws.emit("in", {"t": "v", "w": 640, "h": 480, "d": 2, "n": 2}, namespace=NS)
    assert wait_for(lambda: len(host.of_type(hp.MSG_OPEN)) == 3)
    reopened = decode_open(host.of_type(hp.MSG_OPEN)[-1])
    assert reopened == dict(opened, w=640, h=480, d=200)
    assert host.of_type(hp.MSG_VIEWPORT) == []
    seen.clear()
    wait_status(ws, "live", seen)
    host.send(hp.MSG_FAULT, struct.pack("<I", 4242))
    time.sleep(0.1)
    assert ws.is_connected(NS) and statuses(ws) == []


def test_a_screen_sign_out_ends_that_session_and_disconnects_its_viewer(
    sio, app, client, tokens, host
):
    ws, opened = open_viewer(sio, app, tokens["operator"], host)
    other, _ = open_viewer(sio, app, tokens["viewer"], host)
    wait_status(ws, "live")
    host.send(hp.MSG_SIGN_OUT, struct.pack("<I", opened["viewer"]))
    assert wait_for(lambda: not ws.is_connected(NS))
    assert client.get("/api/whoami", headers=auth(tokens["operator"])).status_code == 401
    assert client.get("/api/whoami", headers=auth(tokens["viewer"])).status_code == 200
    assert other.is_connected(NS)
    assert wait_for(
        lambda: opened["viewer"] in [struct.unpack("<I", p)[0] for p in host.of_type(hp.MSG_CLOSE)]
    )


def test_a_sign_out_for_an_unknown_viewer_is_ignored_and_a_bad_one_drops_the_host(
    sio, app, tokens, host
):
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    host.send(hp.MSG_SIGN_OUT, struct.pack("<I", 999999))
    time.sleep(0.1)
    assert ws.is_connected(NS) and host.connections == 1
    host.send(hp.MSG_SIGN_OUT, b"\x01")
    assert wait_for(lambda: host.connections == 2)


def test_a_database_error_on_sign_out_still_signs_the_browser_out_and_keeps_the_host(
    sio, app, tokens, host, monkeypatch
):
    errors = []
    monkeypatch.setattr(hp.logger, "error", lambda msg, *a: errors.append(msg % a))

    def locked(_payload):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(hp.sessions, "sign_out", locked)
    ws, opened = open_viewer(sio, app, tokens["operator"], host)
    other, _ = open_viewer(sio, app, tokens["viewer"], host)
    host.send(hp.MSG_SIGN_OUT, struct.pack("<I", opened["viewer"]))
    assert wait_for(lambda: not ws.is_connected(NS))
    assert any("could not be ended on sign-out" in e for e in errors)
    assert "hmi-host" in [t.name for t in threading.enumerate()]
    assert other.is_connected(NS) and host.connections == 1


def test_a_message_that_cannot_be_handled_reconnects_instead_of_ending_the_host_link(
    sio, app, tokens, host, monkeypatch
):
    open_viewer(sio, app, tokens["operator"], host)
    proxy = hp.get_proxy()

    def broken(_code):
        raise RuntimeError("boom")

    monkeypatch.setattr(proxy, "_host_status", broken)
    host.send(hp.MSG_STATUS, bytes([hp.HOST_STATUS_LIVE]))
    assert wait_for(lambda: host.connections == 2)
    assert "hmi-host" in [t.name for t in threading.enumerate()]


def test_a_sign_out_token_that_no_longer_verifies_ends_the_session_by_its_id(
    sio, app, client, tokens, host, monkeypatch, warnings
):
    ws, opened = open_viewer(sio, app, tokens["operator"], host)

    def unverifiable(_token, allow_expired=False):
        raise InvalidSignatureError("signature mismatch")

    monkeypatch.setattr(hp, "decode_token", unverifiable)
    host.send(hp.MSG_SIGN_OUT, struct.pack("<I", opened["viewer"]))
    assert wait_for(lambda: not ws.is_connected(NS))
    assert client.get("/api/whoami", headers=auth(tokens["operator"])).status_code == 401
    assert any("session ended by its id" in w for w in warnings)


def test_a_fault_drops_the_frame_still_waiting_for_that_viewer(app, sock_path, hmi_dir):
    emitted = []
    proxy = hp.HmiProxy(app, lambda *args: emitted.append(args), lambda sid: None)
    viewer = proxy.add_viewer("sid", "operator", "operator", "token")
    viewer.in_flight = 1
    viewer.sent_at = time.monotonic()
    proxy._dispatch(hp.MSG_FRAME, struct.pack("<I", viewer.viewer_id) + b"HMDL-old")
    assert viewer.frame == b"HMDL-old"
    proxy._dispatch(hp.MSG_FAULT, struct.pack("<I", viewer.viewer_id))
    assert viewer.frame is None
    assert ("hmi_status", {"state": "fault"}, "sid", None) in emitted


@pytest.mark.parametrize(
    "raw",
    [
        struct.pack("<IB", 1, 0x99),
        struct.pack("<IB", 2, hp.MSG_STATUS) + b"\x07",
        struct.pack("<IB", 3, hp.MSG_STATUS) + b"\x00\x00",
        struct.pack("<IB", 3, hp.MSG_OPENED) + b"\x00\x00",
        struct.pack("<IB", 3, hp.MSG_FAULT) + b"\x00\x00",
        struct.pack("<IB", 3, hp.MSG_FRAME) + b"\x00\x00",
        struct.pack("<IB", 0, hp.MSG_FRAME),
        struct.pack("<IB", hp.MAX_MESSAGE + 1, hp.MSG_FRAME),
    ],
)
def test_a_malformed_host_message_drops_the_connection_and_reconnects(
    sio, app, tokens, host, raw, warnings
):
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    host.send_raw(raw)
    assert wait_for(lambda: host.connections == 2)
    assert wait_for(lambda: len(host.of_type(hp.MSG_OPEN)) == 2)
    assert ws.is_connected(NS)
    assert any("malformed" in line for line in warnings)


def test_a_host_socket_open_to_others_is_not_used(sio, app, tokens, sock_path, hmi_dir, warnings):
    install(hmi_dir)
    os.chmod(os.path.dirname(sock_path), 0o755)
    loose = FakeHost(sock_path).start()
    try:
        ws = viewer_client(sio, app, tokens["admin"])
        wait_status(ws, "stopped")
        time.sleep(0.1)
        assert loose.connections == 0
        assert any("not owner-only" in line for line in warnings)
    finally:
        loose.stop()


def test_a_send_failure_drops_the_host_connection(sio, app, tokens, host):
    ws, _ = open_viewer(sio, app, tokens["operator"], host)
    proxy = hp.get_proxy()
    real = proxy._host

    class Broken:
        def sendall(self, _data):
            raise BrokenPipeError("gone")

        def shutdown(self, _how):
            real.shutdown(socket.SHUT_RDWR)

        def close(self):
            real.close()

    proxy._host = Broken()
    ws.emit("in", {"t": "k", "c": 65, "n": 1}, namespace=NS)
    assert wait_for(lambda: host.connections == 2)
    assert wait_for(lambda: len(host.of_type(hp.MSG_OPEN)) == 2)


def test_input_racing_a_disconnect_never_opens_the_viewer_on_the_host(
    sio, app, tokens, host, monkeypatch
):
    viewer_client(sio, app, tokens["viewer"])
    assert wait_for(lambda: host.connections == 1)
    proxy = hp.get_proxy()
    sid = next(iter(proxy._viewers))
    checked = proxy.token_in_force

    def disconnect_meanwhile(viewer):
        proxy.remove_viewer(sid)
        return checked(viewer)

    monkeypatch.setattr(proxy, "token_in_force", disconnect_meanwhile)
    with app.app_context():
        proxy.handle_input(sid, {"t": "v", "w": 800, "h": 600, "d": 1, "n": 0})
    time.sleep(0.1)
    assert host.of_type(hp.MSG_OPEN) == []
    assert sid not in proxy._viewers


def test_viewports_racing_disconnects_leave_no_viewer_open_on_the_host(sio, app, tokens, host):
    proxy = hp.get_proxy()
    assert wait_for(lambda: host.connections == 1)
    for i in range(100):
        viewer_client(sio, app, tokens["viewer"])
        sid = list(proxy._viewers)[-1]
        start = threading.Barrier(2)

        def viewport(sid=sid):
            start.wait()
            with app.app_context():
                proxy.handle_input(sid, {"t": "v", "w": 800, "h": 600, "d": 1, "n": 0})

        def leave(sid=sid, i=i):
            start.wait()
            time.sleep((i % 20) * 0.0002)
            proxy.remove_viewer(sid)

        threads = [threading.Thread(target=viewport), threading.Thread(target=leave)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    time.sleep(0.2)
    opened = {decode_open(p)["viewer"] for p in host.of_type(hp.MSG_OPEN)}
    closed = {struct.unpack("<I", p)[0] for p in host.of_type(hp.MSG_CLOSE)}
    assert opened <= closed
    assert proxy._viewers == {}


class _ViewportOnReopen:
    """A viewer lock that lets a viewport through just as a new host connection reopens the viewer."""

    def __init__(self, real, proxy, viewport):
        self.real = real
        self.proxy = proxy
        self.viewport = viewport

    def __enter__(self):
        reopening = threading.current_thread().name == "hmi-host" and self.proxy._host is not None
        if reopening and self.viewport is not None:
            send, self.viewport = self.viewport, None
            thread = threading.Thread(target=send)
            thread.start()
            thread.join()
        return self.real.__enter__()

    def __exit__(self, *exc):
        return self.real.__exit__(*exc)


def test_a_viewport_racing_a_reconnect_opens_the_viewer_once(sio, app, tokens, host):
    open_viewer(sio, app, tokens["operator"], host)
    proxy = hp.get_proxy()
    sid = next(iter(proxy._viewers))
    viewer = proxy._viewers[sid]

    def viewport():
        with app.app_context():
            proxy.handle_input(sid, {"t": "v", "w": 640, "h": 480, "d": 1, "n": 1})

    viewer.lock = _ViewportOnReopen(viewer.lock, proxy, viewport)
    host.send_raw(struct.pack("<IB", 1, 0x7F))
    assert wait_for(lambda: host.connections == 2)
    assert wait_for(lambda: len(host.of_type(hp.MSG_OPEN)) == 2)
    time.sleep(0.2)
    opens = [decode_open(p) for p in host.of_type(hp.MSG_OPEN)]
    assert len(opens) == 2
    assert (opens[1]["w"], opens[1]["h"]) == (640, 480)


def test_an_account_name_the_host_would_refuse_is_sent_with_those_characters_replaced():
    name = "op\x01er\x7fat\x85or\ud800é"
    sent = decode_open(hp.encode_open(1, 2, hp.Viewport(1, 1, 100), name)[5:])["name"]
    assert sent == "op?er?at?or?é"


def test_a_regular_file_at_the_socket_path_is_ignored(sio, app, tokens, sock_path, hmi_dir):
    install(hmi_dir)
    with open(sock_path, "w", encoding="utf-8") as f:
        f.write("x")
    ws = viewer_client(sio, app, tokens["admin"])
    wait_status(ws, "stopped")


def test_init_twice_replaces_the_running_proxy(app, fast, sock_path, hmi_dir):
    first = hp.init_hmi_proxy(app, flask_socketio.SocketIO(app, async_mode="threading"))
    second = hp.init_hmi_proxy(app, flask_socketio.SocketIO(app, async_mode="threading"))
    try:
        assert hp.get_proxy() is second
        assert not first._threads
        second.start()
        assert len(second._threads) == 3
    finally:
        second.shutdown()
