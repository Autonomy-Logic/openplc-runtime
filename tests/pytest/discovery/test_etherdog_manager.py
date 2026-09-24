# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""EtherDOG supervision helpers: legacy upload detection, command transport, route shaping."""

import json
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

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
    """Unix-socket server that requires a token, then echoes the command it received."""

    def __init__(self, path: str, token: str) -> None:
        self.path = path
        self.token = token
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
                hello = json.loads(f.readline())
                if hello.get("params", {}).get("token") != self.token:
                    f.write('{"error": "authentication failed"}\n')
                    f.flush()
                    continue
                f.write('{"status": "success", "name": "EtherDOG"}\n')
                f.flush()
                request = json.loads(f.readline())
                f.write(json.dumps({"status": "success", "echo": request["command"]}) + "\n")
                f.flush()

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def run_dir():
    with tempfile.TemporaryDirectory(prefix="edog", dir="/tmp") as d:
        yield Path(d)


def _manager(run_dir: Path, token: str) -> EtherDogManager:
    binary = run_dir / "etherdog-bin"
    binary.write_text("")
    manager = EtherDogManager(binary=str(binary), run_dir=run_dir)
    manager._token = token
    return manager


def test_command_authenticates_and_returns_reply(run_dir: Path) -> None:
    server = FakeEtherDog(str(run_dir / "etherdog.socket"), "secret")
    try:
        reply = _manager(run_dir, "secret").command({"command": "status"})
        assert reply == {"status": "success", "echo": "status"}
    finally:
        server.close()


def test_command_rejected_with_wrong_token(run_dir: Path) -> None:
    server = FakeEtherDog(str(run_dir / "etherdog.socket"), "secret")
    try:
        manager = _manager(run_dir, "wrong")
        with pytest.raises(EtherDogUnavailable):
            manager.command({"command": "status"})
        assert "error" in manager.plugin_style_command({"command": "status"}, timeout=2.0)
    finally:
        server.close()


def test_session_file_is_private(run_dir: Path) -> None:
    manager = _manager(run_dir, "")
    manager._write_session()
    session = json.loads(manager.paths.session_file.read_text())
    assert session["token"] == manager._token and len(session["token"]) == 64
    assert session["control"].startswith("unix:")
    assert os.stat(manager.paths.session_file).st_mode & 0o077 == 0
    assert os.stat(manager.paths.token_file).st_mode & 0o077 == 0
