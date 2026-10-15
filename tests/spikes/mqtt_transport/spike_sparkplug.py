# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Sparkplug B mode end to end: plugin, real Mosquitto and an independent host decoder.

Runs through ``run.sh``. The plugin is started the way ``start_loop`` does it.
A 10 ms scan thread changes a counter, flips a toggle, goes null and
recovers. The host side is a separate paho client whose payloads are decoded
by Debian's C++ ``protoc --decode`` (3.21), not by the product codec, and it
sends rebirth requests, process writes, malformed and oversized commands.
"""

from __future__ import annotations

import codecs
import filecmp
import json
import os
import struct
import subprocess
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
from spike_publisher import Gate

BROKER = os.environ.get("SPIKE_BROKER", "broker")
PROTO_DIR = "/opt/sparkplug"
DEBIAN_PROTOC = "/usr/bin/protoc"
SCAN_PERIOD_S = 0.010
LINT_BASE = 2**62 + 1
WSTRING_UNITS = "A\U0001f600".encode("utf-16-le") + b"\x00\xdc"
ELAPSED_NS = -1_500_000_001
NAMES = ("Counter", "Toggle", "Big", "Speed", "Label", "Fixed", "Elapsed", "Max")
TYPES = ("DINT", "BOOL", "LINT", "REAL", "WSTRING", "INT", "TIME", "ULINT")
SIZES = {
    "DINT": 4,
    "BOOL": 1,
    "LINT": 8,
    "REAL": 4,
    "WSTRING": 253,
    "INT": 2,
    "TIME": 8,
    "ULINT": 8,
}
DATATYPES = {
    "DINT": 3,
    "BOOL": 11,
    "LINT": 4,
    "REAL": 9,
    "WSTRING": 17,
    "INT": 2,
    "TIME": 4,
    "ULINT": 8,
}
GROUP = "E2E Plant"

Text = dict[str, list[object]]


class ScanProgram:
    """DebugAccess over eight leaves that a 10 ms scan thread updates under the image lock."""

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

    def read(self, arr: int, elem: int) -> bytes:
        scans = self.scans
        if elem == 0:
            return struct.pack("<i", scans)
        if elem == 1:
            return bytes([(scans // 30) % 2])
        if elem == 2:
            return struct.pack("<q", LINT_BASE + (scans >= self.big_step_at))
        if elem == 3:
            low, high = self.null_window
            return struct.pack("<f", float("nan") if low <= scans < high else 0.25)
        if elem == 4:
            return (bytes([len(WSTRING_UNITS) // 2]) + WSTRING_UNITS).ljust(253, b"\x00")
        if elem == 5:
            return struct.pack("<h", -300)
        if elem == 6:
            return struct.pack("<q", ELAPSED_NS)
        return struct.pack("<Q", 2**64 - 1)


@dataclass(frozen=True)
class Received:
    at: float
    topic: str
    payload: bytes
    qos: int
    retain: bool


class Host:
    """Independent Sparkplug host: records the node's messages and sends commands."""

    def __init__(self, edge: str) -> None:
        self.edge = edge
        self.messages: list[Received] = []
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"r6-host-{time.monotonic_ns()}"
        )
        self._client.on_message = lambda _c, _u, m: self.messages.append(
            Received(time.monotonic(), m.topic, bytes(m.payload), m.qos, bool(m.retain))
        )
        subscribed = threading.Event()
        self._client.on_subscribe = lambda *_a: subscribed.set()
        self._client.connect(BROKER, 1883)
        self._client.subscribe(f"spBv1.0/{GROUP}/+/{edge}", qos=1)
        self._client.loop_start()
        assert subscribed.wait(5)

    def topic(self, kind: str) -> str:
        return f"spBv1.0/{GROUP}/{kind}/{self.edge}"

    def command(self, payload: bytes) -> None:
        self._client.publish(self.topic("NCMD"), payload, qos=0).wait_for_publish(5)

    def node_messages(self) -> list[tuple[str, Received]]:
        kinds = ("NBIRTH", "NDATA", "NDEATH")
        return [(m.topic.split("/")[2], m) for m in self.messages if m.topic.split("/")[2] in kinds]

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def _unquote(token: str) -> bytes:
    assert token.startswith('"') and token.endswith('"'), token
    return codecs.escape_decode(token[1:-1].encode("utf-8"))[0]  # type: ignore[no-any-return]


def _scalar(token: str) -> object:
    if token.startswith('"'):
        return _unquote(token)
    if token in ("true", "false"):
        return token == "true"
    try:
        return int(token)
    except ValueError:
        return float(token)


def decode(payload: bytes) -> Text:
    """Payload through Debian's C++ protoc, parsed from its text format."""
    out = subprocess.run(
        [
            DEBIAN_PROTOC,
            f"--proto_path={PROTO_DIR}",
            "--decode=org.eclipse.tahu.protobuf.Payload",
            "sparkplug_b.proto",
        ],
        input=payload,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    root: Text = {}
    stack = [root]
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == "}":
            stack.pop()
        elif line.endswith(" {"):
            child: Text = {}
            stack[-1].setdefault(line[:-2], []).append(child)
            stack.append(child)
        else:
            key, _, value = line.partition(": ")
            stack[-1].setdefault(key, []).append(_scalar(value))
    assert len(stack) == 1
    return root


def one(node: Text, key: str) -> object:
    (value,) = node[key]
    return value


def metrics(payload: Text) -> dict[str, Text]:
    out: dict[str, Text] = {}
    for metric in payload.get("metrics", []):
        assert isinstance(metric, dict)
        name = one(metric, "name")
        assert isinstance(name, bytes)
        out[name.decode("utf-8")] = metric
    return out


def properties(metric: Text) -> dict[str, str]:
    (props,) = metric["properties"]
    assert isinstance(props, dict)
    keys = [k.decode() for k in props["keys"]]  # type: ignore[union-attr]
    values = [one(v, "string_value").decode() for v in props["values"]]  # type: ignore[arg-type,union-attr]
    return dict(zip(keys, values))


def _config(path: Path, edge: str, host: str = BROKER, port: int = 1883) -> Path:
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
            "destination": f"Inst0.Main.{name}",
        }
        for index, name in enumerate(NAMES)
    ]
    doc = {
        "contract": "openplc-mqtt-runtime-config",
        "format_version": 1,
        "enabled": True,
        "mode": "sparkplug_b",
        "program": {"md5": "0123456789abcdef0123456789abcdef"},
        "connection": {
            "host": host,
            "port": port,
            "client_id": edge,
            "tls": {"enabled": False},
        },
        "auth": {"method": "anonymous"},
        "sparkplug": {"group_id": GROUP, "edge_node_id": edge, "sample_interval_ms": 100},
        "leaves": leaves,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _metric_bytes(name: str, datatype: int, field: int, value: int) -> bytes:
    raw = name.encode()
    body = bytes([0x0A, len(raw)]) + raw + bytes([0x20, datatype, field << 3])
    out = bytearray()
    while True:
        out.append(value & 0x7F | (0x80 if value >> 7 else 0))
        value >>= 7
        if not value:
            break
    body += bytes(out)
    return bytes([0x12, len(body)]) + body


REBIRTH = _metric_bytes("Node Control/Rebirth", 11, 14, 1)
REBIRTH_FALSE = _metric_bytes("Node Control/Rebirth", 11, 14, 0)
PROCESS_WRITE = _metric_bytes("Inst0.Main.Counter", 3, 10, 999)


@pytest.fixture(autouse=True)
def _no_publisher_left() -> Iterator[None]:
    yield
    assert not [t for t in threading.enumerate() if t.name.startswith("mqtt-publisher")]


def _wait(predicate: object, seconds: float) -> bool:
    assert callable(predicate)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _kinds(host: Host) -> list[str]:
    return [kind for kind, _ in host.node_messages()]


def _check_seq_and_bdseq(decoded: list[tuple[str, Text]]) -> list[int]:
    """Every session starts with NBIRTH seq 0; seq is contiguous; deaths match the birth bdSeq."""
    expected: int | None = None
    bd_seqs: list[int] = []
    for kind, payload in decoded:
        found = metrics(payload)
        if kind == "NBIRTH":
            assert one(payload, "seq") == 0
            bd_seq = one(found["bdSeq"], "long_value")
            assert isinstance(bd_seq, int)
            if not bd_seqs or bd_seqs[-1] != bd_seq:
                bd_seqs.append(bd_seq)
            expected = 1
        elif kind == "NDATA":
            assert expected is not None, "NDATA before any NBIRTH"
            assert one(payload, "seq") == expected
            expected = (expected + 1) % 256
        else:
            assert "seq" not in payload
            assert list(found) == ["bdSeq"] and one(found["bdSeq"], "long_value") == bd_seqs[-1]
            expected = None
    return bd_seqs


def test_generated_code_is_reproduced_by_the_pinned_protoc(tmp_path: Path) -> None:
    subprocess.run(
        [
            "/opt/protoc36/bin/protoc",
            f"--proto_path={PROTO_DIR}",
            f"--python_out={tmp_path}",
            f"{PROTO_DIR}/sparkplug_b.proto",
        ],
        check=True,
    )
    committed = PLUGINS / "mqtt" / "mqtt_sparkplug" / "sparkplug_b_pb2.py"
    assert filecmp.cmp(tmp_path / "sparkplug_b_pb2.py", committed, shallow=False)


def test_birth_data_rebirth_refused_commands_and_death(tmp_path: Path) -> None:
    edge = "r6-node"
    host = Host(edge)
    program = ScanProgram()
    program.start()
    state = mqtt_plugin.PluginState()
    result = state.start(str(_config(tmp_path / "mqtt.json", edge)), program)
    assert not isinstance(result, mqtt_plugin.ConfigError), result
    assert _wait(lambda: "NBIRTH" in _kinds(host), 5)
    time.sleep(2.2)
    for payload in (PROCESS_WRITE, REBIRTH_FALSE, b"\xff\x00garbage", b""):
        host.command(payload)
    time.sleep(0.6)
    births_before_flood = _kinds(host).count("NBIRTH")
    for _ in range(25):
        host.command(REBIRTH)
        time.sleep(0.1)
    time.sleep(1.5)
    status = dict(state.snapshot())
    scans_at_stop = program.scans
    assert state.deactivate() is True
    program.stop()
    time.sleep(0.5)
    host.close()

    node = host.node_messages()
    decoded = [(kind, decode(m.payload)) for kind, m in node]
    bd_seqs = _check_seq_and_bdseq(decoded)
    births = [p for k, p in decoded if k == "NBIRTH"]
    deaths = [p for k, p in decoded if k == "NDEATH"]
    birth_times = [one(p, "timestamp") for p in births]
    counters = [
        one(metrics(p)["Inst0.Main.Counter"], "int_value")
        for _, p in decoded
        if "Inst0.Main.Counter" in metrics(p)
    ]
    speed = [
        (
            "null"
            if metrics(p)["Inst0.Main.Speed"].get("is_null")
            else one(metrics(p)["Inst0.Main.Speed"], "float_value")
        )
        for k, p in decoded
        if k == "NDATA" and "Inst0.Main.Speed" in metrics(p)
    ]
    big = [
        one(metrics(p)["Inst0.Main.Big"], "long_value")
        for _, p in decoded
        if "Inst0.Main.Big" in metrics(p)
    ]
    report = {
        "messages": len(node),
        "kinds": {k: _kinds(host).count(k) for k in ("NBIRTH", "NDATA", "NDEATH")},
        "bd_seqs": bd_seqs,
        "births_before_flood": births_before_flood,
        "birth_gaps_ms": [b - a for a, b in pairwise(birth_times)],  # type: ignore[operator]
        "counter_first_last": [counters[0], counters[-1]],
        "scans_at_stop": scans_at_stop,
        "speed_ndata": speed,
        "big": big,
        "qos_retain": sorted({(k, m.qos, m.retain) for k, m in node}),
        "max_scan_lock_wait_ms": round(program.max_lock_wait * 1000, 3),
        "status_sparkplug": status.get("sparkplug"),
    }
    print("R6-LIFECYCLE " + json.dumps(report, sort_keys=True))

    assert node[0][0] == "NBIRTH" and node[-1][0] == "NDEATH"
    assert len(bd_seqs) == 1 and len(deaths) == 1
    first = metrics(births[0])
    assert list(first)[:2] == ["bdSeq", "Node Control/Rebirth"]
    assert one(first["Node Control/Rebirth"], "boolean_value") is False
    for name, iec in zip(NAMES, TYPES):
        metric = first[f"Inst0.Main.{name}"]
        assert one(metric, "datatype") == DATATYPES[iec]
        assert properties(metric)["iecType"] == iec
    assert properties(first["Inst0.Main.Elapsed"]) == {
        "iecType": "TIME",
        "encoding": "int64-ns-duration",
        "unit": "ns",
    }
    assert properties(first["Inst0.Main.Label"])["encoding"] == "utf16le"
    assert one(first["Inst0.Main.Label"], "bytes_value") == WSTRING_UNITS
    assert one(first["Inst0.Main.Elapsed"], "long_value") == ELAPSED_NS & (2**64 - 1)
    assert one(first["Inst0.Main.Max"], "long_value") == 2**64 - 1
    assert one(first["Inst0.Main.Fixed"], "int_value") == (-300) & 0xFFFF_FFFF
    assert births_before_flood == 1, "refused commands must not cause a birth"
    assert 2 <= len(births) <= 4
    assert all(gap >= 2000 for gap in report["birth_gaps_ms"])  # type: ignore[union-attr]
    for birth in births:
        assert len(metrics(birth)) == len(NAMES) + 2
    assert all(isinstance(v, int) for v in counters) and 999 not in counters
    assert counters == sorted(counters)
    assert scans_at_stop - 30 <= counters[-1] <= scans_at_stop  # type: ignore[operator]
    assert speed[:2] == ["null", 0.25]
    assert big[0] == LINT_BASE and big[-1] == LINT_BASE + 1
    assert set(report["qos_retain"]) == {  # type: ignore[arg-type]
        ("NBIRTH", 0, False),
        ("NDATA", 0, False),
        ("NDEATH", 1, False),
    }
    assert status["sparkplug"]["bdSeq"] == bd_seqs[0]  # type: ignore[index]
    assert program.max_lock_wait < 0.005


def test_connection_loss_publishes_the_will_and_reconnects_with_the_next_bdseq(
    tmp_path: Path,
) -> None:
    edge = "r6-gate"
    host = Host(edge)
    gate = Gate(1883)
    program = ScanProgram()
    program.null_window = (0, 0)
    program.start()
    state = mqtt_plugin.PluginState()
    path = _config(tmp_path / "mqtt.json", edge, host="127.0.0.1", port=gate.port)
    assert not isinstance(state.start(str(path), program), mqtt_plugin.ConfigError)
    assert _wait(lambda: "NDATA" in _kinds(host), 5)
    gate.outage()
    assert _wait(lambda: "NDEATH" in _kinds(host), 5), "broker did not publish the will"
    time.sleep(1.5)
    scans_at_restore = program.scans
    gate.restore()
    assert _wait(lambda: _kinds(host).count("NBIRTH") == 2, 15)
    time.sleep(0.5)
    assert state.deactivate() is True
    program.stop()
    gate.close()
    time.sleep(0.5)
    host.close()

    node = host.node_messages()
    decoded = [(kind, decode(m.payload)) for kind, m in node]
    bd_seqs = _check_seq_and_bdseq(decoded)
    kinds = [k for k, _ in decoded]
    will_at = kinds.index("NDEATH")
    second_birth = decoded[will_at + 1]
    counter_after = one(metrics(second_birth[1])["Inst0.Main.Counter"], "int_value")
    report = {
        "kinds": kinds[:3] + ["..."] + kinds[will_at - 1 : will_at + 3] + ["..."] + kinds[-2:],
        "bd_seqs": bd_seqs,
        "will_timestamp_present": "timestamp" in decoded[will_at][1],
        "final_death_timestamp_present": "timestamp" in decoded[-1][1],
        "counter_in_second_birth": counter_after,
        "scans_at_restore": scans_at_restore,
        "gate_connections": gate.accepted,
    }
    print("R6-RECONNECT " + json.dumps(report, sort_keys=True))
    assert kinds[0] == "NBIRTH" and kinds[-1] == "NDEATH" and kinds.count("NDEATH") == 2
    assert second_birth[0] == "NBIRTH"
    assert len(bd_seqs) == 2 and bd_seqs[1] != bd_seqs[0]
    assert (bd_seqs[1] - bd_seqs[0]) % 256 >= 1
    assert "timestamp" not in decoded[will_at][1] and "timestamp" in decoded[-1][1]
    assert isinstance(counter_after, int) and counter_after >= scans_at_restore


def test_oversized_command_costs_one_reconnect_and_never_reaches_the_program(
    tmp_path: Path,
) -> None:
    edge = "r6-oversized"
    host = Host(edge)
    program = ScanProgram()
    program.null_window = (0, 0)
    program.start()
    state = mqtt_plugin.PluginState()
    assert not isinstance(
        state.start(str(_config(tmp_path / "mqtt.json", edge)), program), mqtt_plugin.ConfigError
    )
    assert _wait(lambda: "NDATA" in _kinds(host), 5)
    scans_at_command = program.scans
    host.command(REBIRTH + b"\x00" * 20_000)
    assert _wait(lambda: "NDEATH" in _kinds(host), 5)
    assert _wait(lambda: state.snapshot()["error"] is not None, 5)
    reported = dict(state.snapshot())
    assert _wait(lambda: _kinds(host).count("NBIRTH") == 2, 10)
    scans_before_stop = program.scans
    assert state.deactivate() is True
    program.stop()
    time.sleep(0.5)
    host.close()
    decoded = [(kind, decode(m.payload)) for kind, m in host.node_messages()]
    bd_seqs = _check_seq_and_bdseq(decoded)
    report = {
        "bd_seqs": bd_seqs,
        "error_after_drop": reported.get("error"),
        "state_after_drop": reported.get("state"),
        "scans_at_command": scans_at_command,
        "scans_before_stop": scans_before_stop,
    }
    print("R6-OVERSIZED " + json.dumps(report, sort_keys=True))
    assert len(bd_seqs) == 2
    error = reported["error"]
    assert isinstance(error, dict) and error["code"] == "inbound_packet_too_large"
    assert scans_before_stop - scans_at_command >= 50, "the scan kept running across the drop"
