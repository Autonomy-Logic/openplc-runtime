"""Tests for the webserver-level VPP license debug FCs (0x48/0x49/0x4A).

Covers the raw-PDU responses the editor's modbus-pdu.ts parsers expect, the raw
(non-hex-decoded) anchor semantics that must match the .so (D70d), and the 0x49
write / 0x4A read round-trip landing on the .license sibling of the plugin
config (same path the bundle + the .so use).
"""
import os

import pytest

lic = pytest.importorskip(
    "webserver.vpp_license_debug",
    reason="runtime webserver package not importable (no venv)",
)


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def _install_plugin(tmp_path, monkeypatch):
    """Fake one installed VPP plugin whose config_path lives under a temp cwd."""
    cwd = tmp_path / "runtime"
    (cwd / "build" / "vpp").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    (cwd / "vpp_plugins.conf").write_text("dummy\n")
    config_path = str(cwd / "build" / "vpp" / "rpi_gpio.json")

    class _P:
        name = "rpi_gpio"
        config_path = None

        def __init__(self, cp):
            self.config_path = cp

    class _Conf:
        plugins = [_P(config_path)]

    monkeypatch.setattr(lic.PluginsConfiguration, "from_file", classmethod(lambda cls, _p: _Conf()))
    return config_path


def test_is_license_command():
    assert lic.is_license_command("48")
    assert lic.is_license_command("49 00 62")
    assert lic.is_license_command("4A")
    assert not lic.is_license_command("41 00 00")
    assert not lic.is_license_command("")


def test_get_board_id_returns_raw_ascii_anchor(tmp_path, monkeypatch):
    # Mimic /proc/device-tree/serial-number: ASCII hex + trailing NUL.
    anchor_file = tmp_path / "serial-number"
    anchor_file.write_bytes(b"8625807b0a83ae7d\x00")
    monkeypatch.setattr(lic, "ANCHOR_PATH", str(anchor_file))

    resp = lic.handle_license_command("48")
    parts = resp.split()
    assert parts[0] == "48"
    assert parts[1] == "7E"  # SUCCESS
    assert parts[2] == "10"  # 16 bytes
    body = bytes(int(p, 16) for p in parts[3:])
    # RAW ascii, NUL stripped -- NOT hex-decoded (the .so hashes exactly these bytes).
    assert body == b"8625807b0a83ae7d"


def test_get_board_id_missing_anchor_is_empty_success(tmp_path, monkeypatch):
    # No anchor -> SUCCESS with id_len=0 (matches Arduino D57), not an error byte.
    monkeypatch.setattr(lic, "ANCHOR_PATH", str(tmp_path / "nope"))
    assert lic.handle_license_command("48") == "48 7E 00"


def test_write_refuses_path_traversal(tmp_path, monkeypatch):
    # A forged vpp_plugins.conf whose config_path escapes the runtime root must
    # NOT let 0x49 write outside it (defense-in-depth; mirrors apply_vpp_plugin_conf).
    cwd = tmp_path / "runtime"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    (cwd / "vpp_plugins.conf").write_text("dummy\n")
    escaping = str(tmp_path / "outside" / "evil.json")  # sibling of cwd -> escapes root

    class _P:
        name = "x"
        config_path = escaping

    class _Conf:
        plugins = [_P()]

    monkeypatch.setattr(lic.PluginsConfiguration, "from_file", classmethod(lambda cls, _p: _Conf()))

    cmd = _hex(bytes([0x49, 0x00, 0x62]) + bytes(98))
    assert lic.handle_license_command(cmd) == "49 85"  # refused -> LIC_UNSUPPORTED
    assert not os.path.exists(tmp_path / "outside" / "evil.license")


def test_read_license_empty_when_no_conf(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no vpp_plugins.conf here
    assert lic.handle_license_command("4A") == "4A 83"  # LIC_EMPTY


def test_write_then_read_roundtrip(tmp_path, monkeypatch):
    config_path = _install_plugin(tmp_path, monkeypatch)
    blob = bytes([0x4F, 0x50, 0x4C, 0x43]) + bytes(range(1, 95))  # 98 bytes
    assert len(blob) == 98

    cmd = _hex(bytes([0x49, 0x00, 0x62]) + blob)  # [0x49][len=98 u16BE][blob]
    assert lic.handle_license_command(cmd) == "49 7E"  # SUCCESS

    expected_path = config_path[:-5] + ".license"
    assert os.path.exists(expected_path)
    assert os.path.getsize(expected_path) == 98

    read = lic.handle_license_command("4A")
    parts = read.split()
    assert parts[0] == "4A" and parts[1] == "7E"
    assert parts[2] == "00" and parts[3] == "62"  # len 98, u16BE
    assert bytes(int(p, 16) for p in parts[4:]) == blob


def test_write_wrong_size_is_corrupt(tmp_path, monkeypatch):
    _install_plugin(tmp_path, monkeypatch)
    cmd = _hex(bytes([0x49, 0x00, 0x04]) + b"\x01\x02\x03\x04")  # not 98
    assert lic.handle_license_command(cmd) == "49 84"  # LIC_CORRUPT


def test_write_without_installed_plugin_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no vpp_plugins.conf
    blob = bytes(98)
    cmd = _hex(bytes([0x49, 0x00, 0x62]) + blob)
    assert lic.handle_license_command(cmd) == "49 85"  # LIC_UNSUPPORTED


def test_non_license_fc_passes_through():
    assert lic.handle_license_command("41 00 00 00 01") is None
