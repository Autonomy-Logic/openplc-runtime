# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Sparkplug B mode: bdSeq/seq lifecycle, NBIRTH before NDATA, rebirth, refused commands, death."""

from __future__ import annotations

import dataclasses
import json
import re
import struct
import threading
from collections.abc import Sequence
from pathlib import Path

import mqtt_plugin
import pytest
import sparkplug_wire as wire
from api_schema import check
from contract.constants import DEBUG_SIZE, MAX_LEAVES
from contract.runtime_config import MqttRuntimeConfig, decode_runtime_config
from fake_broker import FakeBroker
from mqtt_publisher.backoff import Backoff
from mqtt_publisher.connection import ConnectSpec
from mqtt_publisher.sparkplug import (
    PROCESS_BD_SEQ,
    BdSeqCounter,
    SparkplugStrategy,
    build_sparkplug_strategy,
)
from mqtt_publisher.status import StatusTracker
from mqtt_publisher.strategy import InboundMessage, OutboundMessage, PublishOutcome
from mqtt_publisher.supervisor import Supervisor, SupervisorSettings
from mqtt_reader.reader import LeafIssue, SampleResult
from mqtt_reader.values import LeafValue, convert_debug_read, convert_window
from program_fakes import FakeProgram
from publisher_fakes import FakeClock, FakeNetwork, RealTime

MQTT_DIR = Path(__file__).resolve().parents[4] / "core/src/drivers/plugins/python/mqtt"
FIXTURES = MQTT_DIR / "contract/fixtures"
SPARKPLUG_FIXTURE = FIXTURES / "runtime-config/valid/sparkplug-client-certificate-all-types.json"
GENERIC_FIXTURE = FIXTURES / "runtime-config/valid/generic-on-change-anonymous-plaintext.json"
BIRTH = "spBv1.0/Plant 1/NBIRTH/plc-linha1"
DATA = "spBv1.0/Plant 1/NDATA/plc-linha1"
DEATH = "spBv1.0/Plant 1/NDEATH/plc-linha1"
NCMD = "spBv1.0/Plant 1/NCMD/plc-linha1"
NOW = 1_791_547_200_000


class Mono:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _doc(types: Sequence[str], interval_ms: int = 100) -> dict[str, object]:
    return {
        "contract": "openplc-mqtt-runtime-config",
        "format_version": 1,
        "enabled": True,
        "mode": "sparkplug_b",
        "program": {"md5": "3f2c9d0e8b7a6f5e4d3c2b1a09f8e7d6"},
        "connection": {
            "host": "127.0.0.1",
            "port": 1883,
            "client_id": "plc-linha1",
            "tls": {"enabled": False},
        },
        "auth": {"method": "anonymous"},
        "sparkplug": {
            "group_id": "Plant 1",
            "edge_node_id": "plc-linha1",
            "sample_interval_ms": interval_ms,
        },
        "leaves": [
            {
                "source": {
                    "tokens": [
                        {"role": "instance", "name": "Inst0"},
                        {"role": "variable", "name": f"V{index}"},
                    ],
                    "debug_path": f"INST0.V{index}",
                },
                "address": {"arr": 0, "elem": index},
                "iec_type": iec_type,
                "size": DEBUG_SIZE[iec_type],
                "destination": f"Inst0.V{index}",
            }
            for index, iec_type in enumerate(types)
        ],
    }


def _config(types: Sequence[str], interval_ms: int = 100) -> MqttRuntimeConfig:
    return decode_runtime_config(json.dumps(_doc(types, interval_ms)).encode(), None)


def _strategy(
    types: Sequence[str] = ("DINT", "BOOL", "REAL"),
    start: int = 0,
    mono: Mono | None = None,
    logs: list[tuple[str, str]] | None = None,
) -> SparkplugStrategy:
    return SparkplugStrategy(
        _config(types),
        None,
        bd_seq=BdSeqCounter(start),
        log=None if logs is None else lambda level, line: logs.append((level, line)),
        monotonic=mono or Mono(),
    )


def _sample(values: Sequence[LeafValue]) -> SampleResult:
    issues = tuple(LeafIssue(i, v.issue) for i, v in enumerate(values) if v.issue)
    return SampleResult(tuple(values), issues)


def _dint(value: int) -> LeafValue:
    return convert_window("DINT", struct.pack("<i", value))


def _bool(flag: bool) -> LeafValue:
    return convert_window("BOOL", bytes([int(flag)]))


def _real(value: float) -> LeafValue:
    return convert_window("REAL", struct.pack("<f", value))


def _lint(value: int) -> LeafValue:
    return convert_window("LINT", struct.pack("<q", value))


def _values(counter: int, toggle: bool = False, real: float = 1.5) -> SampleResult:
    return _sample([_dint(counter), _bool(toggle), _real(real)])


def _accept(strategy: SparkplugStrategy, messages: Sequence[OutboundMessage]) -> None:
    strategy.after_publish(PublishOutcome(tuple(messages), ()))


def _drop(strategy: SparkplugStrategy, messages: Sequence[OutboundMessage]) -> None:
    strategy.after_publish(PublishOutcome((), tuple(messages)))


def _born(strategy: SparkplugStrategy, sample: SampleResult | None = None) -> wire.Payload:
    strategy.will()
    (birth,) = strategy.on_connected(sample or _values(0), NOW)
    _accept(strategy, [birth])
    return wire.payload(birth.payload)


def _names(payload: wire.Payload) -> list[str | None]:
    return [m.name for m in payload.metrics]


# Topics, will and bdSeq


def test_topics_subscription_and_tick_follow_the_configuration() -> None:
    strategy = SparkplugStrategy(
        decode_runtime_config(SPARKPLUG_FIXTURE.read_bytes(), None), bd_seq=BdSeqCounter()
    )
    assert strategy.subscriptions() == ((NCMD, 0),)
    assert strategy.command_topic == NCMD and strategy.tick_interval_ms == 100
    will = strategy.will()
    assert will is not None and (will.topic, will.qos, will.retain) == (DEATH, 1, False)


def test_every_attempt_takes_the_next_bdseq_and_wraps_after_255() -> None:
    strategy = _strategy(start=254)
    taken = []
    for _ in range(3):
        will = strategy.will()
        assert will is not None
        death = wire.payload(will.payload)
        assert death.seq is None and death.timestamp is None
        assert [(m.name, m.datatype) for m in death.metrics] == [("bdSeq", 4)]
        taken.append(death.metrics[0].value)
        assert strategy.bd_seq == taken[-1]
    assert taken == [254, 255, 0]


def test_the_process_counter_spans_strategies_so_a_new_generation_never_reuses_a_bdseq() -> None:
    writer = StatusTracker(FakeClock()).reset("connecting", None)
    first = build_sparkplug_strategy(_config(["DINT"]), writer)
    second = build_sparkplug_strategy(_config(["DINT"]), writer)
    first.will()
    second.will()
    assert first.bd_seq is not None and second.bd_seq == (first.bd_seq + 1) % 256
    assert PROCESS_BD_SEQ.take() == (second.bd_seq + 1) % 256


# NBIRTH


def test_birth_is_complete_with_the_bdseq_of_the_registered_will() -> None:
    config = decode_runtime_config(SPARKPLUG_FIXTURE.read_bytes(), None)
    strategy = SparkplugStrategy(config, bd_seq=BdSeqCounter(41))
    will = strategy.will()
    values = [
        convert_debug_read(leaf.iec_type, bytes(DEBUG_SIZE[leaf.iec_type]))
        for leaf in config.leaves
    ]
    (birth,) = strategy.on_connected(_sample(values), NOW)
    assert (birth.topic, birth.qos, birth.retain) == (BIRTH, 0, False)
    decoded = wire.payload(birth.payload)
    assert decoded.seq == 0 and decoded.timestamp == NOW
    assert _names(decoded) == [
        "bdSeq",
        "Node Control/Rebirth",
        *[leaf.destination for leaf in config.leaves],
    ]
    assert will is not None
    assert decoded.metric("bdSeq").value == wire.payload(will.payload).metrics[0].value == 41
    assert {m.datatype for m in decoded.metrics[2:]} >= {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 17}
    assert all(m.properties.get("iecType") for m in decoded.metrics[2:])
    assert all(m.timestamp == NOW for m in decoded.metrics)


def test_no_ndata_before_the_birth_of_the_session_was_accepted() -> None:
    strategy = _strategy()
    strategy.will()
    (birth,) = strategy.on_connected(_values(0), NOW)
    _drop(strategy, [birth])
    (retry,) = strategy.on_sample(_values(1), NOW + 100)
    assert retry.topic == BIRTH
    assert wire.payload(retry.payload).metric("Inst0.V0").value == 1
    _accept(strategy, [retry])
    (data,) = strategy.on_sample(_values(2), NOW + 200)
    assert data.topic == DATA and wire.payload(data.payload).seq == 1


# NDATA


def test_ndata_carries_only_changed_metrics_and_increments_seq() -> None:
    strategy = _strategy()
    _born(strategy)
    assert strategy.on_sample(_values(0), NOW + 100) == []
    (first,) = strategy.on_sample(_values(1), NOW + 200)
    _accept(strategy, [first])
    decoded = wire.payload(first.payload)
    assert (first.topic, first.qos, first.retain) == (DATA, 0, False)
    assert decoded.seq == 1 and decoded.timestamp == NOW + 200
    assert [(m.name, m.datatype, m.value) for m in decoded.metrics] == [("Inst0.V0", 3, 1)]
    assert decoded.metrics[0].properties == {} and decoded.metrics[0].timestamp == NOW + 200
    (second,) = strategy.on_sample(_values(1, toggle=True), NOW + 300)
    _accept(strategy, [second])
    assert [(m.name, m.value) for m in wire.payload(second.payload).metrics] == [("Inst0.V1", True)]
    assert wire.payload(second.payload).seq == 2 == strategy.seq


def test_seq_wraps_from_255_to_0() -> None:
    strategy = _strategy()
    _born(strategy)
    seqs = []
    for counter in range(1, 300):
        (message,) = strategy.on_sample(_values(counter), NOW + counter)
        _accept(strategy, [message])
        seqs.append(wire.payload(message.payload).seq)
    assert seqs[:255] == list(range(1, 256))
    assert seqs[255:258] == [0, 1, 2]


def test_dropped_ndata_keeps_seq_and_baseline_so_the_wire_has_no_gap() -> None:
    strategy = _strategy()
    _born(strategy)
    (lost,) = strategy.on_sample(_values(5), NOW + 100)
    _drop(strategy, [lost])
    (again,) = strategy.on_sample(_values(5), NOW + 200)
    assert wire.payload(again.payload).seq == wire.payload(lost.payload).seq == 1
    _drop(strategy, [again])
    assert strategy.on_sample(_values(0), NOW + 300) == []
    (third,) = strategy.on_sample(_values(6), NOW + 400)
    assert wire.payload(third.payload).seq == 1


def test_null_and_recovery_are_changes_and_64_bit_and_negative_zero_are_compared_exactly() -> None:
    strategy = _strategy(("REAL", "LINT"))
    _born(strategy, _sample([_real(2.5), _lint(2**53)]))
    (null,) = strategy.on_sample(_sample([_real(float("nan")), _lint(2**53)]), NOW + 1)
    _accept(strategy, [null])
    assert [(m.name, m.is_null, m.value_field) for m in wire.payload(null.payload).metrics] == [
        ("Inst0.V0", True, None)
    ]
    assert strategy.on_sample(_sample([_real(float("inf")), _lint(2**53)]), NOW + 2) == []
    (back,) = strategy.on_sample(_sample([_real(2.5), _lint(2**53 + 1)]), NOW + 3)
    _accept(strategy, [back])
    metrics = wire.payload(back.payload).metrics
    assert [(m.name, m.value) for m in metrics] == [("Inst0.V0", 2.5), ("Inst0.V1", 2**53 + 1)]
    (zero,) = strategy.on_sample(_sample([_real(-0.0), _lint(2**53 + 1)]), NOW + 4)
    _accept(strategy, [zero])
    (positive,) = strategy.on_sample(_sample([_real(0.0), _lint(2**53 + 1)]), NOW + 5)
    assert struct.pack("<f", wire.payload(positive.payload).metrics[0].value) == bytes(4)  # type: ignore[arg-type]


# Rebirth and commands


def test_rebirth_requests_are_coalesced_and_answered_on_the_same_connection() -> None:
    mono = Mono()
    strategy = _strategy(mono=mono)
    _born(strategy)
    bd_seq = strategy.bd_seq
    (data,) = strategy.on_sample(_values(1), NOW + 100)
    _accept(strategy, [data])
    mono.now = 0.5
    for _ in range(16):
        assert strategy.on_inbound(InboundMessage(NCMD, wire.REBIRTH), NOW + 500) == ()
    mono.now = 1.9
    (early,) = strategy.on_sample(_values(2), NOW + 1900)
    assert early.topic == DATA
    _accept(strategy, [early])
    mono.now = 2.0
    (rebirth,) = strategy.on_sample(_values(3, toggle=True), NOW + 2000)
    assert rebirth.topic == BIRTH
    decoded = wire.payload(rebirth.payload)
    assert decoded.seq == 0 and decoded.metric("bdSeq").value == bd_seq
    assert decoded.metric("Inst0.V0").value == 3 and decoded.metric("Inst0.V1").value is True
    assert len(decoded.metrics) == 5
    _accept(strategy, [rebirth])
    mono.now = 5.0
    (after,) = strategy.on_sample(_values(4, toggle=True), NOW + 5000)
    assert after.topic == DATA and wire.payload(after.payload).seq == 1
    assert (strategy.births, strategy.rebirths_requested, strategy.bd_seq) == (2, 16, bd_seq)


def test_a_flood_costs_at_most_one_birth_per_interval() -> None:
    mono = Mono()
    strategy = _strategy(mono=mono)
    _born(strategy)
    births = 0
    for step in range(1, 101):
        mono.now = step * 0.1
        strategy.on_inbound(InboundMessage(NCMD, wire.REBIRTH), NOW)
        for message in strategy.on_sample(_values(step), NOW + step):
            births += message.topic == BIRTH
            _accept(strategy, [message])
    assert births == 5


@pytest.mark.parametrize(
    "message",
    [
        InboundMessage(NCMD, wire.command([("Inst0.V0", 3, "int_value", 999)])),
        InboundMessage(NCMD, wire.command([("Node Control/Rebirth", 11, "boolean_value", False)])),
        InboundMessage(NCMD, b"\xff\xfe garbage"),
        InboundMessage(NCMD, wire.REBIRTH + b"\x00" * 16_384),
        InboundMessage("spBv1.0/Plant 1/NCMD/other-node", wire.REBIRTH),
        InboundMessage("spBv1.0/Plant 1/DCMD/plc-linha1/dev", wire.REBIRTH),
    ],
    ids=["process-write", "rebirth-false", "malformed", "oversized", "other-node", "device"],
)
def test_refused_commands_change_nothing(message: InboundMessage) -> None:
    mono = Mono()
    logs: list[tuple[str, str]] = []
    strategy = _strategy(mono=mono, logs=logs)
    _born(strategy)
    before = (strategy.seq, strategy.bd_seq, strategy.births)
    assert strategy.on_inbound(message, NOW) == ()
    assert strategy.on_inbound(message, NOW) == ()
    mono.now = 10.0
    assert strategy.on_sample(_values(0), NOW + 10_000) == []
    assert (strategy.seq, strategy.bd_seq, strategy.births) == before
    assert strategy.commands_refused == 2 and strategy.rebirths_requested == 0
    assert len(logs) == 1 and logs[0][0] == "warning"
    assert logs[0][1].startswith("control_refused: ")
    assert "999" not in logs[0][1] and "garbage" not in logs[0][1]


def test_plugin_has_no_path_to_write_process_values() -> None:
    sources = [MQTT_DIR / "mqtt_plugin.py", *MQTT_DIR.glob("mqtt_*/*.py")]
    writers = re.compile(
        r"\b(debug_set|debug_write|journal_write_\w+|request_plc_\w+|set_switch_position"
        r"|(bool|byte|int|dint|lint)_(output|memory))\b"
    )
    for source in sources:
        if source.name != "sparkplug_b_pb2.py":
            assert writers.search(source.read_text(encoding="utf-8")) is None, source.name
    assert writers.search("args.debug_write(") is not None


# Disconnection and shutdown


def test_disconnect_resets_the_session_and_the_next_attempt_births_with_a_new_bdseq() -> None:
    strategy = _strategy(start=9)
    _born(strategy)
    (data,) = strategy.on_sample(_values(1), NOW + 1)
    _accept(strategy, [data])
    strategy.on_disconnected()
    will = strategy.will()
    assert will is not None and wire.payload(will.payload).metrics[0].value == 10
    (birth,) = strategy.on_connected(_values(1), NOW + 2)
    decoded = wire.payload(birth.payload)
    assert birth.topic == BIRTH and decoded.seq == 0 and decoded.metric("bdSeq").value == 10


def test_a_lost_connection_alone_forgets_birth_seq_and_baseline() -> None:
    strategy = _strategy()
    _born(strategy)
    (data,) = strategy.on_sample(_values(1), NOW + 1)
    _accept(strategy, [data])
    strategy.on_disconnected()
    assert strategy.seq == 0
    (message,) = strategy.on_sample(_values(1), NOW + 2)
    assert message.topic == BIRTH


def test_shutdown_publishes_ndeath_of_the_current_session() -> None:
    strategy = _strategy(start=77)
    assert strategy.on_shutdown(NOW) == ()
    _born(strategy)
    (death,) = strategy.on_shutdown(NOW + 5)
    assert (death.topic, death.qos, death.retain) == (DEATH, 1, False)
    decoded = wire.payload(death.payload)
    assert decoded.timestamp == NOW + 5 and decoded.seq is None
    assert [(m.name, m.value) for m in decoded.metrics] == [("bdSeq", 77)]


# Selection and status


def _replace_leaves(config: MqttRuntimeConfig, names: Sequence[str]) -> MqttRuntimeConfig:
    leaves = tuple(
        dataclasses.replace(leaf, destination=name) for leaf, name in zip(config.leaves, names)
    )
    return dataclasses.replace(config, leaves=leaves)


def test_selections_the_protocol_cannot_carry_are_refused() -> None:
    base = _config(["DINT", "DINT"])
    with pytest.raises(ValueError):
        SparkplugStrategy(decode_runtime_config(GENERIC_FIXTURE.read_bytes(), None))
    for names in (["bdSeq", "A"], ["Node Control/Rebirth", "A"], ["A.b", "A.B"], ["", "A"]):
        with pytest.raises(ValueError):
            SparkplugStrategy(_replace_leaves(base, names))
    assert base.sparkplug is not None
    bad_group = dataclasses.replace(
        base, sparkplug=dataclasses.replace(base.sparkplug, group_id="a/b")
    )
    with pytest.raises(ValueError):
        SparkplugStrategy(bad_group)
    many = dataclasses.replace(base, leaves=base.leaves * (MAX_LEAVES // 2 + 1))
    with pytest.raises(ValueError):
        SparkplugStrategy(many)
    long_names = _config(["LREAL"] * 449)
    huge = _replace_leaves(long_names, [f"{i:04d}" + "x" * 508 for i in range(449)])
    with pytest.raises(ValueError):
        SparkplugStrategy(huge)
    SparkplugStrategy(dataclasses.replace(huge, leaves=huge.leaves[:448]))


def test_sample_that_does_not_match_the_selection_fails_closed() -> None:
    strategy = _strategy()
    strategy.will()
    with pytest.raises(ValueError):
        strategy.on_connected(_sample([_dint(1)]), NOW)
    with pytest.raises(ValueError):
        strategy.on_connected(_sample([_dint(1), _dint(2), _real(1.0)]), NOW)


def test_status_reports_bdseq_seq_and_last_birth() -> None:
    clock = FakeClock()
    tracker = StatusTracker(clock)
    config = _config(["DINT", "BOOL", "REAL"])
    writer = tracker.reset("connecting", config)
    strategy = SparkplugStrategy(config, writer, bd_seq=BdSeqCounter(3))
    strategy.will()
    assert tracker.snapshot()["sparkplug"] == {"bdSeq": 3, "seq": 0, "lastBirthAt": None}
    (birth,) = strategy.on_connected(_values(0), clock.wall_ms())
    _accept(strategy, [birth])
    (data,) = strategy.on_sample(_values(1), clock.wall_ms() + 100)
    _accept(strategy, [data])
    snapshot = tracker.snapshot()
    assert snapshot["sparkplug"] == {
        "bdSeq": 3,
        "seq": 1,
        "lastBirthAt": "2026-10-09T12:00:00.000Z",
    }
    assert check(snapshot, "status") == []


# Through the supervisor


def test_lifecycle_through_the_supervisor_with_outage_rebirth_and_stop() -> None:
    clock = FakeClock()
    network = FakeNetwork(clock)
    config = _config(["DINT", "BOOL", "REAL"])
    tracker = StatusTracker(clock)
    writer = tracker.reset("connecting", config)
    strategy = SparkplugStrategy(
        config, writer, bd_seq=BdSeqCounter(200), monotonic=clock.monotonic
    )
    state = {"n": 0}
    raws = [struct.pack("<i", 0), b"\x00", struct.pack("<f", 2.5)]

    class Sampler:
        def sample(self) -> SampleResult:
            return _sample(
                [convert_debug_read(t, r) for t, r in zip(("DINT", "BOOL", "REAL"), raws)]
            )

    def scan() -> None:
        state["n"] += 1
        raws[0] = struct.pack("<i", state["n"])
        raws[1] = bytes([state["n"] // 5 % 2])
        if state["n"] < 60:
            clock.at(clock.now + 0.1, scan)

    def command(payload: bytes) -> None:
        network.opened[-1].inbound.append(InboundMessage(NCMD, payload))

    cancel = threading.Event()
    clock.at(0.05, scan)
    clock.at(0.75, lambda: setattr(network, "up", False))
    clock.at(3.4, lambda: setattr(network, "up", True))
    clock.at(5.05, lambda: command(wire.command([("Inst0.V0", 3, "int_value", 999)])))
    clock.at(5.1, lambda: command(wire.REBIRTH))
    clock.at(5.15, lambda: command(wire.REBIRTH))
    clock.at(6.5, cancel.set)
    supervisor = Supervisor(
        ConnectSpec("h", 1, "plc-linha1", None),
        network.factory,
        strategy,
        Sampler(),
        writer,
        clock,
        Backoff(lambda: 0.0),
        SupervisorSettings(tick_interval_s=0.1),
        lambda *_a: None,
    )
    supervisor.run(cancel)

    assert len(network.opened) == 2
    wills = [wire.payload(s.spec.will.payload).metrics[0].value for s in network.sessions]  # type: ignore[union-attr]
    assert wills == sorted(wills) and len(set(wills)) == len(wills) and wills[0] == 200
    for session in network.opened:
        will_bd_seq = wire.payload(session.spec.will.payload).metrics[0].value  # type: ignore[union-attr]
        assert session.subscriptions == [(NCMD, 0)]
        sent = [(m.topic, wire.payload(m.payload)) for m in session.sent]
        assert sent[0][0] == BIRTH and sent[0][1].metric("bdSeq").value == will_bd_seq
        expected = 0
        for topic, payload in sent:
            if topic == BIRTH:
                expected = 0
                assert payload.metric("bdSeq").value == will_bd_seq and len(payload.metrics) == 5
            assert payload.seq == expected
            expected = (expected + 1) % 256
    first, second = network.opened
    assert first.final == [] and first.aborted
    (death,) = second.final
    assert death.topic == DEATH and death.qos == 1
    assert (
        wire.payload(death.payload).metrics[0].value
        == wire.payload(second.spec.will.payload).metrics[0].value  # type: ignore[union-attr]
    )
    births = [m for m in second.sent if m.topic == BIRTH]
    assert len(births) == 2, "connect birth plus one coalesced rebirth"
    counters = [
        m.value
        for msg in second.sent
        for m in wire.payload(msg.payload).metrics
        if m.name == "Inst0.V0"
    ]
    assert 999 not in counters and counters == sorted(counters)
    assert strategy.commands_refused == 1 and strategy.rebirths_requested == 2
    assert tracker.snapshot()["sparkplug"]["bdSeq"] == wills[-1]  # type: ignore[index]


# Plugin and real client


def _write_config(path: Path, port: int) -> Path:
    doc = json.loads(SPARKPLUG_FIXTURE.read_text(encoding="utf-8"))
    doc["connection"].update({"host": "127.0.0.1", "port": port, "tls": {"enabled": False}})
    doc["auth"] = {"method": "anonymous"}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _until(predicate: object, seconds: float = 5.0) -> bool:
    assert callable(predicate)
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return True
        RealTime().advance(0.01)
    return bool(predicate())


def test_plugin_registers_the_sparkplug_mode(tmp_path: Path) -> None:
    assert mqtt_plugin.STRATEGY_BUILDERS["sparkplug_b"] is mqtt_plugin.build_sparkplug_strategy
    network = FakeNetwork(RealTime())
    state = mqtt_plugin.PluginState(
        publishers=mqtt_plugin.PublisherFactory(None, network.factory, log=lambda *_a: None)  # type: ignore[arg-type]
    )
    result = state.start(
        str(_write_config(tmp_path / "mqtt.json", 1883)), FakeProgram.from_debug_map()
    )
    try:
        assert not isinstance(result, mqtt_plugin.ConfigError)
        assert _until(lambda: len(network.delivered) >= 1)
        assert network.delivered[0].topic == BIRTH
        assert network.opened[0].subscriptions == [(NCMD, 0)]
    finally:
        assert state.deactivate() is True


def test_real_client_birth_data_rebirth_refusal_and_death_on_the_wire(tmp_path: Path) -> None:
    broker = FakeBroker()
    program = FakeProgram.from_debug_map()
    program.values[(0, 5)] = struct.pack("<i", 7)
    before = dict(program.values)
    state = mqtt_plugin.PluginState(publishers=mqtt_plugin.PublisherFactory(log=lambda *_a: None))
    path = _write_config(tmp_path / "mqtt.json", broker.port)

    def on(topic: str) -> list[wire.Payload]:
        return [wire.payload(p.payload) for p in broker.publishes if p.topic == topic]

    try:
        assert not isinstance(state.start(str(path), program), mqtt_plugin.ConfigError)
        assert _until(lambda: len(on(BIRTH)) == 1 and (NCMD, 0) in broker.subscriptions)
        program.values[(0, 5)] = struct.pack("<i", 8)
        before[(0, 5)] = program.values[(0, 5)]
        assert _until(lambda: len(on(DATA)) == 1)
        assert broker.inject(NCMD, wire.command([("Inst0.V_DINT", 3, "int_value", 999)])) == 1
        RealTime().advance(0.3)
        assert program.values == before and len(on(BIRTH)) == 1
        RealTime().advance(1.8)
        assert broker.inject(NCMD, wire.REBIRTH) == 1
        assert broker.inject(NCMD, wire.REBIRTH) == 1
        assert _until(lambda: len(on(BIRTH)) == 2)
        RealTime().advance(0.3)
    finally:
        assert state.deactivate() is True
        _until(lambda: broker.disconnects == 1, 2.0)
        broker.close()
    (connect,) = broker.connects
    assert (connect.will_topic, connect.will_qos, connect.will_retain) == (DEATH, 1, False)
    assert connect.clean_session and connect.will_payload is not None
    will_bd_seq = wire.payload(connect.will_payload).metrics[0].value
    sequence = [(p.topic, p.qos, p.retain) for p in broker.publishes]
    assert sequence[0] == (BIRTH, 0, False) and sequence[-1] == (DEATH, 1, False)
    assert {entry for entry in sequence[:-1]} <= {(BIRTH, 0, False), (DATA, 0, False)}
    first_birth, rebirth = on(BIRTH)
    assert len(first_birth.metrics) == 23 and len(rebirth.metrics) == 23
    assert first_birth.metric("bdSeq").value == rebirth.metric("bdSeq").value == will_bd_seq
    assert first_birth.metric("Inst0.V_DINT").value == 7
    assert rebirth.metric("Inst0.V_DINT").value == 8 and rebirth.seq == 0
    (data,) = on(DATA)
    assert data.seq == 1 and [(m.name, m.value) for m in data.metrics] == [("Inst0.V_DINT", 8)]
    (death,) = on(DEATH)
    assert death.metrics[0].value == will_bd_seq
    assert broker.disconnects == 1 and program.values == before
