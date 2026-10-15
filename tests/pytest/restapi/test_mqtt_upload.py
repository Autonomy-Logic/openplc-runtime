# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""conf/mqtt.json through upload, build and activation.

A rejected configuration must leave the device exactly as it was; an accepted
one becomes active only once the build of its own program succeeds.
"""

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = RUNTIME_ROOT / "core/src/drivers/plugins/python/mqtt/contract/fixtures/runtime-config"
GENERIC = CONFIG_DIR / "valid" / "generic-on-change-anonymous-plaintext.json"
SPARKPLUG = CONFIG_DIR / "valid" / "sparkplug-client-certificate-all-types.json"
DEBUG_MAP = CONFIG_DIR / "debug-map.json"
ACTIVE = Path("core/src/drivers/plugins/python/mqtt/mqtt.json")
OLD_PLUGINS_CONF = (
    "opcua,./core/src/drivers/plugins/python/opcua/plugin.py,0,0,"
    "./core/src/drivers/plugins/python/opcua/opcua.json,./venvs/opcua\n"
)


class _FakeRuntime:
    def __init__(self, state: str = "STOPPED") -> None:
        self.state = state
        self.calls: list[str] = []

    def status_plc(self) -> str:
        return f"STATUS:{self.state}\n"

    def stop_plc(self) -> str:
        self.calls.append("stop")
        self.state = "STOPPED"
        return "OK\n"

    def reset_crash_tracking(self) -> None:
        self.calls.append("reset")


class _FakeProcess:
    def __init__(self, exit_code: int) -> None:
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self._exit_code = exit_code

    def wait(self) -> int:
        return self._exit_code


def _program_zip(files: dict[str, bytes], root: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{root}program.st", "PROGRAM p END_PROGRAM")
        for name, content in files.items():
            zf.writestr(f"{root}{name}", content)
    return buf.getvalue()


def _with_mqtt(config: bytes, debug_map: bytes | None = None, root: str = "") -> bytes:
    files = {"conf/mqtt.json": config}
    files["debug-map.json"] = DEBUG_MAP.read_bytes() if debug_map is None else debug_map
    return _program_zip(files, root)


def _plugins() -> dict[str, list[str]]:
    rows = {}
    for line in Path("plugins.conf").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            fields = line.split(",")
            rows[fields[0]] = fields
    return rows


@pytest.fixture
def device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeRuntime:
    """A runtime working directory whose plugins.conf predates the MQTT plugin."""
    from webserver import app as app_module

    monkeypatch.chdir(tmp_path)
    shutil.copy(RUNTIME_ROOT / "plugins_default.conf", tmp_path / "plugins_default.conf")
    Path("plugins.conf").write_text(OLD_PLUGINS_CONF, encoding="utf-8")
    runtime = _FakeRuntime()
    monkeypatch.setattr(app_module, "runtime_manager", runtime)
    monkeypatch.setattr(app_module, "apply_vpp_plugin_conf", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "apply_retain_conf", lambda *a, **k: None)
    monkeypatch.setattr(app_module.etherdog_manager, "apply_busconfig", lambda _path: None)
    monkeypatch.setattr(app_module, "run_compile", lambda *a, **k: None)
    monkeypatch.setattr(app_module.build_state, "status", app_module.BuildStatus.SUCCESS)
    return runtime


@pytest.fixture
def active_previous_program() -> bytes:
    """State left by an earlier program whose MQTT configuration is active."""
    ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    previous = GENERIC.read_bytes()
    ACTIVE.write_bytes(previous)
    Path("plugins.conf").write_text(
        OLD_PLUGINS_CONF
        + "mqtt,./core/src/drivers/plugins/python/mqtt/mqtt_plugin.py,1,0,"
        + f"./{ACTIVE},./venvs/mqtt\n",
        encoding="utf-8",
    )
    generated = Path("core/generated")
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "previous.marker").write_text("previous program", encoding="utf-8")
    return previous


def _upload(blob: bytes) -> dict[str, object]:
    from webserver import app as app_module

    data = {"file": (io.BytesIO(blob), "program.zip")}
    with app_module.app.test_request_context(
        "/api/upload-file", method="POST", data=data, content_type="multipart/form-data"
    ):
        result: dict[str, object] = app_module.handle_upload_file({})
        return result


def _build(monkeypatch: pytest.MonkeyPatch, runtime: _FakeRuntime, exit_code: int) -> str:
    from webserver import plcapp_management

    monkeypatch.setattr(
        plcapp_management.subprocess, "Popen", lambda *a, **k: _FakeProcess(exit_code)
    )
    plcapp_management.build_state.clear()
    plcapp_management.run_compile(runtime, cwd="core/generated")
    return plcapp_management.build_state.status.name


def _mutated(rel: str, **changes: object) -> bytes:
    doc = json.loads((CONFIG_DIR / rel).read_text(encoding="utf-8"))
    doc.update(changes)
    return json.dumps(doc).encode("utf-8")


# --- rejection before any change ---------------------------------------------


def _assert_untouched(runtime: _FakeRuntime, previous: bytes) -> None:
    assert runtime.calls == []
    assert Path("core/generated/previous.marker").exists()
    assert ACTIVE.read_bytes() == previous
    assert _plugins()["mqtt"][2] == "1"


@pytest.mark.parametrize(
    "blob, code, pointer",
    [
        pytest.param(
            _with_mqtt(_mutated("valid/" + GENERIC.name, format_version=2)),
            "config_unknown_version",
            "/format_version",
            id="unknown-version",
        ),
        pytest.param(
            _with_mqtt((CONFIG_DIR / "invalid/duplicate-key.json").read_bytes()),
            "config_duplicate_key",
            "/enabled",
            id="duplicate-enabled",
        ),
        pytest.param(
            _with_mqtt((CONFIG_DIR / "invalid/address-not-in-program.json").read_bytes()),
            "config_address_invalid",
            "/leaves/0/address",
            id="false-address",
        ),
        pytest.param(
            _with_mqtt((CONFIG_DIR / "invalid/program-mismatch.json").read_bytes()),
            "config_program_mismatch",
            "/program/md5",
            id="different-program",
        ),
        pytest.param(
            _program_zip({"conf/mqtt.json": GENERIC.read_bytes()}),
            "config_program_mismatch",
            "/program/md5",
            id="no-debug-map",
        ),
        pytest.param(
            _with_mqtt(GENERIC.read_bytes(), b"{not json"),
            "config_program_mismatch",
            "/program/md5",
            id="corrupt-debug-map",
        ),
        pytest.param(
            _with_mqtt(SPARKPLUG.read_bytes()),
            "config_credential_missing",
            "/auth/credential_ref",
            id="credential-not-on-runtime",
        ),
    ],
)
def test_invalid_configuration_is_rejected_before_the_device_changes(
    device: _FakeRuntime, active_previous_program: bytes, blob: bytes, code: str, pointer: str
) -> None:
    device.state = "RUNNING"
    result = _upload(blob)

    assert result["CompilationStatus"] == "FAILED"
    error = result["MqttConfigError"]
    assert isinstance(error, dict)
    assert (error["code"], error["pointer"], error["category"]) == (code, pointer, "configuration")
    assert str(result["UploadFileFail"]).startswith(f"MQTT configuration rejected: {code}")
    device.state = "STOPPED"
    _assert_untouched(device, active_previous_program)


def test_rejection_does_not_echo_the_configuration(device: _FakeRuntime) -> None:
    marker = "MARKER-BROKER-55d1"
    text = GENERIC.read_text(encoding="utf-8").replace("broker.example.net", f"{marker}!")
    result = _upload(_with_mqtt(text.encode("utf-8")))
    assert result["CompilationStatus"] == "FAILED"
    assert marker not in json.dumps(result)


# --- acceptance and activation -------------------------------------------------


def test_valid_upload_waits_for_the_build_then_activates(
    device: _FakeRuntime, active_previous_program: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = (CONFIG_DIR / "valid" / "generic-on-change-anonymous-plaintext.json").read_bytes()
    result = _upload(_with_mqtt(config, root="bundle/"))

    assert result["UploadFileFail"] == ""
    assert "MqttConfigError" not in result
    assert Path("core/generated/conf/mqtt.json").read_bytes() == config
    assert _plugins()["mqtt"][2] == "0"
    assert not ACTIVE.exists()

    assert _build(monkeypatch, device, exit_code=0) == "SUCCESS"
    mqtt = _plugins()["mqtt"]
    assert mqtt[2] == "1"
    assert Path(mqtt[4]).resolve() == ACTIVE.resolve()
    assert ACTIVE.read_bytes() == config


def test_failed_build_leaves_the_integration_inactive(
    device: _FakeRuntime, active_previous_program: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _upload(_with_mqtt(GENERIC.read_bytes()))["UploadFileFail"] == ""
    assert _build(monkeypatch, device, exit_code=1) == "FAILED"
    assert _plugins()["mqtt"][2] == "0"
    assert not ACTIVE.exists()


def test_upload_without_mqtt_removes_the_previous_configuration(
    device: _FakeRuntime, active_previous_program: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _upload(_program_zip({"debug-map.json": DEBUG_MAP.read_bytes()}))["UploadFileFail"] == ""
    assert _plugins()["mqtt"][2] == "0"
    assert not ACTIVE.exists()
    assert _build(monkeypatch, device, exit_code=0) == "SUCCESS"
    assert _plugins()["mqtt"][2] == "0"
    assert not ACTIVE.exists()


def test_configuration_changed_after_upload_is_not_activated(
    device: _FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _upload(_with_mqtt(GENERIC.read_bytes()))["UploadFileFail"] == ""
    tampered = (CONFIG_DIR / "invalid" / "duplicate-address.json").read_bytes()
    Path("core/generated/conf/mqtt.json").write_bytes(tampered)

    assert _build(monkeypatch, device, exit_code=0) == "FAILED"
    assert _plugins()["mqtt"][2] == "0"
    assert not ACTIVE.exists()
    from webserver import plcapp_management

    assert any("config_leaf_duplicate" in line for line in plcapp_management.build_state.logs)


def test_other_plugins_keep_their_activation(
    device: _FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = _program_zip(
        {
            "conf/opcua.json": b"{}",
            "conf/mqtt.json": GENERIC.read_bytes(),
            "debug-map.json": DEBUG_MAP.read_bytes(),
        }
    )
    assert _upload(blob)["UploadFileFail"] == ""
    rows = _plugins()
    assert (rows["opcua"][2], rows["mqtt"][2]) == ("1", "0")
    assert _build(monkeypatch, device, exit_code=0) == "SUCCESS"
    rows = _plugins()
    assert (rows["opcua"][2], rows["mqtt"][2]) == ("1", "1")


def test_upload_without_conf_directory_disables_mqtt(
    device: _FakeRuntime, active_previous_program: bytes
) -> None:
    from webserver.plcapp_management import update_plugin_configurations

    Path("core/generated").mkdir(parents=True, exist_ok=True)
    update_plugin_configurations("core/generated")
    assert _plugins()["mqtt"][2] == "0"
    assert not ACTIVE.exists()


def test_registration_loads_mqtt_before_the_existing_python_plugins(device: _FakeRuntime) -> None:
    from webserver.plcapp_management import update_plugin_configurations

    defaults = (RUNTIME_ROOT / "plugins_default.conf").read_text(encoding="utf-8").splitlines()
    old = [line for line in defaults if not line.startswith("mqtt,")]
    Path("plugins.conf").write_text("\n".join(old) + "\n", encoding="utf-8")
    Path("core/generated/conf").mkdir(parents=True, exist_ok=True)

    update_plugin_configurations("core/generated", program_built=False)

    names = [line.split(",")[0] for line in Path("plugins.conf").read_text().splitlines()]
    expected = [line.split(",")[0] for line in defaults]
    assert names == expected
    assert names[0] == "mqtt"


def test_configuration_errors_are_reported_before_a_missing_debug_map(
    device: _FakeRuntime,
) -> None:
    blob = _program_zip({"conf/mqtt.json": _mutated("valid/" + GENERIC.name, format_version=9)})
    error = _upload(blob)["MqttConfigError"]
    assert isinstance(error, dict) and error["code"] == "config_unknown_version"
