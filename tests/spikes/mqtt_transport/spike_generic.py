# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Generic mode end to end: plugin, real Mosquitto and an independent subscriber.

Runs through ``run.sh``. The plugin is started the way ``start_loop`` does it
(installed configuration file, bound leaves, registered strategy, pinned
transport). The stand-in program changes a counter every 10 ms scan, flips a
toggle, goes null and recovers, so frozen, duplicated or truncated values are
observable. The subscriber decodes the wire with its own JSON parsing and
expected values computed from the program, never with the product serializer.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

PLUGINS = Path(os.environ.get("SPIKE_PLUGINS", "/plugins"))
sys.path[:0] = [str(PLUGINS / "mqtt"), str(PLUGINS)]

# pylint: disable=wrong-import-position
import mqtt_plugin

BROKER = os.environ.get("SPIKE_BROKER", "broker")
SCAN_PERIOD_S = 0.010
LINT_BASE = 2**62 + 1
WSTRING_UNITS = "A\U0001f600".encode("utf-16-le") + b"\x00\xdc"
NAMES = ("Counter", "Toggle", "Big", "Speed", "Label", "Fixed")
TYPES = ("DINT", "BOOL", "LINT", "REAL", "WSTRING", "INT")
SIZES = {"DINT": 4, "BOOL": 1, "LINT": 8, "REAL": 4, "WSTRING": 253, "INT": 2}


class ScanProgram:
    """DebugAccess over six leaves that a 10 ms scan thread updates under the image lock."""

    def __init__(self) -> None:
        self._image = threading.Lock()
        self._stop = threading.Event()
        self.scans = 0
        self.max_lock_wait = 0.0
        self.null_window = (100, 160)
        self.big_step_at = 150
        self._thread = threading.Thread(target=self._scan, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)

    def _scan(self) -> None:
        while not self._stop.is_set():
            wanted = time.monotonic()
            with self._image:
                self.max_lock_wait = max(self.max_lock_wait, time.monotonic() - wanted)
                self.scans += 1
            time.sleep(SCAN_PERIOD_S)

    def lock(self) -> None:
        self._image.acquire()

    def unlock(self) -> None:
        self._image.release()

    def array_count(self) -> int:
        return 1

    def elem_count(self, arr: int) -> int:
        return len(TYPES)

    def size(self, arr: int, elem: int) -> int:
        return SIZES[TYPES[elem]]

    def toggle(self, scans: int) -> bool:
        return (scans // 30) % 2 == 1

    def read(self, arr: int, elem: int) -> bytes:
        scans = self.scans
        if elem == 0:
            return struct.pack("<i", scans)
        if elem == 1:
            return bytes([int(self.toggle(scans))])
        if elem == 2:
            return struct.pack("<q", LINT_BASE + (scans >= self.big_step_at))
        if elem == 3:
            low, high = self.null_window
            return struct.pack("<f", float("nan") if low <= scans < high else 0.25)
        if elem == 4:
            return (bytes([len(WSTRING_UNITS) // 2]) + WSTRING_UNITS).ljust(253, b"\x00")
        return struct.pack("<h", -300)


@dataclass(frozen=True)
class Received:
    at: float
    topic: str
    payload: bytes
    qos: int
    retain: bool


class Subscriber:
    def __init__(self, pattern: str, name: str) -> None:
        self.messages: list[Received] = []
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"{name}-{time.monotonic_ns()}"
        )
        self._client.on_message = lambda _c, _u, m: self.messages.append(
            Received(time.monotonic(), m.topic, bytes(m.payload), m.qos, bool(m.retain))
        )
        subscribed = threading.Event()
        self._client.on_subscribe = lambda *_a: subscribed.set()
        self._client.connect(BROKER, 1883)
        self._client.subscribe(pattern, qos=1)
        self._client.loop_start()
        assert subscribed.wait(5)

    def on(self, leaf: str) -> list[Received]:
        return [m for m in self.messages if m.topic.endswith(f"/{leaf}")]

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def _value(payload: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        keys = [key for key, _ in items]
        assert keys == ["value"], keys
        return dict(items)

    return json.loads(payload, object_pairs_hook=pairs)["value"]


def _config(path: Path, client_id: str, generic: dict[str, object]) -> Path:
    leaves = [
        {
            "source": {
                "tokens": [
                    {"role": "instance", "name": "Inst0"},
                    {"role": "program", "name": "Main"},
                    {"role": "variable", "name": name},
                ],
                "debug_path": f"INST0.{name.upper()}",
            },
            "address": {"arr": 0, "elem": index},
            "iec_type": TYPES[index],
            "size": SIZES[TYPES[index]],
            "destination": f"openplc/{client_id}/Inst0/Main/{name}",
        }
        for index, name in enumerate(NAMES)
    ]
    doc = {
        "contract": "openplc-mqtt-runtime-config",
        "format_version": 1,
        "enabled": True,
        "mode": "generic",
        "program": {"md5": "0123456789abcdef0123456789abcdef"},
        "connection": {
            "host": BROKER,
            "port": 1883,
            "client_id": client_id,
            "tls": {"enabled": False},
        },
        "auth": {"method": "anonymous"},
        "generic": generic,
        "leaves": leaves,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _clear_retained(client_id: str) -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"clear-{client_id}")
    client.connect(BROKER, 1883)
    client.loop_start()
    for name in NAMES:
        client.publish(
            f"openplc/{client_id}/Inst0/Main/{name}", b"", qos=1, retain=True
        ).wait_for_publish(5)
    client.loop_stop()
    client.disconnect()


@pytest.fixture(autouse=True)
def _no_publisher_left() -> Iterator[None]:
    yield
    assert not [t for t in threading.enumerate() if t.name.startswith("mqtt-publisher")]


def _run(path: Path, program: ScanProgram, seconds: float) -> tuple[dict[str, object], int]:
    state = mqtt_plugin.PluginState()
    result = state.start(str(path), program)
    assert not isinstance(result, mqtt_plugin.ConfigError), result
    time.sleep(seconds)
    stats = dict(state.snapshot())
    scans = program.scans
    assert state.deactivate() is True
    return stats, scans


def test_on_change_qos0_counter_toggle_null_recovery_and_no_retain(tmp_path: Path) -> None:
    client_id = "e2e-onchange"
    _clear_retained(client_id)
    subscriber = Subscriber(f"openplc/{client_id}/#", "r5-consumer")
    program = ScanProgram()
    program.start()
    path = _config(
        tmp_path / "mqtt.json",
        client_id,
        {"publish_mode": "on_change", "sample_interval_ms": 100, "qos": 0, "retain": False},
    )
    status, scans_at_stop = _run(path, program, 3.0)
    program.stop()
    time.sleep(0.5)
    subscriber.close()

    counter = [_value(m.payload) for m in subscriber.on("Counter")]
    toggle = [_value(m.payload) for m in subscriber.on("Toggle")]
    big = [_value(m.payload) for m in subscriber.on("Big")]
    speed = [_value(m.payload) for m in subscriber.on("Speed")]
    label = [_value(m.payload) for m in subscriber.on("Label")]
    fixed = [_value(m.payload) for m in subscriber.on("Fixed")]
    late = Subscriber(f"openplc/{client_id}/#", "r5-late")
    time.sleep(1.0)
    late.close()
    report = {
        "received": len(subscriber.messages),
        "counter_count": len(counter),
        "counter_first_last": [counter[0], counter[-1]] if counter else None,
        "scans_at_stop": scans_at_stop,
        "toggle": toggle,
        "big": big,
        "speed": speed,
        "label": label,
        "fixed": fixed,
        "late_retained": len(late.messages),
        "max_scan_lock_wait_ms": round(program.max_lock_wait * 1000, 3),
        "status_stats": status.get("stats"),
    }
    print("R5-ONCHANGE " + json.dumps(report, sort_keys=True))
    assert all(isinstance(v, int) for v in counter)
    assert counter == sorted(counter) and len(set(counter)) == len(counter) >= 20
    assert scans_at_stop - 30 <= counter[-1] <= scans_at_stop  # type: ignore[operator]
    assert len(toggle) >= 5 and all(a != b for a, b in pairwise(toggle))
    assert big == [str(LINT_BASE), str(LINT_BASE + 1)]
    assert speed == [0.25, None, 0.25]
    assert len(label) == 1 and base64.b64decode(str(label[0])) == WSTRING_UNITS
    assert fixed == [-300]
    assert {(m.qos, m.retain) for m in subscriber.messages} == {(0, False)}
    assert late.messages == []
    stats = status["stats"]
    assert isinstance(stats, dict) and stats["published"] >= len(subscriber.messages) - len(NAMES)
    assert program.max_lock_wait < 0.005


def test_periodic_qos1_retain_republishes_stable_values_and_leaves_them_retained(
    tmp_path: Path,
) -> None:
    client_id = "e2e-periodic"
    _clear_retained(client_id)
    subscriber = Subscriber(f"openplc/{client_id}/#", "r5-consumer")
    program = ScanProgram()
    program.null_window = (0, 0)
    program.big_step_at = 10**9
    program.start()
    path = _config(
        tmp_path / "mqtt.json",
        client_id,
        {"publish_mode": "periodic", "publish_interval_ms": 1000, "qos": 1, "retain": True},
    )
    status, _scans = _run(path, program, 3.5)
    program.stop()
    time.sleep(0.5)
    subscriber.close()
    late = Subscriber(f"openplc/{client_id}/#", "r5-late")
    time.sleep(1.0)
    late.close()
    _clear_retained(client_id)

    fixed = subscriber.on("Fixed")
    gaps = [round(b.at - a.at, 3) for a, b in pairwise(fixed)]
    report = {
        "received": len(subscriber.messages),
        "per_topic": {name: len(subscriber.on(name)) for name in NAMES},
        "fixed_gaps_s": gaps,
        "late_retained": [(m.topic.rsplit("/", 1)[1], m.retain) for m in late.messages],
        "status_stats": status.get("stats"),
    }
    print("R5-PERIODIC " + json.dumps(report, sort_keys=True))
    for name in NAMES:
        assert 3 <= len(subscriber.on(name)) <= 5, name
    assert {_value(m.payload) for m in fixed} == {-300}
    assert {_value(m.payload) for m in subscriber.on("Big")} == {str(LINT_BASE)}
    assert all(0.8 <= gap <= 1.25 for gap in gaps), gaps
    counter = [_value(m.payload) for m in subscriber.on("Counter")]
    assert counter == sorted(counter) and len(set(counter)) == len(counter)
    assert {(m.qos, m.retain) for m in subscriber.messages} == {(1, False)}
    assert sorted(m.topic.rsplit("/", 1)[1] for m in late.messages) == sorted(NAMES)
    assert all(m.retain for m in late.messages)
    last_counter = _value(late.on("Counter")[0].payload)
    assert last_counter == counter[-1]
    stats = status["stats"]
    assert isinstance(stats, dict) and stats["published"] <= len(subscriber.messages)
