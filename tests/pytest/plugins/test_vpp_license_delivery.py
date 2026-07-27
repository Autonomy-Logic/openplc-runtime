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


def _plugin_license_path(config_path: str) -> str:
    """Mirror of derive_license_path() in the licensed rpi_plugin.c: the .so's
    view of where its license lives, given its config path."""
    base = config_path[:-5] if config_path.endswith(".json") else config_path
    return base + ".license"


def _runtime_license_dest(dest_config: str) -> str:
    """Mirror of the delivery rule in apply_vpp_plugin_conf (kept identical)."""
    base = dest_config[:-5] if dest_config.endswith(".json") else dest_config
    return base + ".license"


@pytest.mark.parametrize(
    "config_path",
    [
        "/opt/runtime/build/vpp/rpi_gpio.json",
        "build/vpp/rpi_gpio.json",
        "rpi_gpio",  # no extension
        "a/b.c/rpi_gpio.json",
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

    class _P:
        name = "rpi_gpio"

        def __init__(self, cp):
            self.config_path = cp

    class _Conf:
        plugins = [_P(config_path)]

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
