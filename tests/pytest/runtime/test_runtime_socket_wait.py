# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""The webserver waits for a freshly started runtime to open its command socket."""

import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from webserver import runtimemanager
from webserver.runtimemanager import RuntimeManager


@pytest.fixture
def run_dir():
    with tempfile.TemporaryDirectory(prefix="rtm", dir="/tmp") as d:
        yield Path(d)


def _manager(run_dir: Path) -> RuntimeManager:
    return RuntimeManager(
        runtime_path="/bin/true",
        plc_socket=str(run_dir / "plc.sock"),
        log_socket=str(run_dir / "log.sock"),
    )


def _listen_later(path: str, delay: float) -> threading.Thread:
    def serve() -> None:
        time.sleep(delay)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        conn, _ = srv.accept()
        time.sleep(0.5)
        conn.close()
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def _errors_about(caplog, run_dir: Path) -> list[str]:
    """ERROR records about this test's socket; the app's own runtime manager logs too."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "ERROR" and str(run_dir) in r.getMessage()
    ]


def test_connects_once_the_socket_appears_without_errors(run_dir: Path, monkeypatch, caplog) -> None:
    manager = _manager(run_dir)
    monkeypatch.setattr(manager, "is_runtime_alive", lambda: True)
    _listen_later(manager.plc_socket, 0.6)
    with caplog.at_level("ERROR"):
        manager._connect_runtime_socket_when_ready()
    assert manager.runtime_socket.is_connected()
    assert not _errors_about(caplog, run_dir)
    manager.runtime_socket.close()


def test_reports_once_when_the_runtime_never_listens(run_dir: Path, monkeypatch, caplog) -> None:
    manager = _manager(run_dir)
    monkeypatch.setattr(manager, "is_runtime_alive", lambda: True)
    monkeypatch.setattr(runtimemanager, "RUNTIME_SOCKET_WAIT_S", 0.5)
    with caplog.at_level("ERROR"):
        manager._connect_runtime_socket_when_ready()
    assert not manager.runtime_socket.is_connected()
    failures = _errors_about(caplog, run_dir)
    assert len(failures) == 1 and "Failed to connect to runtime socket" in failures[0]


def test_stops_waiting_when_the_runtime_exits(run_dir: Path, monkeypatch) -> None:
    manager = _manager(run_dir)
    monkeypatch.setattr(manager, "is_runtime_alive", lambda: False)
    started = time.monotonic()
    manager._connect_runtime_socket_when_ready()
    assert time.monotonic() - started < 1.0
