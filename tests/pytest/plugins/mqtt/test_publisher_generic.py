# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Generic MQTT mode: envelope, topics, QoS/retain, periodic and on-change publication."""

from __future__ import annotations

import base64
import dataclasses
import json
import struct
import threading
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path

import mqtt_plugin
import pytest
from contract.constants import DEBUG_SIZE, MAX_LEAVES, MAX_PAYLOAD_BYTES
from contract.runtime_config import MqttRuntimeConfig, decode_runtime_config
from fake_broker import FakeBroker
from mqtt_publisher.backoff import Backoff
from mqtt_publisher.connection import ConnectSpec
from mqtt_publisher.generic import GenericStrategy, build_generic_strategy, envelope
from mqtt_publisher.status import StatusTracker
from mqtt_publisher.strategy import InboundMessage, OutboundMessage, PublishOutcome
from mqtt_publisher.supervisor import Supervisor, SupervisorSettings
from mqtt_reader.reader import LeafIssue, SampleResult
from mqtt_reader.values import LeafValue, convert_debug_read, convert_window
from program_fakes import FakeProgram
from publisher_fakes import FakeClock, FakeNetwork, RealTime

FIXTURES = (
    Path(__file__).resolve().parents[4] / "core/src/drivers/plugins/python/mqtt/contract/fixtures"
)
VECTORS = json.loads((FIXTURES / "values/value-vectors.json").read_text(encoding="utf-8"))[
    "vectors"
]
GENERIC_FIXTURE = FIXTURES / "runtime-config/valid/generic-on-change-anonymous-plaintext.json"
PERIODIC_FIXTURE = FIXTURES / "runtime-config/valid/generic-periodic-password-private-ca.json"
SPARKPLUG_FIXTURE = FIXTURES / "runtime-config/valid/sparkplug-client-certificate-all-types.json"


def _doc(
    types: Sequence[str],
    publish_mode: str = "on_change",
    interval_ms: int = 100,
    qos: int = 0,
    retain: bool = False,
) -> dict[str, object]:
    interval_key = "sample_interval_ms" if publish_mode == "on_change" else "publish_interval_ms"
    return {
        "contract": "openplc-mqtt-runtime-config",
        "format_version": 1,
        "enabled": True,
        "mode": "generic",
        "program": {"md5": "3f2c9d0e8b7a6f5e4d3c2b1a09f8e7d6"},
        "connection": {
            "host": "127.0.0.1",
            "port": 1883,
            "client_id": "plc-test",
            "tls": {"enabled": False},
        },
        "auth": {"method": "anonymous"},
        "generic": {
            "publish_mode": publish_mode,
            interval_key: interval_ms,
            "qos": qos,
            "retain": retain,
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
                "destination": f"openplc/plc-test/Inst0/V{index}",
            }
            for index, iec_type in enumerate(types)
        ],
    }


def _config(types: Sequence[str], **settings: object) -> MqttRuntimeConfig:
    raw = json.dumps(_doc(types, **settings)).encode()  # type: ignore[arg-type]
    return decode_runtime_config(raw, None)


def _sample(values: Sequence[LeafValue]) -> SampleResult:
    issues = tuple(
        LeafIssue(index, value.issue) for index, value in enumerate(values) if value.issue
    )
    return SampleResult(tuple(values), issues)


def _dint(value: int) -> LeafValue:
    return convert_window("DINT", struct.pack("<i", value))


def _bool(flag: bool) -> LeafValue:
    return convert_window("BOOL", bytes([int(flag)]))


def _lint(value: int) -> LeafValue:
    return convert_window("LINT", struct.pack("<q", value))


def _real(value: float) -> LeafValue:
    return convert_window("REAL", struct.pack("<f", value))


def _accept_all(strategy: GenericStrategy, messages: Sequence[OutboundMessage]) -> None:
    strategy.after_publish(PublishOutcome(tuple(messages), ()))


def _topics(messages: Sequence[OutboundMessage]) -> list[str]:
    return [message.topic for message in messages]


# Envelope and encodings


@pytest.mark.parametrize("vector", VECTORS, ids=[v["id"] for v in VECTORS])
def test_envelope_reproduces_every_contract_vector(vector: dict[str, object]) -> None:
    value = convert_window(str(vector["iecType"]), bytes.fromhex(str(vector["raw"])))
    assert envelope(value) == str(vector["generic"]).encode("utf-8")


def test_every_vector_through_the_strategy_keeps_order_topic_and_exact_bytes() -> None:
    strategy = GenericStrategy(_config([str(v["iecType"]) for v in VECTORS]))
    values = [convert_window(str(v["iecType"]), bytes.fromhex(str(v["raw"]))) for v in VECTORS]
    messages = strategy.on_connected(_sample(values), 0)
    assert [m.payload for m in messages] == [str(v["generic"]).encode() for v in VECTORS]
    assert _topics(messages) == [f"openplc/plc-test/Inst0/V{i}" for i in range(len(VECTORS))]
    assert [m.key for m in messages] == list(range(len(VECTORS)))


def test_payloads_parse_back_to_exact_values() -> None:
    big = 2**53 + 1
    assert json.loads(envelope(_lint(big))) == {"value": str(big)}
    assert json.loads(envelope(_lint(-(2**63)))) == {"value": str(-(2**63))}
    ulint = convert_window("ULINT", struct.pack("<Q", 2**64 - 1))
    assert json.loads(envelope(ulint)) == {"value": "18446744073709551615"}
    real = _real(0.1)
    (parsed,) = struct.unpack("<f", struct.pack("<f", json.loads(envelope(real))["value"]))
    assert struct.pack("<f", parsed) == struct.pack("<f", 0.1)
    units = "a\U0001f600".encode("utf-16-le") + b"\x00\xd8"
    wstring = convert_window("WSTRING", bytes([len(units) // 2]) + units.ljust(252, b"\x00"))
    assert base64.b64decode(json.loads(envelope(wstring))["value"]) == units
    nan = _real(float("nan"))
    assert envelope(nan) == b'{"value":null}' and nan.issue is not None


def test_worst_case_string_payloads_stay_far_below_the_payload_limit() -> None:
    control = convert_debug_read("STRING", bytes([125]) + b"\x01" * 125 + b"\x00")
    wide = convert_debug_read("WSTRING", bytes([125]) + b"\x01\x00" * 125 + b"\x00\x00")
    for value in (control, wide):
        payload = envelope(value)
        assert value.issue is None and len(payload) < 1024 < MAX_PAYLOAD_BYTES
        assert set(json.loads(payload)) == {"value"}
    assert json.loads(envelope(control))["value"] == "\x01" * 125


def test_string_at_the_read_window_is_null_never_truncated() -> None:
    full = convert_debug_read("STRING", bytes([126]) + b"x" * 126)
    assert envelope(full) == b'{"value":null}'
    assert full.issue is not None and full.issue.code == "string_exceeds_read_window"


# Topics, QoS and retain


@pytest.mark.parametrize(("qos", "retain"), [(0, False), (0, True), (1, False), (1, True)])
@pytest.mark.parametrize("publish_mode", ["on_change", "periodic"])
def test_qos_and_retain_apply_to_announcement_updates_and_recovery(
    qos: int, retain: bool, publish_mode: str
) -> None:
    strategy = GenericStrategy(
        _config(["DINT", "BOOL"], publish_mode=publish_mode, qos=qos, retain=retain)
    )
    announce = strategy.on_connected(_sample([_dint(1), _bool(False)]), 0)
    _accept_all(strategy, announce)
    update = strategy.on_sample(_sample([_dint(2), _bool(False)]), 0)
    _accept_all(strategy, update)
    strategy.on_disconnected()
    recovery = strategy.on_connected(_sample([_dint(3), _bool(True)]), 0)
    every = [*announce, *update, *recovery]
    assert every and all((m.qos, m.retain) == (qos, retain) for m in every)
    assert len(recovery) == 2


def test_fixture_topics_are_the_configured_destinations() -> None:
    config = decode_runtime_config(GENERIC_FIXTURE.read_bytes(), None)
    strategy = build_generic_strategy(config, StatusTracker(FakeClock()).reset("connecting", None))
    values = [convert_window(leaf.iec_type, bytes(leaf.size)) for leaf in config.leaves]
    messages = strategy.on_connected(_sample(values), 0)
    assert _topics(messages) == [leaf.destination for leaf in config.leaves]
    assert {(m.qos, m.retain) for m in messages} == {(0, False)}
    assert strategy.tick_interval_ms == 100


def test_tick_interval_follows_the_publication_mode() -> None:
    assert GenericStrategy(_config(["DINT"], interval_ms=250)).tick_interval_ms == 250
    periodic = decode_runtime_config(PERIODIC_FIXTURE.read_bytes(), None)
    strategy = GenericStrategy(periodic)
    assert strategy.tick_interval_ms == 1000
    messages = strategy.on_connected(
        _sample([convert_window(leaf.iec_type, bytes(leaf.size)) for leaf in periodic.leaves]), 0
    )
    assert {(m.qos, m.retain) for m in messages} == {(1, True)}


def test_generic_mode_has_no_will_subscription_control_or_shutdown_message() -> None:
    strategy = GenericStrategy(_config(["DINT"]))
    assert strategy.will() is None
    assert list(strategy.subscriptions()) == []
    assert list(strategy.on_inbound(InboundMessage("openplc/plc-test/x", b"1"), 0)) == []
    assert list(strategy.on_shutdown(0)) == []


# Periodic and on-change


def test_announcement_publishes_every_leaf_including_null() -> None:
    strategy = GenericStrategy(_config(["DINT", "REAL", "BOOL"]))
    messages = strategy.on_connected(_sample([_dint(7), _real(float("inf")), _bool(True)]), 0)
    assert [m.payload for m in messages] == [b'{"value":7}', b'{"value":null}', b'{"value":true}']


def test_periodic_republishes_stable_values_on_every_tick() -> None:
    strategy = GenericStrategy(_config(["DINT", "BOOL"], publish_mode="periodic", interval_ms=1000))
    sample = _sample([_dint(5), _bool(True)])
    _accept_all(strategy, strategy.on_connected(sample, 0))
    for _ in range(3):
        messages = strategy.on_sample(sample, 0)
        assert [m.payload for m in messages] == [b'{"value":5}', b'{"value":true}']
        _accept_all(strategy, messages)


def test_on_change_publishes_only_observed_changes() -> None:
    strategy = GenericStrategy(_config(["DINT", "BOOL", "INT"]))
    stable = convert_window("INT", struct.pack("<h", -2))
    _accept_all(strategy, strategy.on_connected(_sample([_dint(0), _bool(False), stable]), 0))
    seen: list[tuple[str, bytes]] = []
    for scan in range(1, 6):
        messages = strategy.on_sample(_sample([_dint(scan), _bool(scan % 2 == 1), stable]), 0)
        _accept_all(strategy, messages)
        seen += [(m.topic.rsplit("/", 1)[1], m.payload) for m in messages]
    counter = [p for t, p in seen if t == "V0"]
    toggle = [p for t, p in seen if t == "V1"]
    assert counter == [f'{{"value":{n}}}'.encode() for n in range(1, 6)]
    assert toggle == [b'{"value":true}', b'{"value":false}'] * 2 + [b'{"value":true}']
    assert not [t for t, _ in seen if t == "V2"]
    assert strategy.on_sample(_sample([_dint(5), _bool(True), stable]), 0) == []


def test_on_change_compares_without_loss_or_deadband() -> None:
    strategy = GenericStrategy(_config(["LINT", "REAL", "LREAL"]))
    base = [
        _lint(2**53),
        _real(0.0),
        convert_window("LREAL", struct.pack("<d", 1.0)),
    ]
    _accept_all(strategy, strategy.on_connected(_sample(base), 0))
    changed = strategy.on_sample(
        _sample(
            [
                _lint(2**53 + 1),
                _real(-0.0),
                convert_window("LREAL", struct.pack("<d", 1.0000000000000002)),
            ]
        ),
        0,
    )
    assert [m.payload for m in changed] == [
        b'{"value":"9007199254740993"}',
        b'{"value":-0.0}',
        b'{"value":1.0000000000000002}',
    ]


def test_null_and_recovery_are_changes_and_repeated_null_is_not() -> None:
    strategy = GenericStrategy(_config(["REAL"]))
    _accept_all(strategy, strategy.on_connected(_sample([_real(1.5)]), 0))
    went_null = strategy.on_sample(_sample([_real(float("nan"))]), 0)
    _accept_all(strategy, went_null)
    still_null = strategy.on_sample(_sample([_real(float("-inf"))]), 0)
    recovered = strategy.on_sample(_sample([_real(1.5)]), 0)
    assert [m.payload for m in went_null] == [b'{"value":null}']
    assert list(still_null) == []
    assert [m.payload for m in recovered] == [b'{"value":1.5}']


def test_dropped_message_keeps_the_baseline_and_is_retried() -> None:
    strategy = GenericStrategy(_config(["DINT", "DINT"]))
    _accept_all(strategy, strategy.on_connected(_sample([_dint(1), _dint(1)]), 0))
    first = strategy.on_sample(_sample([_dint(2), _dint(2)]), 0)
    strategy.after_publish(PublishOutcome((first[0],), (first[1],)))
    retried = strategy.on_sample(_sample([_dint(2), _dint(2)]), 0)
    assert [(m.key, m.payload) for m in retried] == [(1, b'{"value":2}')]
    _accept_all(strategy, retried)
    assert strategy.on_sample(_sample([_dint(2), _dint(2)]), 0) == []


def test_dropped_change_that_reverts_is_not_republished() -> None:
    strategy = GenericStrategy(_config(["DINT"]))
    _accept_all(strategy, strategy.on_connected(_sample([_dint(1)]), 0))
    lost = strategy.on_sample(_sample([_dint(2)]), 0)
    strategy.after_publish(PublishOutcome((), tuple(lost)))
    assert strategy.on_sample(_sample([_dint(1)]), 0) == []


def test_disconnect_clears_the_baseline_so_the_next_session_announces_current_state() -> None:
    strategy = GenericStrategy(_config(["DINT"]))
    _accept_all(strategy, strategy.on_connected(_sample([_dint(1)]), 0))
    strategy.on_disconnected()
    assert strategy.baseline == (None,)
    announce = strategy.on_connected(_sample([_dint(9)]), 0)
    assert [m.payload for m in announce] == [b'{"value":9}']
    _accept_all(strategy, announce)
    assert strategy.on_sample(_sample([_dint(9)]), 0) == []


def test_unknown_message_keys_do_not_move_the_baseline() -> None:
    strategy = GenericStrategy(_config(["DINT"]))
    strategy.on_connected(_sample([_dint(1)]), 0)
    strategy.after_publish(PublishOutcome((OutboundMessage("other", b"x", 0, False, None),), ()))
    assert strategy.baseline == (None,)


# Selection validation


def test_non_generic_configuration_is_refused() -> None:
    sparkplug = decode_runtime_config(SPARKPLUG_FIXTURE.read_bytes(), None)
    with pytest.raises(ValueError):
        GenericStrategy(sparkplug)


@pytest.mark.parametrize("defect", ["duplicate", "invalid", "too_many", "empty"])
def test_selection_that_bypassed_the_decoder_is_refused(defect: str) -> None:
    config = _config(["DINT", "DINT"])
    leaves = list(config.leaves)
    if defect == "duplicate":
        leaves[1] = dataclasses.replace(leaves[1], destination=leaves[0].destination)
    elif defect == "invalid":
        leaves[1] = dataclasses.replace(leaves[1], destination="openplc/+/x")
    elif defect == "too_many":
        leaves = [
            dataclasses.replace(leaves[0], destination=f"t/{i}", elem=i)
            for i in range(MAX_LEAVES + 1)
        ]
    else:
        leaves = []
    with pytest.raises(ValueError):
        GenericStrategy(dataclasses.replace(config, leaves=tuple(leaves)))


def test_thousand_leaves_are_accepted() -> None:
    strategy = GenericStrategy(_config(["DINT"] * MAX_LEAVES))
    messages = strategy.on_connected(_sample([_dint(i) for i in range(MAX_LEAVES)]), 0)
    assert len(messages) == MAX_LEAVES and len({m.topic for m in messages}) == MAX_LEAVES


@pytest.mark.parametrize("defect", ["short", "type"])
def test_sample_that_does_not_match_the_selection_is_an_error(defect: str) -> None:
    strategy = GenericStrategy(_config(["DINT", "BOOL"]))
    values = [_dint(1)] if defect == "short" else [_dint(1), _dint(0)]
    with pytest.raises(ValueError):
        strategy.on_connected(_sample(values), 0)


# Through the supervisor and the plugin


class ScriptedSampler:
    """Leaves whose raw debug bytes a test sets between ticks."""

    def __init__(self, types: Sequence[str], raws: list[bytes]) -> None:
        self.types = list(types)
        self.raws = raws

    def sample(self) -> SampleResult:
        return _sample(
            [convert_debug_read(t, raw) for t, raw in zip(self.types, self.raws, strict=True)]
        )


def _supervisor(
    config: MqttRuntimeConfig, sampler: ScriptedSampler, clock: FakeClock, network: FakeNetwork
) -> tuple[Supervisor, StatusTracker]:
    tracker = StatusTracker(clock)
    writer = tracker.reset("connecting", config)
    strategy = build_generic_strategy(config, writer)
    supervisor = Supervisor(
        ConnectSpec("h", 1, "c", None),
        network.factory,
        strategy,
        sampler,
        writer,
        clock,
        Backoff(lambda: 0.0),
        SupervisorSettings(tick_interval_s=strategy.tick_interval_ms / 1000),
        lambda *_a: None,
    )
    return supervisor, tracker


def test_counter_and_toggle_through_the_supervisor_with_outage_and_recovery() -> None:
    clock = FakeClock()
    network = FakeNetwork(clock)
    config = _config(["DINT", "BOOL", "REAL"])
    raws = [struct.pack("<i", 0), b"\x00", struct.pack("<f", 2.5)]
    sampler = ScriptedSampler(["DINT", "BOOL", "REAL"], raws)
    supervisor, tracker = _supervisor(config, sampler, clock, network)
    cancel = threading.Event()
    state = {"n": 0}

    def scan() -> None:
        state["n"] += 1
        raws[0] = struct.pack("<i", state["n"])
        if state["n"] % 3 == 0:
            raws[1] = bytes([raws[1][0] ^ 1])
        raws[2] = struct.pack("<f", float("nan") if 4 <= state["n"] < 6 else 2.5)
        if state["n"] < 12:
            clock.at(clock.now + 0.1, scan)

    clock.at(0.05, scan)
    clock.at(0.75, lambda: setattr(network, "up", False))
    clock.at(1.6, lambda: setattr(network, "up", True))
    clock.at(3.0, cancel.set)
    supervisor.run(cancel)

    delivered = [(m.topic.rsplit("/", 1)[1], m.payload) for m in network.delivered]
    counter = [int(json.loads(p)["value"]) for t, p in delivered if t == "V0"]
    real = [p for t, p in delivered if t == "V2"]
    toggle = [p for t, p in delivered if t == "V1"]
    assert len(network.opened) == 2
    assert counter == sorted(counter) and len(counter) == len(set(counter))
    assert counter[-1] == 12
    assert real[:3] == [b'{"value":2.5}', b'{"value":null}', b'{"value":2.5}']
    assert len(toggle) >= 4
    for session in network.opened:
        flips = [m.payload for m in session.sent if m.topic.endswith("/V1")]
        assert all(a != b for a, b in pairwise(flips))
    second = network.opened[1].sent
    assert {m.topic.rsplit("/", 1)[1] for m in second[:3]} == {"V0", "V1", "V2"}
    assert tracker.snapshot()["stats"]["published"] == len(network.delivered)  # type: ignore[index]


def _write_config(path: Path, source: Path, port: int, **generic: object) -> Path:
    doc = json.loads(source.read_text(encoding="utf-8"))
    doc["connection"].update({"host": "127.0.0.1", "port": port, "tls": {"enabled": False}})
    doc["auth"] = {"method": "anonymous"}
    doc["generic"].update(generic)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_plugin_registers_the_generic_mode_and_publishes_through_it(tmp_path: Path) -> None:
    assert mqtt_plugin.STRATEGY_BUILDERS["generic"] is build_generic_strategy
    network = FakeNetwork(RealTime())
    state = mqtt_plugin.PluginState(
        publishers=mqtt_plugin.PublisherFactory(None, network.factory, log=lambda *_a: None)  # type: ignore[arg-type]
    )
    path = _write_config(tmp_path / "mqtt.json", GENERIC_FIXTURE, 1883)
    result = state.start(str(path), FakeProgram.from_debug_map())
    try:
        assert not isinstance(result, mqtt_plugin.ConfigError)
        config = result.config
        for _ in range(200):
            if len(network.delivered) >= len(config.leaves):
                break
            RealTime().advance(0.01)
        topics = [m.topic for m in network.delivered[: len(config.leaves)]]
        assert topics == [leaf.destination for leaf in config.leaves]
    finally:
        assert state.deactivate() is True


def test_builder_refusal_fails_closed_without_connecting(tmp_path: Path) -> None:
    network = FakeNetwork(RealTime())

    def refusing(_config: MqttRuntimeConfig, _writer: object) -> GenericStrategy:
        raise ValueError("selection refused")

    state = mqtt_plugin.PluginState(
        publishers=mqtt_plugin.PublisherFactory({"generic": refusing}, network.factory)  # type: ignore[dict-item,arg-type]
    )
    path = _write_config(tmp_path / "mqtt.json", GENERIC_FIXTURE, 1883)
    result = state.start(str(path), FakeProgram.from_debug_map())
    assert isinstance(result, mqtt_plugin.ConfigError) and result.code == "internal_error"
    assert network.sessions == [] and state.publisher is None
    assert state.snapshot()["state"] == "config_error"


# Real client against an in-process broker that parses the wire independently


def _until(predicate: object, seconds: float = 5.0) -> bool:
    assert callable(predicate)
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return True
        RealTime().advance(0.01)
    return bool(predicate())


def _fixture_program() -> FakeProgram:
    program = FakeProgram.from_debug_map()
    program.values.update(
        {
            (0, 21): struct.pack("<f", 1.5),
            (0, 23): struct.pack("<f", 2.5),
            (0, 24): struct.pack("<h", -2),
            (0, 26): struct.pack("<h", 0),
            (0, 27): struct.pack("<q", -5_000_000_001),
            (0, 7): struct.pack("<q", 2**53),
        }
    )
    return program


FIXTURE_ANNOUNCE = [
    ("openplc/plc-linha1/Inst0/Motor1/Speed", b'{"value":1.5}'),
    ("openplc/plc-linha1/Inst0/Motor2/Speed", b'{"value":2.5}'),
    ("openplc/plc-linha1/Inst0/Temps/-2", b'{"value":-2}'),
    ("openplc/plc-linha1/Inst0/Temps/0", b'{"value":0}'),
    ("openplc/plc-linha1/Inst0/Timer1/ET", b'{"value":"-5000000001"}'),
    ("openplc/plc-linha1/Inst0/V_LINT", b'{"value":"9007199254740992"}'),
]


def test_real_client_on_change_qos0_reaches_the_broker_exactly(tmp_path: Path) -> None:
    assert mqtt_plugin.TRANSPORT_FACTORY is not None
    broker = FakeBroker()
    program = _fixture_program()
    state = mqtt_plugin.PluginState(publishers=mqtt_plugin.PublisherFactory(log=lambda *_a: None))
    path = _write_config(tmp_path / "mqtt.json", GENERIC_FIXTURE, broker.port)
    try:
        assert not isinstance(state.start(str(path), program), mqtt_plugin.ConfigError)
        assert _until(lambda: len(broker.publishes) >= 6)
        RealTime().advance(0.35)
        assert len(broker.publishes) == 6
        program.values[(0, 7)] = struct.pack("<q", 2**53 + 1)
        program.values[(0, 21)] = struct.pack("<f", float("nan"))
        assert _until(lambda: len(broker.publishes) >= 8)
        program.values[(0, 21)] = struct.pack("<f", 1.5)
        assert _until(lambda: len(broker.publishes) >= 9)
        RealTime().advance(0.35)
    finally:
        assert state.deactivate() is True
        broker.close()
    wire = [(p.topic, p.payload) for p in broker.publishes]
    assert wire[:6] == FIXTURE_ANNOUNCE
    assert sorted(wire[6:8]) == sorted(
        [
            ("openplc/plc-linha1/Inst0/Motor1/Speed", b'{"value":null}'),
            ("openplc/plc-linha1/Inst0/V_LINT", b'{"value":"9007199254740993"}'),
        ]
    )
    assert wire[8:] == [("openplc/plc-linha1/Inst0/Motor1/Speed", b'{"value":1.5}')]
    assert {(p.qos, p.retain) for p in broker.publishes} == {(0, False)}
    assert broker.disconnects == 1


def test_real_client_periodic_qos1_retain_reaches_the_broker_exactly(tmp_path: Path) -> None:
    broker = FakeBroker()
    state = mqtt_plugin.PluginState(publishers=mqtt_plugin.PublisherFactory(log=lambda *_a: None))
    path = _write_config(
        tmp_path / "mqtt.json", PERIODIC_FIXTURE, broker.port, publish_interval_ms=100
    )
    try:
        assert not isinstance(state.start(str(path), _fixture_program()), mqtt_plugin.ConfigError)
        assert _until(lambda: len(broker.publishes) >= 24)
        stats = state.snapshot()["stats"]
        received = len(broker.publishes)
    finally:
        assert state.deactivate() is True
        broker.close()
    rounds = [broker.publishes[i : i + 6] for i in range(0, 24, 6)]
    for batch in rounds:
        assert [(p.topic, p.payload) for p in batch] == FIXTURE_ANNOUNCE
    assert {(p.qos, p.retain) for p in broker.publishes} == {(1, True)}
    assert all(p.packet_id is not None for p in broker.publishes)
    # forwarded QoS 1 means PUBACK seen, so it never exceeds what the broker had received
    assert 18 <= stats["published"] <= received  # type: ignore[index,operator]
