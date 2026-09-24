# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""EtherDOG supervision helpers: legacy upload detection, command transport, route shaping."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from webserver import etherdog_manager
from webserver.discovery.discovery_routes import _with_plugin_state
from webserver.etherdog_manager import (
    EtherDogManager,
    EtherDogUnavailable,
    legacy_ethercat_config_in_use,
)
from webserver.plugin_config_model import PluginsConfiguration


def test_legacy_config_detected_only_with_masters(tmp_path: Path) -> None:
    assert not legacy_ethercat_config_in_use(tmp_path)

    legacy = tmp_path / "ethercat.json"
    legacy.write_text("")
    assert not legacy_ethercat_config_in_use(tmp_path)

    legacy.write_text("[]")
    assert not legacy_ethercat_config_in_use(tmp_path)

    legacy.write_text(json.dumps([{"name": "m", "protocol": "ETHERCAT", "config": {}}]))
    assert legacy_ethercat_config_in_use(tmp_path)


def test_plugin_state_added_next_to_state() -> None:
    result = _with_plugin_state({"masters": [{"name": "m", "state": "OPERATIONAL"}]})
    assert result["masters"][0]["plugin_state"] == "OPERATIONAL"
    assert _with_plugin_state({"error": "x"}) == {"error": "x"}


def test_ethercat_plugin_reads_iomapping_file(tmp_path: Path) -> None:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "ethercat_iomapping.json").write_text('{"version": 1, "masters": []}')
    (conf_dir / "ethercat_busconfig.json").write_text("[]")
    plugins = tmp_path / "plugins.conf"
    plugins.write_text("ethercat,./build/plugins/libethercat_plugin.so,0,1,,\n")

    config = PluginsConfiguration.from_file(str(plugins))
    config.update_plugins_from_config_dir(str(conf_dir))
    ethercat = next(p for p in config.plugins if p.name == "ethercat")
    assert ethercat.enabled
    assert ethercat.config_path.endswith("ethercat_iomapping.json")


class FakeEtherDog:
    """Unix-socket server that records each command and echoes it back."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.received: list[str] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn, conn.makefile("rw", encoding="utf-8") as f:
                for line in f:
                    command = json.loads(line)["command"]
                    self.received.append(command)
                    f.write(json.dumps({"status": "success", "echo": command}) + "\n")
                    f.flush()

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def run_dir():
    with tempfile.TemporaryDirectory(prefix="edog", dir="/tmp") as d:
        yield Path(d)


def _manager(run_dir: Path) -> EtherDogManager:
    binary = run_dir / "etherdog-bin"
    binary.write_text("")
    return EtherDogManager(binary=str(binary), run_dir=run_dir, busconfig=run_dir / "bus.json")


def test_command_says_hello_and_returns_reply(run_dir: Path) -> None:
    server = FakeEtherDog(str(run_dir / "etherdog.socket"))
    try:
        reply = _manager(run_dir).command({"command": "status"})
        assert reply == {"status": "success", "echo": "status"}
        assert server.received == ["hello", "status"]
    finally:
        server.close()


def test_session_file_names_endpoint_transport_and_busconfig(run_dir: Path) -> None:
    manager = _manager(run_dir)
    manager._write_session()
    session = json.loads(manager.paths.session_file.read_text())
    assert session["control"] == f"unix:{run_dir / 'etherdog.socket'}"
    assert session["data"] in ("unix", "udp")
    assert session["busconfig"] == str((run_dir / "bus.json").resolve())
    assert "token" not in session
    assert os.stat(manager.paths.session_file).st_mode & 0o077 == 0


def test_session_file_is_not_written_through_a_symlink(run_dir: Path) -> None:
    manager = _manager(run_dir)
    target = run_dir / "elsewhere"
    target.write_text("keep")
    manager.paths.session_file.symlink_to(target)
    with pytest.raises(OSError):
        manager._write_session()
    assert target.read_text() == "keep"


def test_upload_stops_the_bus_and_only_stages_the_config(run_dir: Path) -> None:
    server = FakeEtherDog(str(run_dir / "etherdog.socket"))
    manager = _manager(run_dir)
    manager._running = True
    manager._process = subprocess.Popen(["sleep", "30"])
    source = run_dir / "upload.json"
    source.write_text("[]")
    try:
        manager.apply_busconfig(source)
        assert (run_dir / "bus.json").read_text() == "[]"
        assert server.received == ["hello", "stop"]  # the plugin configures and starts it

        manager.apply_busconfig(None)
        assert not (run_dir / "bus.json").exists()
    finally:
        manager._process.kill()
        manager._process.wait()
        server.close()


def test_leftover_etherdog_is_killed(run_dir: Path) -> None:
    binary = run_dir / "etherdog-bin"
    shutil.copy(shutil.which("sleep"), binary)
    leftover = subprocess.Popen([str(binary), "30"])
    try:
        manager = EtherDogManager(binary=str(binary), run_dir=run_dir)
        assert etherdog_manager._processes_running(str(binary)) == [leftover.pid]
        manager._kill_leftovers()
        assert leftover.wait(timeout=5) != 0
    finally:
        if leftover.poll() is None:
            leftover.kill()


def test_startup_errors_on_stderr_are_logged_as_errors(run_dir: Path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(etherdog_manager, "READY_TIMEOUT_S", 0.3)
    manager = _fake_binary(
        run_dir, "echo '[2026-01-01 00:00:00] [ERROR] [BUS] bad config'; sleep 30"
    )
    try:
        with caplog.at_level("ERROR"):
            manager._spawn()
            assert _wait_for(lambda: any("bad config" in r.message for r in caplog.records))
        record = next(r for r in caplog.records if "bad config" in r.message)
        assert record.levelname == "ERROR"
        assert record.message == "[BUS] bad config"
    finally:
        manager._process.kill()
        manager._process.wait()


def test_stop_reaps_a_process_it_had_to_kill(run_dir: Path, monkeypatch) -> None:
    manager = _fake_binary(run_dir, "trap '' TERM INT; sleep 30")
    manager._process = subprocess.Popen([manager.paths.binary])
    manager._running = True
    monkeypatch.setattr(manager, "command", lambda *a, **k: {})
    real_wait = manager._process.wait

    def short_wait(timeout=None):
        return real_wait(timeout=0.2 if timeout == 10 else timeout)

    monkeypatch.setattr(manager._process, "wait", short_wait)
    manager.stop()
    assert manager._process.returncode is not None


def _fake_binary(run_dir: Path, body: str) -> EtherDogManager:
    """An executable that counts its launches in <run_dir>/launches, then runs @body."""
    binary = run_dir / "etherdog-bin"
    binary.write_text(f'#!/bin/sh\necho x >> "{run_dir}/launches"\n{body}\n')
    binary.chmod(0o755)
    return EtherDogManager(binary=str(binary), run_dir=run_dir)


def _launches(run_dir: Path) -> int:
    path = run_dir / "launches"
    return len(path.read_text().splitlines()) if path.exists() else 0


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _session(manager: EtherDogManager) -> dict:
    return json.loads(manager.paths.session_file.read_text())


def test_missing_binary_disables_without_raising(run_dir: Path) -> None:
    manager = EtherDogManager(binary=str(run_dir / "absent"), run_dir=run_dir)
    manager.start()
    assert "not installed" in manager.disabled_reason
    assert "not installed" in _session(manager)["disabled"]
    assert "not installed" in manager.plugin_style_command({"command": "status"}, 1.0)["error"]


def test_missing_npcap_disables_after_one_exit(run_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(etherdog_manager, "IS_WINDOWS", True)
    manager = _fake_binary(
        run_dir,
        "echo 'etherdog.exe: error while loading shared libraries: wpcap.dll: "
        "cannot open shared object file' >&2; exit 127",
    )
    manager.start()
    assert _wait_for(lambda: manager.disabled_reason is not None)
    assert manager.disabled_reason == etherdog_manager.NPCAP_REASON
    assert _session(manager)["disabled"] == etherdog_manager.NPCAP_REASON
    time.sleep(0.5)
    assert _launches(run_dir) == 1


def test_missing_library_on_linux_is_fatal(run_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(etherdog_manager, "IS_WINDOWS", False)
    manager = _fake_binary(
        run_dir, "echo 'libfoo.so: cannot open shared object file' >&2; exit 127"
    )
    manager.start()
    assert _wait_for(lambda: manager.disabled_reason is not None)
    assert "libfoo.so" in manager.disabled_reason
    assert _launches(run_dir) == 1


def test_rapid_exits_disable_after_limit(run_dir: Path) -> None:
    manager = _fake_binary(run_dir, "exit 1")
    manager.start()
    assert _wait_for(lambda: manager.disabled_reason is not None)
    assert f"{etherdog_manager.MAX_RAPID_EXITS} times" in manager.disabled_reason
    assert _launches(run_dir) == etherdog_manager.MAX_RAPID_EXITS


def test_restart_is_immediate(run_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(etherdog_manager, "READY_TIMEOUT_S", 0.2)
    # First launch exits, the second stays up
    manager = _fake_binary(
        run_dir, f'[ "$(wc -l < "{run_dir}/launches")" -gt 1 ] && exec sleep 30\nexit 1'
    )
    started = time.monotonic()
    manager.start()
    try:
        assert _wait_for(lambda: _launches(run_dir) == 2, timeout=3.0)
        assert time.monotonic() - started < 2.0
        assert manager.disabled_reason is None
    finally:
        manager._running = False
        if manager._process is not None:
            manager._process.kill()


def test_new_program_retries_disabled_etherdog(run_dir: Path) -> None:
    manager = _fake_binary(run_dir, "exit 1")
    manager.start()
    assert _wait_for(lambda: manager.disabled_reason is not None)
    manager.apply_busconfig(None)
    assert _wait_for(lambda: _launches(run_dir) > etherdog_manager.MAX_RAPID_EXITS)
    assert _wait_for(lambda: manager.disabled_reason is not None)


def test_clean_exit_is_not_restarted(run_dir: Path) -> None:
    manager = _fake_binary(run_dir, "exit 0")
    manager.start()
    assert _wait_for(lambda: not manager._running)
    time.sleep(0.5)
    assert _launches(run_dir) == 1
    assert manager.disabled_reason is None
