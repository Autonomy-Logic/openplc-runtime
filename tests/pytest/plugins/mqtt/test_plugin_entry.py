# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Plugin-side activation: one generation at a time, never a partially valid one."""

import json
import re
import subprocess
import sys
from pathlib import Path

import mqtt_plugin
import pytest
from contract.runtime_config import ConfigError
from mqtt_reader.reader import LeafReader
from program_fakes import FakeProgram
from publisher_fakes import FakeNetwork, RealTime, RecordingStrategy
from shared.safe_logging_access import SafeLoggingAccess

CONFIG_DIR = (
    Path(__file__).resolve().parents[4]
    / "core/src/drivers/plugins/python/mqtt/contract/fixtures/runtime-config"
)
VALID = CONFIG_DIR / "valid" / "sparkplug-client-certificate-all-types.json"


@pytest.fixture
def anonymous_sparkplug(tmp_path: Path) -> Path:
    """The all-types Sparkplug fixture without a secret, so no vault is involved."""
    doc = json.loads(VALID.read_text(encoding="utf-8"))
    doc["auth"] = {"method": "anonymous"}
    path = tmp_path / "mqtt.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


_BLOCKED_PROBE = (
    "import sys, importlib.abc\n"
    "class Block(importlib.abc.MetaPathFinder):\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name.split('.')[0] in {'paho', 'google', 'cryptography'}:\n"
    "            raise ImportError(name)\n"
    "        return None\n"
    "sys.meta_path.insert(0, Block())\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import mqtt_plugin\n"
    "print(sorted({'paho', 'google', 'cryptography'} & set(sys.modules)),"
    " mqtt_plugin.TRANSPORT_FACTORY is None)\n"
)


def test_loading_the_entry_point_does_not_need_the_plugin_venv() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _BLOCKED_PROBE, str(Path(mqtt_plugin.__file__).parent)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[] True"
    assert {"init", "start_loop", "stop_loop", "cleanup"} <= set(dir(mqtt_plugin))


def test_the_client_library_is_bound_while_the_plugin_venv_leads_the_path() -> None:
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); import mqtt_plugin;"
        "import paho.mqtt as p; print('paho' in sys.modules, p.__version__,"
        " mqtt_plugin.TRANSPORT_FACTORY is not None)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(Path(mqtt_plugin.__file__).parent)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "True 2.1.0 True"


def test_valid_configuration_becomes_a_new_generation(anonymous_sparkplug: Path) -> None:
    state = mqtt_plugin.PluginState()
    path = anonymous_sparkplug
    first = state.activate(str(path))
    second = state.activate(str(path))
    assert isinstance(first, mqtt_plugin.ActiveGeneration)
    assert isinstance(second, mqtt_plugin.ActiveGeneration)
    assert (first.number, second.number) == (1, 2)
    assert state.generation is second and state.last_error is None
    assert second.config.mode == "sparkplug_b"


def test_invalid_configuration_drops_the_previous_generation(anonymous_sparkplug: Path) -> None:
    state = mqtt_plugin.PluginState()
    path = anonymous_sparkplug
    assert isinstance(state.activate(str(path)), mqtt_plugin.ActiveGeneration)
    path.write_bytes((CONFIG_DIR / "invalid" / "unknown-version.json").read_bytes())
    result = state.activate(str(path))
    assert isinstance(result, ConfigError) and result.code == "config_unknown_version"
    assert state.generation is None and state.last_error == result


def test_missing_configuration_is_reported_without_activation(tmp_path: Path) -> None:
    state = mqtt_plugin.PluginState()
    result = state.activate(str(tmp_path / "absent.json"))
    assert isinstance(result, ConfigError) and result.pointer == ""
    assert state.generation is None


def test_oversized_installed_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mqtt.json"
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    result = mqtt_plugin.load_active_config(str(path))
    assert isinstance(result, ConfigError) and result.code == "config_too_large"


def test_stop_and_cleanup_drop_the_generation(
    anonymous_sparkplug: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = anonymous_sparkplug
    network = FakeNetwork(RealTime())
    factory = mqtt_plugin.PublisherFactory(
        {"sparkplug_b": lambda _config, _writer: RecordingStrategy()},  # type: ignore[arg-type,return-value]
        network.factory,  # type: ignore[arg-type]
    )
    state = mqtt_plugin.PluginState(publishers=factory)
    state.config_path = str(path)
    monkeypatch.setattr(mqtt_plugin, "_state", state)
    monkeypatch.setattr(mqtt_plugin, "_program", FakeProgram.from_debug_map())
    assert mqtt_plugin.start_loop() is True
    assert state.generation is not None
    assert isinstance(state.generation.reader, LeafReader)
    assert mqtt_plugin.stop_loop() is True
    assert state.generation is None
    assert mqtt_plugin.start_loop() is True
    assert mqtt_plugin.cleanup() is True
    assert state.generation is None


def test_start_without_a_configuration_path_stays_inactive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = mqtt_plugin.PluginState()
    monkeypatch.setattr(mqtt_plugin, "_state", state)
    monkeypatch.setattr(mqtt_plugin, "_logging", None)
    assert mqtt_plugin.start_loop() is False
    assert state.generation is None
    assert "no configuration installed" in capsys.readouterr().out


def test_rejection_log_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "MARKER-HOST-91c2"
    text = VALID.read_text(encoding="utf-8").replace("broker.example.net", f"{marker} bad")
    path = tmp_path / "mqtt.json"
    path.write_text(text, encoding="utf-8")
    state = mqtt_plugin.PluginState()
    state.config_path = str(path)
    monkeypatch.setattr(mqtt_plugin, "_state", state)
    monkeypatch.setattr(mqtt_plugin, "_logging", None)
    monkeypatch.setattr(mqtt_plugin, "_program", FakeProgram.from_debug_map())
    assert mqtt_plugin.start_loop() is False
    out = capsys.readouterr().out
    assert "config_schema at '/connection/host'" in out
    assert marker not in out


def test_init_rejects_a_non_capsule() -> None:
    assert mqtt_plugin.init(object()) is False


def test_start_without_program_access_stays_inactive(
    anonymous_sparkplug: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = mqtt_plugin.PluginState()
    state.config_path = str(anonymous_sparkplug)
    monkeypatch.setattr(mqtt_plugin, "_state", state)
    monkeypatch.setattr(mqtt_plugin, "_logging", None)
    monkeypatch.setattr(mqtt_plugin, "_program", None)
    assert mqtt_plugin.start_loop() is False
    assert state.generation is None
    assert "program variable access unavailable" in capsys.readouterr().out


def test_activation_binds_every_leaf_to_the_loaded_program(anonymous_sparkplug: Path) -> None:
    state = mqtt_plugin.PluginState()
    generation = state.activate(str(anonymous_sparkplug), FakeProgram.from_debug_map())
    assert isinstance(generation, mqtt_plugin.ActiveGeneration)
    assert generation.reader is not None
    assert generation.reader.leaves == generation.config.leaves


@pytest.mark.parametrize(
    ("mutate", "pointer"),
    [
        (lambda p: p.sizes.__setitem__((0, 20), 252), "/leaves/20/size"),
        (lambda p: p.counts.__setitem__(0, 5), "/leaves/5/address"),
        (lambda p: p.sizes.pop((0, 3)), "/leaves/3/size"),
    ],
)
def test_program_mismatch_rejects_the_whole_generation(
    anonymous_sparkplug: Path, mutate: object, pointer: str
) -> None:
    program = FakeProgram.from_debug_map()
    assert callable(mutate)
    mutate(program)
    state = mqtt_plugin.PluginState()
    assert isinstance(state.activate(str(anonymous_sparkplug)), mqtt_plugin.ActiveGeneration)
    result = state.activate(str(anonymous_sparkplug), program)
    assert isinstance(result, ConfigError)
    assert (result.code, result.pointer) == ("config_address_invalid", pointer)
    assert state.generation is None and state.last_error == result


def test_program_not_loaded_rejects_before_any_secret_is_unsealed(
    anonymous_sparkplug: Path,
) -> None:
    calls: list[str] = []

    def unseal(*_args: object) -> object:
        calls.append("unseal")
        raise AssertionError("must not unseal")

    program = FakeProgram.from_debug_map()
    program.loaded = False
    result = mqtt_plugin.PluginState(unseal).activate(str(anonymous_sparkplug), program)
    assert isinstance(result, ConfigError) and result.code == "config_program_mismatch"
    assert calls == []


_LEVEL_CALL = re.compile(r"""(?:_log|_report|log)\(\s*["'](debug|info|warning|error)["']""")


def test_every_level_the_plugin_logs_maps_to_a_runtime_logger_method() -> None:
    sources = Path(mqtt_plugin.__file__).parent.rglob("*.py")
    used = {m for path in sources for m in _LEVEL_CALL.findall(path.read_text(encoding="utf-8"))}
    assert used == {"debug", "info", "warning", "error"}
    for level in used:
        assert callable(getattr(SafeLoggingAccess, mqtt_plugin.LOG_METHODS[level], None)), level


class _RuntimeLogger:
    """Exactly the methods SafeLoggingAccess offers; anything else raises AttributeError."""

    is_valid = True

    def __init__(self) -> None:
        self.lines: list[str] = []

    def log_debug(self, message: str) -> None:
        self.lines.append(f"debug {message}")

    def log_info(self, message: str) -> None:
        self.lines.append(f"info {message}")

    def log_warn(self, message: str) -> None:
        self.lines.append(f"warn {message}")

    def log_error(self, message: str) -> None:
        self.lines.append(f"error {message}")


def test_a_warning_reaches_the_runtime_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = _RuntimeLogger()
    monkeypatch.setattr(mqtt_plugin, "_logging", logger)
    for level in ("debug", "info", "warning", "error"):
        mqtt_plugin._log(level, "line")  # pylint: disable=protected-access
    assert logger.lines == [
        "debug [MQTT] line",
        "info [MQTT] line",
        "warn [MQTT] line",
        "error [MQTT] line",
    ]
