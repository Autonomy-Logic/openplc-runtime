"""Tests for VPP device-license delivery via apply_vpp_plugin_conf.

A licensed VPP's activated blob rides in the upload as conf/<plugin>.license and
must land next to the plugin config at the sibling path the .so derives from its
config_path (drop a trailing ".json", append ".license"). If the runtime and the
plugin disagree on that path, the .so never finds the license and falls to demo.

The parity test is pure (no runtime deps). The integration test imports the real
apply_vpp_plugin_conf and is skipped where the webserver package can't import
(e.g. a dev box without the runtime venv); CI with the venv runs it.
"""
import os
import shutil

import pytest

_lic = pytest.importorskip(
    "webserver.vpp_license_debug",
    reason="runtime webserver package not importable (no venv)",
)


def _plugin_license_path(config_path: str) -> str:
    """Mirror of derive_license_path() in the licensed rpi_plugin.c: the .so's
    view of where its license lives, given its config path.

    This is the ONLY hand-written copy in the test -- it stands in for the C,
    which we cannot call from here, so it must track rpi_plugin.c line by line:
    empty in, empty out; and the extension is stripped only when the path is
    strictly LONGER than ".json" (`len > elen` in the C), so a path of exactly
    ".json" keeps it.
    """
    if not config_path:
        return ""
    ext = ".json"
    base = config_path[: -len(ext)] if len(config_path) > len(ext) and config_path.endswith(ext) else config_path
    return base + ".license"


def _runtime_license_dest(dest_config: str) -> str:
    """The runtime's real derivation -- NOT a copy.

    A second hand-written mirror here would make this a comparison of two
    transcriptions: it would stay green even if the shipped function drifted,
    which is precisely the drift the test exists to catch.
    """
    return _lic.derive_license_path(dest_config)


@pytest.mark.parametrize(
    "config_path",
    [
        "/opt/runtime/build/vpp/rpi_gpio.json",
        "build/vpp/rpi_gpio.json",
        "rpi_gpio",  # no extension
        "a/b.c/rpi_gpio.json",
        ".json",  # exactly the extension: the C's `len > elen` keeps it
        "",  # empty in, empty out
        "rpi_gpio.JSON",  # case-sensitive on both sides: kept, not stripped
    ],
)
def test_delivery_path_matches_plugin_derivation(config_path):
    """The runtime must deliver the .license to exactly the path the plugin reads."""
    assert _runtime_license_dest(config_path) == _plugin_license_path(config_path)


def test_apply_vpp_plugin_conf_relocates_to_persistent_dir(tmp_path, monkeypatch):
    """Integration: apply relocates config+license into PERSISTENT_DATA_DIR/vpp
    (NOT build/vpp) and rewrites config_path in vpp_plugins.conf to that absolute
    path, so a runtime update -- which wipes build/ -- cannot delete the license.
    The .so path stays under build/vpp (it is code, rebuilt each upload)."""
    mgmt = pytest.importorskip(
        "webserver.plcapp_management",
        reason="runtime webserver package not importable (no venv)",
    )

    # Persistent dir lives OUTSIDE the runtime cwd on purpose -- that is the whole
    # point of the change. Point the module's VPP_DATA_DIR at a temp location so
    # the test never touches /var/lib.
    persist = tmp_path / "data" / "vpp"
    persist.mkdir(parents=True)
    monkeypatch.setattr(mgmt, "VPP_DATA_DIR", persist)

    cwd = tmp_path / "runtime"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(mgmt.build_state, "log", lambda *_a, **_k: None, raising=False)

    # A REAL uploaded conf (no from_file monkeypatch): the .so path is relative
    # and inside build/vpp so validate_vpp_plugins_conf accepts it; config_path is
    # what the editor emits (relative build/vpp) and what apply must rewrite.
    gen = tmp_path / "generated"
    (gen / "conf").mkdir(parents=True)
    (gen / "vpp_plugins.conf").write_text(
        "rpi_gpio,./build/vpp/librpi_gpio_plugin.so,1,1,build/vpp/rpi_gpio.json,\n"
    )
    (gen / "conf" / "rpi_gpio.json").write_text("{}\n")
    (gen / "conf" / "rpi_gpio.license").write_bytes(b"\x4f\x50\x4c\x43" + b"\x00" * 94)  # 98-byte blob

    mgmt.apply_vpp_plugin_conf(str(gen))

    # Config and license landed in the persistent dir, not build/vpp.
    assert os.path.exists(persist / "rpi_gpio.json")
    assert os.path.exists(persist / "rpi_gpio.license")
    assert os.path.getsize(persist / "rpi_gpio.license") == 98
    assert not os.path.exists(cwd / "build" / "vpp" / "rpi_gpio.license")

    # vpp_plugins.conf was rewritten: config_path -> persistent absolute; the .so
    # path is untouched (stays under build/vpp).
    rewritten = mgmt.PluginsConfiguration.from_file(str(cwd / "vpp_plugins.conf"))
    plugin = rewritten.plugins[0]
    assert plugin.config_path == str(persist / "rpi_gpio.json")
    assert plugin.path == "./build/vpp/librpi_gpio_plugin.so"


def test_persistent_license_survives_an_upload_without_a_license(tmp_path, monkeypatch):
    """A re-upload that does not carry a .license must NOT wipe the license the
    device already holds in the persistent dir -- that survival is the point."""
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
    conf_line = "rpi_gpio,./build/vpp/librpi_gpio_plugin.so,1,1,build/vpp/rpi_gpio.json,\n"
    (gen / "vpp_plugins.conf").write_text(conf_line)
    (gen / "conf" / "rpi_gpio.json").write_text("{}\n")
    (gen / "conf" / "rpi_gpio.license").write_bytes(b"\x4f\x50\x4c\x43" + b"\x00" * 94)

    mgmt.apply_vpp_plugin_conf(str(gen))
    assert os.path.exists(persist / "rpi_gpio.license")

    # Second upload of the same VPP, this time WITHOUT the license blob.
    (gen / "conf" / "rpi_gpio.license").unlink()
    mgmt.apply_vpp_plugin_conf(str(gen))

    assert os.path.exists(persist / "rpi_gpio.license"), "persistent license must survive a license-less upload"
    assert os.path.getsize(persist / "rpi_gpio.license") == 98


def test_migration_rescues_a_pre_change_license_from_build_vpp(tmp_path, monkeypatch):
    """A device licensed before this change has its blob at build/vpp/<name>.license.
    When the update did not wipe build/ (the wipe is conditional on CMakeCache.txt),
    the next upload without a bundled license migrates that blob into the persistent
    dir instead of leaving it orphaned."""
    mgmt = pytest.importorskip(
        "webserver.plcapp_management",
        reason="runtime webserver package not importable (no venv)",
    )
    persist = tmp_path / "data" / "vpp"
    persist.mkdir(parents=True)
    monkeypatch.setattr(mgmt, "VPP_DATA_DIR", persist)
    cwd = tmp_path / "runtime"
    (cwd / "build" / "vpp").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(mgmt.build_state, "log", lambda *_a, **_k: None, raising=False)

    old = cwd / "build" / "vpp" / "rpi_gpio.license"  # pre-change location
    old.write_bytes(b"\x4f\x50\x4c\x43" + b"\x00" * 94)

    gen = tmp_path / "generated"
    (gen / "conf").mkdir(parents=True)
    (gen / "vpp_plugins.conf").write_text(
        "rpi_gpio,./build/vpp/librpi_gpio_plugin.so,1,1,build/vpp/rpi_gpio.json,\n"
    )
    (gen / "conf" / "rpi_gpio.json").write_text("{}\n")  # no .license in the upload

    mgmt.apply_vpp_plugin_conf(str(gen))
    assert (persist / "rpi_gpio.license").read_bytes() == old.read_bytes()


def test_migration_ignores_a_license_a_forged_config_path_points_at(tmp_path, monkeypatch):
    """Security: the migration source is the FIXED build/vpp/<name>.license, never
    config_path. validate_vpp_plugins_conf only confines config_path to the runtime
    root, so a forged conf could name a .license elsewhere under the root; that file
    must NOT be copied to where 0x4A would read it back."""
    mgmt = pytest.importorskip(
        "webserver.plcapp_management",
        reason="runtime webserver package not importable (no venv)",
    )
    persist = tmp_path / "data" / "vpp"
    persist.mkdir(parents=True)
    monkeypatch.setattr(mgmt, "VPP_DATA_DIR", persist)
    cwd = tmp_path / "runtime"
    (cwd / "build" / "vpp").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(mgmt.build_state, "log", lambda *_a, **_k: None, raising=False)

    # A decoy .license elsewhere in the runtime root (passes the validator's
    # runtime-root confinement) that a forged config_path tries to point at.
    (cwd / "secrets").mkdir()
    (cwd / "secrets" / "target.license").write_bytes(b"\x4f\x50\x4c\x43" + b"\x00" * 94)

    gen = tmp_path / "generated"
    (gen / "conf").mkdir(parents=True)
    (gen / "vpp_plugins.conf").write_text(
        "rpi_gpio,./build/vpp/librpi_gpio_plugin.so,1,1,secrets/target.json,\n"
    )
    (gen / "conf" / "rpi_gpio.json").write_text("{}\n")  # no .license in the upload

    mgmt.apply_vpp_plugin_conf(str(gen))
    # migration looked at build/vpp/rpi_gpio.license (absent), NOT secrets/target.license
    assert not (persist / "rpi_gpio.license").exists()
