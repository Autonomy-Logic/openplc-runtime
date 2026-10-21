# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Plugin lifecycle with a real publisher thread: start, stop, replacement, refusal."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import mqtt_plugin
import pytest
from api_schema import check
from contract.runtime_config import MqttRuntimeConfig
from mqtt_publisher.backoff import Backoff
from mqtt_publisher.clock import SystemClock
from mqtt_publisher.connection import ConnectSpec
from mqtt_publisher.status import StatusTracker, StatusWriter
from mqtt_publisher.supervisor import Publisher, Supervisor, SupervisorSettings
from program_fakes import FakeProgram
from publisher_fakes import (
    CounterSampler,
    FakeNetwork,
    FakeSession,
    RealTime,
    RecordingStrategy,
)

CONFIG_DIR = (
    Path(__file__).resolve().parents[4]
    / "core/src/drivers/plugins/python/mqtt/contract/fixtures/runtime-config"
)
SPARKPLUG = CONFIG_DIR / "valid" / "sparkplug-client-certificate-all-types.json"
GENERIC = CONFIG_DIR / "valid" / "generic-on-change-anonymous-plaintext.json"


def _anonymous(source: Path, target: Path) -> Path:
    doc = json.loads(source.read_text(encoding="utf-8"))
    doc["auth"] = {"method": "anonymous"}
    target.write_text(json.dumps(doc), encoding="utf-8")
    return target


class Harness:
    def __init__(self, network: FakeNetwork) -> None:
        self.network = network
        self.strategies: list[RecordingStrategy] = []
        self.order: list[str] = []

    def strategy(self, config: MqttRuntimeConfig, _writer: StatusWriter) -> RecordingStrategy:
        strategy = RecordingStrategy(
            interval_ms=config.generic.interval_ms if config.generic else 100
        )
        self.strategies.append(strategy)
        return strategy

    def transport(self, spec: ConnectSpec) -> FakeSession:
        self.order.append(f"connect:{spec.client_id}")
        return self.network.factory(spec)

    def factory(self) -> mqtt_plugin.PublisherFactory:
        builders: dict[str, Callable[[MqttRuntimeConfig, StatusWriter], RecordingStrategy]] = {
            "generic": self.strategy,
            "sparkplug_b": self.strategy,
        }
        return mqtt_plugin.PublisherFactory(builders, self.transport, log=lambda *_a: None)  # type: ignore[arg-type]


@pytest.fixture
def harness() -> Harness:
    return Harness(FakeNetwork(RealTime()))


@pytest.fixture
def plugin(
    tmp_path: Path, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> Iterator[mqtt_plugin.PluginState]:
    state = mqtt_plugin.PluginState(publishers=harness.factory())
    state.config_path = str(_anonymous(SPARKPLUG, tmp_path / "mqtt.json"))
    monkeypatch.setattr(mqtt_plugin, "_state", state)
    monkeypatch.setattr(mqtt_plugin, "_logging", None)
    monkeypatch.setattr(mqtt_plugin, "_program", FakeProgram.from_debug_map())
    yield state
    state.deactivate()


def _publisher_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("mqtt-publisher-")]


def _until(predicate: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_start_publishes_and_stop_closes_within_the_budget(
    plugin: mqtt_plugin.PluginState, harness: Harness
) -> None:
    assert mqtt_plugin.start_loop() is True
    assert _until(lambda: plugin.snapshot()["state"] == "connected")
    assert _until(lambda: len(harness.network.delivered) >= 42)
    assert check(mqtt_plugin.status_snapshot(), "status") == []
    start = time.monotonic()
    assert mqtt_plugin.stop_loop() is True
    assert time.monotonic() - start < 1.0
    assert _publisher_threads() == []
    session = harness.network.sessions[0]
    assert session.closed and not session.aborted and [m.topic for m in session.final] == ["state"]
    status = plugin.snapshot()
    assert status["state"] == "plc_stopped" and check(status, "status") == []
    assert plugin.generation is None and plugin.publisher is None


def test_stop_during_retry_cancels_it(plugin: mqtt_plugin.PluginState, harness: Harness) -> None:
    harness.network.up = False
    assert mqtt_plugin.start_loop() is True
    assert _until(lambda: plugin.snapshot()["state"] == "retry_wait")
    attempts = len(harness.network.sessions)
    start = time.monotonic()
    assert mqtt_plugin.stop_loop() is True
    assert time.monotonic() - start < 0.5
    time.sleep(1.5)
    assert len(harness.network.sessions) == attempts
    assert _publisher_threads() == []


@pytest.mark.parametrize("second", [SPARKPLUG, GENERIC], ids=["program", "mode"])
def test_replacement_stops_the_previous_publisher_before_the_new_one(
    plugin: mqtt_plugin.PluginState, harness: Harness, tmp_path: Path, second: Path
) -> None:
    assert mqtt_plugin.start_loop() is True
    assert _until(lambda: len(harness.network.delivered) > 0)
    first = plugin.publisher
    assert first is not None
    _anonymous(second, Path(plugin.config_path))
    assert mqtt_plugin.start_loop() is True
    assert not first.alive
    assert harness.network.sessions[0].closed
    assert len(_publisher_threads()) == 1
    current = plugin.publisher
    assert current is not None and current is not first and current.alive
    assert plugin.generation is not None and plugin.generation.number == 2
    assert harness.strategies[0].disconnects == 1


def test_invalid_configuration_never_connects(
    plugin: mqtt_plugin.PluginState, harness: Harness
) -> None:
    Path(plugin.config_path).write_bytes(
        (CONFIG_DIR / "invalid" / "unknown-version.json").read_bytes()
    )
    assert mqtt_plugin.start_loop() is False
    assert harness.network.sessions == [] and _publisher_threads() == []
    status = plugin.snapshot()
    assert status["state"] == "config_error" and check(status, "status") == []
    error = status["error"]
    assert isinstance(error, dict) and error["code"] == "config_unknown_version"


def test_missing_credential_is_a_credential_error(
    plugin: mqtt_plugin.PluginState, harness: Harness
) -> None:
    Path(plugin.config_path).write_bytes(SPARKPLUG.read_bytes())
    assert mqtt_plugin.start_loop() is False
    assert harness.network.sessions == []
    status = plugin.snapshot()
    assert status["state"] == "credential_error" and check(status, "status") == []


def test_program_mismatch_never_connects(
    plugin: mqtt_plugin.PluginState, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = FakeProgram.from_debug_map()
    program.loaded = False
    monkeypatch.setattr(mqtt_plugin, "_program", program)
    assert mqtt_plugin.start_loop() is False
    assert harness.network.sessions == []
    assert plugin.snapshot()["state"] == "config_error"


def test_disabled_integration_reports_disabled(plugin: mqtt_plugin.PluginState) -> None:
    Path(plugin.config_path).unlink()
    assert mqtt_plugin.stop_loop() is True
    assert plugin.snapshot() == {
        "contract": "openplc-mqtt-status",
        "version": 1,
        "configured": False,
        "state": "disabled",
        "since": plugin.snapshot()["since"],
        "error": None,
    }


@pytest.mark.parametrize("missing", ["strategy", "transport"])
def test_unavailable_mode_or_library_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    network = FakeNetwork(RealTime())
    strategies = (
        {} if missing == "strategy" else {"sparkplug_b": lambda _c, _w: RecordingStrategy()}
    )
    transport = network.factory if missing == "strategy" else None
    monkeypatch.setattr(mqtt_plugin, "TRANSPORT_FACTORY", None)
    state = mqtt_plugin.PluginState(
        publishers=mqtt_plugin.PublisherFactory(strategies, transport)  # type: ignore[arg-type]
    )
    path = _anonymous(SPARKPLUG, tmp_path / "mqtt.json")
    result = state.start(str(path), FakeProgram.from_debug_map())
    assert isinstance(result, mqtt_plugin.ConfigError) and result.code == "internal_error"
    assert network.sessions == [] and state.publisher is None and state.generation is None
    status = state.snapshot()
    assert status["state"] == "config_error" and check(status, "status") == []


def test_default_factory_uses_the_pinned_transport() -> None:
    assert mqtt_plugin.TRANSPORT_FACTORY is not None
    assert (
        mqtt_plugin.tick_interval_ms(
            mqtt_plugin.load_active_config(str(GENERIC))  # type: ignore[arg-type]
        )
        == 100
    )


def test_publisher_stop_reports_an_overrun() -> None:
    release = threading.Event()

    class StuckSampler(CounterSampler):
        def sample(self):  # type: ignore[no-untyped-def]
            release.wait(5)
            return super().sample()

    network = FakeNetwork(RealTime())
    tracker = StatusTracker(SystemClock())
    supervisor = Supervisor(
        ConnectSpec("h", 1, "c", None),
        network.factory,
        RecordingStrategy(),
        StuckSampler([1]),
        tracker.reset("connecting", None),
        SystemClock(),
        Backoff(),
        SupervisorSettings(tick_interval_s=0.1),
        lambda *_a: None,
    )
    publisher = Publisher(supervisor, "mqtt-publisher-stuck")
    publisher.start()
    assert _until(lambda: len(network.sessions) == 1)
    assert publisher.stop(0.2) is False
    release.set()
    assert _until(lambda: not publisher.alive)
    assert network.sessions[0].closed
