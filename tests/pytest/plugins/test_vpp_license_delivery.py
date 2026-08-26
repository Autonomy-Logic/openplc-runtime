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


def test_apply_vpp_plugin_conf_delivers_license(tmp_path, monkeypatch):
    """Integration: a conf/<plugin>.license in the upload is copied to the sibling
    of the plugin's config_path; absence leaves no license (device -> demo)."""
    mgmt = pytest.importorskip(
        "webserver.plcapp_management",
        reason="runtime webserver package not importable (no venv)",
    )

    # Fake a single native plugin whose config_path lives under the temp cwd.
    cwd = tmp_path / "runtime"
    (cwd).mkdir()
    monkeypatch.chdir(cwd)
    config_path = str(cwd / "build" / "vpp" / "rpi_gpio.json")

    # `path` is not decoration: apply_vpp_plugin_conf now runs the uploaded conf
    # through validate_vpp_plugins_conf first, which requires every VPP plugin's
    # .so to resolve inside build/vpp/. A fake without it would only prove the
    # fake is out of date.
    class _P:
        name = "rpi_gpio"

        def __init__(self, cp, so):
            self.config_path = cp
            self.path = so

    class _Conf:
        plugins = [_P(config_path, "./build/vpp/librpi_gpio_plugin.so")]

    monkeypatch.setattr(mgmt.PluginsConfiguration, "from_file", classmethod(lambda cls, _p: _Conf()))
    monkeypatch.setattr(mgmt.build_state, "log", lambda *_a, **_k: None, raising=False)

    # Build the uploaded generated_dir: vpp_plugins.conf + conf/{json,license}.
    gen = tmp_path / "generated"
    (gen / "conf").mkdir(parents=True)
    (gen / "vpp_plugins.conf").write_text("dummy\n")
    (gen / "conf" / "rpi_gpio.json").write_text("{}\n")
    (gen / "conf" / "rpi_gpio.license").write_bytes(b"\x4f\x50\x4c\x43" + b"\x00" * 94)  # 98-byte blob

    mgmt.apply_vpp_plugin_conf(str(gen))

    expected = config_path[:-5] + ".license"
    assert os.path.exists(expected), "license blob not delivered to the plugin's sibling path"
    assert os.path.getsize(expected) == 98

    # Second pass without a .license in the upload must not resurrect a stale one
    # from the same source (delivery only copies what the upload carries).
    os.remove(expected)
    (gen / "conf" / "rpi_gpio.license").unlink()
    mgmt.apply_vpp_plugin_conf(str(gen))
    assert not os.path.exists(expected)
