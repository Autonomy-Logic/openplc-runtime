# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Local status channel of the plugin: protocol, socket hygiene, lifecycle, revocation."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import mqtt_plugin
import pytest
from api_schema import check
from contract.runtime_config import MqttRuntimeConfig
from mqtt_credentials.model import Binding, SecretPayload, VaultLocator
from mqtt_publisher import status_channel
from mqtt_publisher.status import StatusWriter
from mqtt_publisher.status_channel import ChannelError, StatusServer, request
from program_fakes import FakeProgram
from publisher_fakes import FakeNetwork, FakeSession, RealTime, RecordingStrategy

CONFIG_DIR = (
    Path(__file__).resolve().parents[4]
    / "core/src/drivers/plugins/python/mqtt/contract/fixtures/runtime-config"
)
GENERIC = CONFIG_DIR / "valid" / "generic-on-change-anonymous-plaintext.json"
REF = {"id": "0d9e8f7a-6b5c-4d3e-8f1a-2b3c4d5e6f70", "revision": 1, "staging_id": None}
SECRET = b"MARKER-PW-status-channel-5c1e"


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """Unix socket paths are limited to ~104 bytes; pytest tmp paths on macOS are longer."""
    path = Path(tempfile.mkdtemp(prefix="mq", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _logs() -> tuple[list[str], Callable[[str, str], None]]:
    lines: list[str] = []
    return lines, lambda level, message: lines.append(f"{level}:{message}")


def _until(predicate: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _status_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "mqtt-status"]


def _raw(path: Path, payload: bytes, timeout: float = 2.0) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(payload)
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return data
            data += chunk


@pytest.fixture
def server(short_dir: Path) -> Iterator[tuple[StatusServer, list[str]]]:
    calls: list[str] = []

    def handler(line: str) -> dict[str, object]:
        calls.append(line)
        return {"contract": "openplc-mqtt-status", "request": line}

    _, log = _logs()
    srv = StatusServer(str(short_dir / status_channel.STATUS_SOCKET_NAME), handler, log)
    assert srv.start() is True
    yield srv, calls
    srv.stop()


# --- protocol -------------------------------------------------------------------


def test_status_request_is_answered_with_the_handler_document(server) -> None:
    srv, calls = server
    reply = request(srv.path, status_channel.REQUEST_STATUS, 2.0)
    assert reply == {"contract": "openplc-mqtt-status", "request": "STATUS"}
    assert request(srv.path, status_channel.REQUEST_DROP_CREDENTIAL, 2.0)["request"] == (
        "DROP_CREDENTIAL"
    )
    assert calls == ["STATUS", "DROP_CREDENTIAL"]


def test_unknown_request_never_reaches_the_handler(server) -> None:
    srv, calls = server
    assert request(srv.path, "STOP", 2.0) == {"error": "unknown_request"}
    assert calls == []


def test_oversized_or_unterminated_request_gets_no_reply(server) -> None:
    srv, calls = server
    assert _raw(Path(srv.path), b"S" * 200) == b""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(srv.path)
        sock.sendall(b"STATUS")
        sock.shutdown(socket.SHUT_WR)
        assert sock.recv(10) == b""
    assert calls == []
    assert request(srv.path, "STATUS", 2.0)["request"] == "STATUS"


def test_a_silent_client_is_dropped_and_others_are_still_served(server) -> None:
    srv, _ = server
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as idle:
        idle.connect(srv.path)
        start = time.monotonic()
        assert request(srv.path, "STATUS", 3.0)["request"] == "STATUS"
        assert time.monotonic() - start < status_channel.CLIENT_IO_TIMEOUT_S + 1.0


def test_unencodable_reply_becomes_an_internal_error(short_dir: Path) -> None:
    lines, log = _logs()
    srv = StatusServer(str(short_dir / "s.socket"), lambda _l: {"x": object()}, log)
    assert srv.start()
    try:
        assert request(srv.path, "STATUS", 2.0) == {"error": "internal_error"}
        assert lines == ["error:the status channel could not encode its reply"]
    finally:
        srv.stop()


def test_socket_is_owner_only_and_removed_on_stop(server) -> None:
    srv, _ = server
    mode = os.lstat(srv.path).st_mode
    assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) == 0o600
    start = time.monotonic()
    assert srv.stop() is True
    assert time.monotonic() - start < 1.0
    assert not os.path.exists(srv.path)
    assert _status_threads() == []
    with pytest.raises(ChannelError):
        request(srv.path, "STATUS", 1.0)


def test_stale_socket_is_replaced(short_dir: Path) -> None:
    path = short_dir / "stale.socket"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    _, log = _logs()
    srv = StatusServer(str(path), lambda _l: {"ok": True}, log)
    assert srv.start() is True
    try:
        assert request(str(path), "STATUS", 2.0) == {"ok": True}
    finally:
        srv.stop()


def test_a_regular_file_at_the_path_is_never_deleted(short_dir: Path) -> None:
    path = short_dir / "taken.socket"
    path.write_text("keep")
    lines, log = _logs()
    srv = StatusServer(str(path), lambda _l: {}, log)
    assert srv.start() is False
    assert path.read_text() == "keep"
    assert lines == [
        "error:the status channel is not available (the status socket path is taken by another file)"
    ]


def test_too_long_path_is_refused_without_binding(short_dir: Path) -> None:
    path = short_dir / ("d" * 120) / "s.socket"
    lines, log = _logs()
    assert StatusServer(str(path), lambda _l: {}, log).start() is False
    assert "too long" in lines[0]
    with pytest.raises(ChannelError):
        request(str(path), "STATUS", 1.0)


@pytest.mark.parametrize(
    "reply",
    [b"", b"not json\n", b"[1,2]\n", b'{"a":1}', b"{" + b" " * 70_000 + b"}\n"],
    ids=["empty", "not-json", "not-object", "unterminated", "oversized"],
)
def test_client_refuses_malformed_replies(short_dir: Path, reply: bytes) -> None:
    path = short_dir / "fake.socket"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def answer() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(64)
            try:
                conn.sendall(reply)
            except OSError:
                pass

    thread = threading.Thread(target=answer)
    thread.start()
    try:
        with pytest.raises(ChannelError):
            request(str(path), "STATUS", 2.0)
    finally:
        thread.join(3)
        listener.close()


def test_socket_path_follows_the_installed_configuration() -> None:
    assert status_channel.status_socket_path("core/src/drivers/plugins/python/mqtt/mqtt.json") == (
        "core/src/drivers/plugins/python/mqtt/mqtt_status.socket"
    )


# --- plugin lifecycle and revocation ---------------------------------------------


class Harness:
    def __init__(self) -> None:
        self.network = FakeNetwork(RealTime())
        self.specs: list[str] = []

    def strategy(self, config: MqttRuntimeConfig, _writer: StatusWriter) -> RecordingStrategy:
        assert config.generic is not None
        return RecordingStrategy(interval_ms=config.generic.interval_ms)

    def transport(self, spec) -> FakeSession:  # type: ignore[no-untyped-def]
        self.specs.append(f"{spec.username}:{spec.password!r}")
        return self.network.factory(spec)

    def factory(self) -> mqtt_plugin.PublisherFactory:
        builders: dict[str, mqtt_plugin.StrategyBuilder] = {"generic": self.strategy}
        return mqtt_plugin.PublisherFactory(builders, self.transport, log=lambda *_a: None)


def _write_config(path: Path, method: str) -> None:
    doc = json.loads(GENERIC.read_text(encoding="utf-8"))
    doc["auth"] = (
        {"method": "anonymous"}
        if method == "anonymous"
        else {"method": method, "credential_ref": REF}
    )
    path.write_text(json.dumps(doc), encoding="utf-8")
    locator = VaultLocator("/v/credentials.vault", "/v/mqtt-vault.key", "/usr/bin/python3")
    path.with_name("mqtt.vault.json").write_bytes(locator.to_bytes())


def _unseal(_locator: VaultLocator, _id: str, _rev: int, _binding: Binding) -> SecretPayload:
    return SecretPayload("plc-user", SECRET, None, None)


@pytest.fixture
def plugin(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[mqtt_plugin.PluginState, Harness, list[str]]]:
    harness = Harness()
    state = mqtt_plugin.PluginState(unseal=_unseal, publishers=harness.factory())
    state.config_path = str(short_dir / "mqtt.json")
    lines, log = _logs()
    monkeypatch.setattr(mqtt_plugin, "_state", state)
    monkeypatch.setattr(mqtt_plugin, "_logging", None)
    monkeypatch.setattr(mqtt_plugin, "_log", log)
    monkeypatch.setattr(mqtt_plugin, "_program", FakeProgram.from_debug_map())
    monkeypatch.setattr(mqtt_plugin, "_channel", None)
    yield state, harness, lines
    mqtt_plugin.stop_loop()


def _socket(state: mqtt_plugin.PluginState) -> str:
    return status_channel.status_socket_path(state.config_path)


def test_the_channel_lives_exactly_between_start_and_stop(plugin) -> None:
    state, _, _ = plugin
    _write_config(Path(state.config_path), "anonymous")
    assert not os.path.exists(_socket(state))
    assert mqtt_plugin.start_loop() is True
    assert _until(lambda: state.snapshot()["state"] == "connected")
    reply = request(_socket(state), "STATUS", 2.0)
    assert check(reply, "status") == []
    assert reply["state"] == "connected" and reply["mode"] == "generic"
    assert len(_status_threads()) == 1
    assert mqtt_plugin.stop_loop() is True
    assert not os.path.exists(_socket(state)) and _status_threads() == []
    with pytest.raises(ChannelError):
        request(_socket(state), "STATUS", 1.0)


def test_a_failed_activation_is_still_visible_on_the_channel(plugin) -> None:
    state, harness, _ = plugin
    Path(state.config_path).write_bytes(
        (CONFIG_DIR / "invalid" / "unknown-version.json").read_bytes()
    )
    assert mqtt_plugin.start_loop() is False
    reply = request(_socket(state), "STATUS", 2.0)
    assert check(reply, "status") == []
    assert reply["state"] == "config_error"
    error = reply["error"]
    assert isinstance(error, dict) and error["code"] == "config_unknown_version"
    assert harness.network.sessions == []


def test_restart_replaces_the_channel_without_leaking_a_thread(plugin) -> None:
    state, _, _ = plugin
    _write_config(Path(state.config_path), "anonymous")
    for _ in range(3):
        assert mqtt_plugin.start_loop() is True
        assert request(_socket(state), "STATUS", 2.0)["configured"] is True
    assert len(_status_threads()) == 1


def test_removed_credential_stops_the_session_that_used_it(plugin) -> None:
    state, harness, _ = plugin
    _write_config(Path(state.config_path), "username_password")
    assert mqtt_plugin.start_loop() is True
    assert _until(lambda: state.snapshot()["state"] == "connected")
    assert harness.specs == [f"plc-user:{SECRET!r}"]
    start = time.monotonic()
    assert request(_socket(state), "DROP_CREDENTIAL", 7.0) == {"revoked": True}
    assert time.monotonic() - start < 5.0
    assert harness.network.sessions[0].closed
    assert state.publisher is None and state.generation is None
    assert [t for t in threading.enumerate() if t.name.startswith("mqtt-publisher-")] == []
    reply = request(_socket(state), "STATUS", 2.0)
    assert check(reply, "status") == []
    assert reply["state"] == "credential_error"
    assert reply["error"] == {
        "category": "credential",
        "code": "credential_missing",
        "message": "the credential was removed from this runtime",
        "since": reply["error"]["since"],  # type: ignore[index]
        "count": 1,
    }
    time.sleep(0.3)
    assert len(harness.network.sessions) == 1
    assert SECRET.decode() not in json.dumps(reply)


def test_anonymous_session_is_unaffected_by_a_credential_removal(plugin) -> None:
    state, harness, _ = plugin
    _write_config(Path(state.config_path), "anonymous")
    assert mqtt_plugin.start_loop() is True
    assert _until(lambda: state.snapshot()["state"] == "connected")
    assert request(_socket(state), "DROP_CREDENTIAL", 7.0) == {"revoked": False}
    assert state.publisher is not None and state.publisher.alive
    assert not harness.network.sessions[0].closed


def test_removal_without_a_generation_changes_nothing(plugin) -> None:
    state, _, _ = plugin
    assert state.revoke_credential() is False
    assert state.snapshot()["state"] == "disabled"
