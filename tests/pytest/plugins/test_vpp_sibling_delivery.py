# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy

"""Tests for VPP sibling-file delivery via apply_vpp_plugin_conf.

The Weidmuller UC20 VPP, and any VPP that follows it, needs a ride-along
binary next to its JSON (for Weidmuller: conf/<name>.station.bin, the opaque
u-OS station archive). apply_vpp_plugin_conf must deliver that sibling to the
persistent dir so the .so can resolve it via dirname(config_path), the same
way the already-handled .license sibling works.

These tests exercise the generalised sibling copy: the delivery itself, the
coexistence with the existing .license path, the regression of the no-sibling
flow, the discovery guard against an orphan sibling, and the atomicity across
re-uploads.
"""
import os

import pytest


def _setup(tmp_path, monkeypatch):
    """Mock layout mirroring what safe_extract leaves in core/generated/ plus
    the persistent VPP_DATA_DIR the function is pointed at. Shared by every
    integration test here, same shape test_vpp_license_delivery uses."""
    mgmt = pytest.importorskip(
        "webserver.plcapp_management",
        reason="runtime webserver package not importable (no venv)",
    )

    persist = tmp_path / "data" / "vpp"
    persist.mkdir(parents=True)
    monkeypatch.setattr(mgmt, "VPP_DATA_DIR", persist)

    cwd = tmp_path / "runtime"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(mgmt.build_state, "log", lambda *_a, **_k: None, raising=False)

    gen = tmp_path / "generated"
    (gen / "conf").mkdir(parents=True)
    return mgmt, persist, cwd, gen


def _conf_line(name: str = "weidmuller") -> str:
    return (
        f"{name},./build/vpp/lib{name}_plugin.so,1,1,build/vpp/{name}.json,\n"
    )


def test_station_bin_sibling_lands_in_persistent_dir(tmp_path, monkeypatch):
    """A conf/<name>.station.bin ride-along must land at
    VPP_DATA_DIR/<name>.station.bin, with the same bytes and the mtime
    shutil.copy2 preserves, so the .so finds it via dirname(config_path)."""
    mgmt, persist, _cwd, gen = _setup(tmp_path, monkeypatch)

    (gen / "vpp_plugins.conf").write_text(_conf_line())
    (gen / "conf" / "weidmuller.json").write_text("{}\n")
    archive_bytes = b"\x1f\x8b\x08" + b"\xaa" * 1021  # 1024 bytes, not JSON
    archive_src = gen / "conf" / "weidmuller.station.bin"
    archive_src.write_bytes(archive_bytes)
    src_mtime = archive_src.stat().st_mtime

    mgmt.apply_vpp_plugin_conf(str(gen))

    delivered = persist / "weidmuller.station.bin"
    assert delivered.exists()
    assert delivered.read_bytes() == archive_bytes
    assert delivered.stat().st_mtime == pytest.approx(src_mtime, abs=1e-3)


def test_station_bin_and_license_coexist(tmp_path, monkeypatch):
    """The generic sibling copy must not break the .license carve-out: both
    files land in the persistent dir on the same upload, each at the path its
    consumer reads from."""
    mgmt, persist, _cwd, gen = _setup(tmp_path, monkeypatch)

    (gen / "vpp_plugins.conf").write_text(_conf_line())
    (gen / "conf" / "weidmuller.json").write_text("{}\n")
    (gen / "conf" / "weidmuller.station.bin").write_bytes(b"\xca\xfe" * 10)
    (gen / "conf" / "weidmuller.license").write_bytes(b"\x4f\x50\x4c\x43" + b"\x00" * 94)

    mgmt.apply_vpp_plugin_conf(str(gen))

    assert (persist / "weidmuller.json").exists()
    assert (persist / "weidmuller.station.bin").exists()
    assert (persist / "weidmuller.license").exists()
    assert (persist / "weidmuller.license").stat().st_size == 98


def test_no_sibling_leaves_persistent_dir_at_the_json(tmp_path, monkeypatch):
    """Regression: a VPP with just the JSON config keeps the current
    single-file behaviour. Nothing extra is created in the persistent dir."""
    mgmt, persist, _cwd, gen = _setup(tmp_path, monkeypatch)

    (gen / "vpp_plugins.conf").write_text(_conf_line("rpi_gpio"))
    (gen / "conf" / "rpi_gpio.json").write_text("{}\n")

    mgmt.apply_vpp_plugin_conf(str(gen))

    assert sorted(os.listdir(persist)) == ["rpi_gpio.json"]


def test_orphan_sibling_without_vpp_entry_is_ignored(tmp_path, monkeypatch):
    """Discovery stays vpp_plugins.conf-anchored: a conf/other.station.bin with
    no matching VPP entry for 'other' must not be copied anywhere, even when
    another plugin's JSON is present on the same upload."""
    mgmt, persist, _cwd, gen = _setup(tmp_path, monkeypatch)

    (gen / "vpp_plugins.conf").write_text(_conf_line("weidmuller"))
    (gen / "conf" / "weidmuller.json").write_text("{}\n")
    (gen / "conf" / "other.station.bin").write_bytes(b"\xff" * 32)

    mgmt.apply_vpp_plugin_conf(str(gen))

    assert (persist / "weidmuller.json").exists()
    assert not (persist / "other.station.bin").exists()
    assert sorted(os.listdir(persist)) == ["weidmuller.json"]


def test_two_vpp_plugins_each_get_only_their_own_siblings(tmp_path, monkeypatch):
    """A sibling must bind to the plugin whose basename it starts with. Two VPPs
    on the same upload: each one's .station.bin reaches the persistent dir, and
    neither crosses over."""
    mgmt, persist, _cwd, gen = _setup(tmp_path, monkeypatch)

    (gen / "vpp_plugins.conf").write_text(_conf_line("weidmuller") + _conf_line("acme"))
    (gen / "conf" / "weidmuller.json").write_text("{}\n")
    (gen / "conf" / "weidmuller.station.bin").write_bytes(b"W" * 16)
    (gen / "conf" / "acme.json").write_text("{}\n")
    (gen / "conf" / "acme.station.bin").write_bytes(b"A" * 16)

    mgmt.apply_vpp_plugin_conf(str(gen))

    assert (persist / "weidmuller.station.bin").read_bytes() == b"W" * 16
    assert (persist / "acme.station.bin").read_bytes() == b"A" * 16


def test_subdirectory_under_conf_is_not_copied(tmp_path, monkeypatch):
    """Flat sibling only. A conf/<name>/ as a directory is out of scope: the
    discovery is one level deep. Any directory starting with the plugin name
    is skipped by os.path.isfile."""
    mgmt, persist, _cwd, gen = _setup(tmp_path, monkeypatch)

    (gen / "vpp_plugins.conf").write_text(_conf_line("weidmuller"))
    (gen / "conf" / "weidmuller.json").write_text("{}\n")
    (gen / "conf" / "weidmuller.d").mkdir()
    (gen / "conf" / "weidmuller.d" / "inner.bin").write_bytes(b"X" * 8)

    mgmt.apply_vpp_plugin_conf(str(gen))

    assert (persist / "weidmuller.json").exists()
    assert not (persist / "weidmuller.d").exists()


def test_second_upload_updates_sibling_content(tmp_path, monkeypatch):
    """A re-upload with a different sibling payload must replace the previous
    one in the persistent dir, so the plugin's SHA check on the next init
    reads the fresh archive instead of a stale copy."""
    mgmt, persist, _cwd, gen = _setup(tmp_path, monkeypatch)

    (gen / "vpp_plugins.conf").write_text(_conf_line("weidmuller"))
    (gen / "conf" / "weidmuller.json").write_text("{}\n")
    (gen / "conf" / "weidmuller.station.bin").write_bytes(b"old" * 10)

    mgmt.apply_vpp_plugin_conf(str(gen))
    assert (persist / "weidmuller.station.bin").read_bytes() == b"old" * 10

    (gen / "conf" / "weidmuller.station.bin").write_bytes(b"new-and-longer" * 4)
    mgmt.apply_vpp_plugin_conf(str(gen))

    assert (persist / "weidmuller.station.bin").read_bytes() == b"new-and-longer" * 4
