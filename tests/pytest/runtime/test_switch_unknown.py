# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The webserver reports an unknown mode switch and gives START the time a gated start takes."""

import inspect
from typing import Optional

import pytest

from webserver.runtimemanager import RuntimeManager
from webserver.unixclient import SyncUnixClient

START_TIMEOUT_S = 15.0


class _RecordingSocket:
    """Stands in for the connected runtime socket: answers one line, records what it was asked."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.sent = b""
        self.timeout: Optional[float] = None
        self.recv_timeout: Optional[float] = None

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, timeout: Optional[float]) -> None:
        self.timeout = timeout

    def recv(self, size: int) -> bytes:
        self.recv_timeout = self.timeout
        reply, self.reply = self.reply, b""
        return reply

    def close(self) -> None:
        pass


def _manager_answering(reply: bytes) -> tuple[RuntimeManager, _RecordingSocket]:
    manager = RuntimeManager(
        runtime_path="/bin/true",
        plc_socket="/nonexistent/plc.sock",
        log_socket="/nonexistent/log.sock",
    )
    fake = _RecordingSocket(reply)
    manager.runtime_socket.sock = fake
    return manager, fake


class _UnknownSwitchRuntime:
    """A runtime whose switch owner has not reported a position."""

    def status_plc(self) -> str:
        return "STATUS:STOPPED"

    def switch_plc(self) -> str:
        return "SWITCH:UNKNOWN"

    def stats_plc(self) -> str:
        return "STATS:{}"


@pytest.mark.parametrize(
    ("reply", "position"),
    [("SWITCH:UNKNOWN\n", "unknown"), ("SWITCH:RUN\n", "run"), ("SWITCH:STOP\n", "stop")],
)
def test_parse_switch_position_maps_each_reply(reply: str, position: str) -> None:
    from webserver import app as app_module

    assert app_module.parse_switch_position(reply) == position


def test_status_reports_switch_position_unknown(monkeypatch) -> None:
    from webserver import app as app_module

    monkeypatch.setattr(app_module, "runtime_manager", _UnknownSwitchRuntime())

    assert app_module.handle_status({})["switchPosition"] == "unknown"


def test_start_waits_up_to_15_seconds_for_the_reply() -> None:
    manager, sock = _manager_answering(b"START:ERROR_SWITCH_UNKNOWN\n")

    reply = manager.start_plc()

    assert sock.sent == b"START\n"
    assert sock.recv_timeout == START_TIMEOUT_S
    assert reply == "START:ERROR_SWITCH_UNKNOWN"


def test_status_keeps_the_default_reply_timeout() -> None:
    default = inspect.signature(SyncUnixClient.send_and_receive).parameters["timeout"].default
    manager, sock = _manager_answering(b"STATUS:STOPPED\n")

    manager.status_plc()

    assert sock.sent == b"STATUS\n"
    assert sock.recv_timeout == default
