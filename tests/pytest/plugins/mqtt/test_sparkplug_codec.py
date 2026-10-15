# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Sparkplug B codec: metric wire per contract vector, NBIRTH/NDATA/NDEATH shape and NCMD parsing."""

from __future__ import annotations

import hashlib
import json
import random
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import sparkplug_wire as wire
from contract.constants import DEBUG_SIZE, MAX_PAYLOAD_BYTES
from contract.runtime_config import (
    SPARKPLUG_DATATYPE,
    Leaf,
    birth_properties,
    sparkplug_birth_upper_bound,
)
from mqtt_reader.values import LeafValue, convert_debug_read, convert_window, null_value
from mqtt_sparkplug import codec
from mqtt_sparkplug.codec import (
    MAX_CONTROL_BYTES,
    encode_birth,
    encode_data,
    encode_death,
    leaf_metric,
    node_topic,
    parse_command,
)

MQTT_DIR = Path(__file__).resolve().parents[4] / "core/src/drivers/plugins/python/mqtt"
FIXTURES = MQTT_DIR / "contract/fixtures"
VECTORS = json.loads((FIXTURES / "values/value-vectors.json").read_text(encoding="utf-8"))[
    "vectors"
]
GENCODE = MQTT_DIR / "mqtt_sparkplug/sparkplug_b_pb2.py"
GENCODE_SHA256 = "8be718dc5e5ade5efc9252f14cdb107c0e5b12729e45274a40535595a5a1318c"
NOW_MS = 1_791_547_200_123


def _vector_value(vector: dict[str, object]) -> LeafValue:
    return convert_window(str(vector["iecType"]), bytes.fromhex(str(vector["raw"])))


def _leaf(name: str, iec_type: str, elem: int = 0) -> Leaf:
    return Leaf((), f"P.{name}", 0, elem, iec_type, DEBUG_SIZE[iec_type], name)


# Metric wire against the contract vectors


@pytest.mark.parametrize("vector", VECTORS, ids=[v["id"] for v in VECTORS])
def test_metric_wire_equals_every_contract_vector(vector: dict[str, object]) -> None:
    spec = vector["sparkplug"]
    assert isinstance(spec, dict)
    value = _vector_value(vector)
    assert leaf_metric("v", value, birth=True).hex() == spec["birthMetric"]
    assert leaf_metric("v", value, birth=False).hex() == spec["dataMetric"]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["id"] for v in VECTORS])
def test_metric_decodes_to_the_exact_value_with_an_independent_reader(
    vector: dict[str, object],
) -> None:
    iec = str(vector["iecType"])
    raw = bytes.fromhex(str(vector["raw"]))
    value = _vector_value(vector)
    decoded = wire.metric(leaf_metric("v", value, birth=True, timestamp_ms=NOW_MS))
    assert decoded.name == "v" and decoded.timestamp == NOW_MS
    assert decoded.datatype == SPARKPLUG_DATATYPE[iec]
    assert decoded.properties == dict(birth_properties(iec))
    if value.is_null:
        assert decoded.is_null and decoded.value_field is None
        return
    assert not decoded.is_null
    if iec in ("SINT", "INT", "DINT"):
        expected = struct.unpack({"SINT": "<b", "INT": "<h", "DINT": "<i"}[iec], raw)[0]
        assert decoded.value_field == "int_value"
        assert decoded.value == expected & 0xFFFF_FFFF
    elif iec in ("LINT", "TIME", "TOD", "DATE", "DT"):
        assert decoded.value_field == "long_value"
        assert isinstance(decoded.value, int)
        scale = 86_400_000_000_000 if iec == "DATE" else 1
        assert wire.signed64(decoded.value) == struct.unpack("<q", raw)[0] * scale
    elif iec in ("ULINT", "LWORD"):
        assert decoded.value_field == "long_value" and decoded.value == struct.unpack("<Q", raw)[0]
    elif iec == "REAL":
        assert decoded.value_field == "float_value"
        assert struct.pack("<f", decoded.value) == raw  # type: ignore[arg-type]
    elif iec == "LREAL":
        assert decoded.value_field == "double_value"
        assert struct.pack("<d", decoded.value) == raw  # type: ignore[arg-type]
    elif iec == "BOOL":
        assert decoded.value_field == "boolean_value" and decoded.value is (raw[0] == 1)
    elif iec == "STRING":
        assert decoded.value_field == "string_value"
        assert decoded.value == raw[1 : 1 + raw[0]].decode("utf-8")
    elif iec == "WSTRING":
        assert decoded.value_field == "bytes_value" and decoded.value == raw[1 : 1 + 2 * raw[0]]
    else:
        assert decoded.value_field == "int_value"
        assert decoded.value == int.from_bytes(raw, "little")


def test_value_of_the_wrong_kind_is_refused_instead_of_coerced() -> None:
    wrong = LeafValue("DINT", "7", "7", b"\x07\x00\x00\x00", None)
    with pytest.raises(ValueError):
        leaf_metric("v", wrong, birth=False)
    flag = LeafValue("DINT", True, "1", b"\x01\x00\x00\x00", None)
    with pytest.raises(ValueError):
        leaf_metric("v", flag, birth=False)


# Payloads


def test_birth_carries_seq_zero_bdseq_rebirth_control_and_every_leaf_in_order() -> None:
    values = [_vector_value(v) for v in VECTORS]
    names = [f"Inst0.{v['id']}" for v in VECTORS]
    decoded = wire.payload(encode_birth(7, names, values, NOW_MS))
    assert decoded.timestamp == NOW_MS and decoded.seq == 0
    assert [m.name for m in decoded.metrics] == ["bdSeq", "Node Control/Rebirth", *names]
    bd_seq = decoded.metrics[0]
    assert (bd_seq.datatype, bd_seq.value_field, bd_seq.value) == (4, "long_value", 7)
    rebirth = decoded.metrics[1]
    assert (rebirth.datatype, rebirth.value_field, rebirth.value) == (11, "boolean_value", False)
    for metric, vector, value in zip(decoded.metrics[2:], VECTORS, values):
        assert metric.timestamp == NOW_MS
        assert metric.properties == dict(birth_properties(str(vector["iecType"])))
        assert metric.is_null == value.is_null
    assert all(1 not in m.field_numbers or m.name for m in decoded.metrics)
    assert all(2 not in m.field_numbers for m in decoded.metrics), "no aliases"


def test_data_carries_seq_and_only_the_given_metrics_without_properties() -> None:
    lint = convert_window("LINT", struct.pack("<q", 2**53 + 1))
    null = null_value("REAL", "conversion_failed", "float_not_finite")
    decoded = wire.payload(encode_data(255, [("A.B", lint), ("A.C", null)], NOW_MS))
    assert decoded.seq == 255 and decoded.timestamp == NOW_MS
    assert [(m.name, m.datatype) for m in decoded.metrics] == [("A.B", 4), ("A.C", 9)]
    assert decoded.metrics[0].value == 2**53 + 1 and decoded.metrics[0].properties == {}
    assert decoded.metrics[1].is_null and decoded.metrics[1].value_field is None
    with pytest.raises(ValueError):
        encode_data(256, [("A.B", lint)], NOW_MS)
    with pytest.raises(ValueError):
        encode_data(1, [], NOW_MS)


def test_death_holds_only_the_bdseq_and_no_seq() -> None:
    will = wire.payload(encode_death(255, None))
    assert will.timestamp is None and will.seq is None
    assert [(m.name, m.datatype, m.value) for m in will.metrics] == [("bdSeq", 4, 255)]
    final = wire.payload(encode_death(0, NOW_MS))
    assert final.timestamp == NOW_MS and final.seq is None and final.metrics[0].value == 0
    with pytest.raises(ValueError):
        encode_death(256, None)


def test_topics_follow_the_sparkplug_namespace() -> None:
    assert node_topic("Plant 1", "NBIRTH", "plc-linha1") == "spBv1.0/Plant 1/NBIRTH/plc-linha1"
    assert node_topic("G", "NCMD", "E") == "spBv1.0/G/NCMD/E"


def test_worst_case_birth_stays_within_the_upper_bound_the_producers_use() -> None:
    worst = {
        "BOOL": b"\x01",
        "SINT": struct.pack("<b", -1),
        "INT": struct.pack("<h", -1),
        "DINT": struct.pack("<i", -1),
        "LINT": struct.pack("<q", -1),
        "USINT": b"\xff",
        "UINT": b"\xff\xff",
        "UDINT": b"\xff" * 4,
        "ULINT": b"\xff" * 8,
        "BYTE": b"\xff",
        "WORD": b"\xff" * 2,
        "DWORD": b"\xff" * 4,
        "LWORD": b"\xff" * 8,
        "REAL": struct.pack("<f", 1.5),
        "LREAL": struct.pack("<d", 1.5),
        "TIME": struct.pack("<q", -1),
        "TOD": struct.pack("<q", 86_399_999_999_999),
        "DATE": struct.pack("<q", -1),
        "DT": struct.pack("<q", -1),
        "STRING": bytes([125]) + "é".encode() * 62 + b"x" + b"\x00",
        "WSTRING": bytes([125]) + b"\xff\xff" * 125 + b"\x00\x00",
    }
    leaves = [_leaf(f"Inst0.V_{t}", t, i) for i, t in enumerate(worst)]
    values = [convert_debug_read(t, raw) for t, raw in worst.items()]
    assert not [v for v in values if v.is_null]
    size = len(encode_birth(255, [leaf.destination for leaf in leaves], values, 2**64 - 1))
    assert size <= sparkplug_birth_upper_bound(leaves)


def test_birth_at_the_payload_limit_is_complete_and_fits() -> None:
    def name(i: int) -> str:
        return f"{i:04d}" + "x" * 508

    leaves = [_leaf(name(i), "LREAL", i) for i in range(448)]
    assert sparkplug_birth_upper_bound(leaves) <= MAX_PAYLOAD_BYTES
    value = convert_window("LREAL", struct.pack("<d", 0.1))
    birth = encode_birth(1, [leaf.destination for leaf in leaves], [value] * 448, NOW_MS)
    assert len(birth) <= MAX_PAYLOAD_BYTES
    assert len(wire.payload(birth).metrics) == 450
    assert (
        sparkplug_birth_upper_bound([*leaves, _leaf(name(448), "LREAL", 448)]) > MAX_PAYLOAD_BYTES
    )


def test_pure_python_protobuf_produces_the_same_bytes() -> None:
    values = [_vector_value(v) for v in VECTORS]
    names = [str(v["id"]) for v in VECTORS]
    expected = encode_birth(3, names, values, NOW_MS).hex()
    probe = textwrap.dedent("""
        import json, sys
        sys.path[:0] = [sys.argv[1], sys.argv[2]]
        from google.protobuf.internal import api_implementation
        from mqtt_reader.values import convert_window
        from mqtt_sparkplug.codec import encode_birth
        vectors = json.loads(open(sys.argv[3], encoding="utf-8").read())["vectors"]
        values = [convert_window(v["iecType"], bytes.fromhex(v["raw"])) for v in vectors]
        names = [v["id"] for v in vectors]
        print(api_implementation.Type(), encode_birth(3, names, values, int(sys.argv[4])).hex())
        """)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            probe,
            str(MQTT_DIR),
            str(MQTT_DIR.parent),
            str(FIXTURES / "values/value-vectors.json"),
            str(NOW_MS),
        ],
        capture_output=True,
        text=True,
        check=True,
        env={"PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python"},
    )
    kind, encoded = result.stdout.split()
    assert kind == "python" and encoded == expected


# Generated code and runtime pin


def test_generated_code_is_the_recorded_protoc_output() -> None:
    data = GENCODE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == GENCODE_SHA256
    assert b"# source: sparkplug_b.proto\n# Protobuf Python Version: 7.36.2\n" in data
    assert codec.PINNED_PROTOBUF == "7.36.2"


def test_another_protobuf_runtime_fails_the_import(tmp_path: Path) -> None:
    fake = tmp_path / "google" / "protobuf"
    fake.mkdir(parents=True)
    (tmp_path / "google" / "__init__.py").write_text("", encoding="utf-8")
    (fake / "__init__.py").write_text('__version__ = "7.36.1"\n', encoding="utf-8")
    probe = (
        "import sys; sys.path[:0] = [sys.argv[1], sys.argv[2], sys.argv[3]]\n"
        "try:\n    import mqtt_sparkplug.codec\nexcept ImportError as exc:\n    print('refused', exc)\n"
        "import mqtt_plugin; print(sorted(mqtt_plugin.STRATEGY_BUILDERS))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(tmp_path), str(MQTT_DIR), str(MQTT_DIR.parent)],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    assert lines[0] == "refused protobuf 7.36.2 is required, found 7.36.1"
    assert lines[1] == "['generic']"


def test_plugin_without_the_codec_dependencies_offers_only_the_generic_mode() -> None:
    probe = (
        "import sys, importlib.abc\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'google':\n"
        "            raise ImportError(name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "sys.path[:0] = [sys.argv[1], sys.argv[2]]\n"
        "import mqtt_plugin\n"
        "print(sorted(mqtt_plugin.STRATEGY_BUILDERS), mqtt_plugin.TRANSPORT_FACTORY is not None)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(MQTT_DIR), str(MQTT_DIR.parent)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "['generic'] True"


# NCMD


def test_only_a_true_rebirth_metric_is_accepted() -> None:
    assert parse_command(wire.REBIRTH) is None
    untyped = wire.command([("Node Control/Rebirth", None, "boolean_value", True)])
    assert parse_command(untyped) is None
    with_seq = wire.command([("Node Control/Rebirth", 11, "boolean_value", True)], seq=9)
    assert parse_command(with_seq) is None


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (wire.command([("Node Control/Rebirth", 11, "boolean_value", False)]), "rebirth_not_true"),
        (wire.command([("Node Control/Rebirth", 11, "is_null", True)]), "rebirth_not_true"),
        (wire.command([("Node Control/Rebirth", 4, "long_value", 1)]), "unsupported_metric"),
        (wire.command([("Node Control/Rebirth", None, "long_value", 1)]), "rebirth_not_true"),
        (wire.command([("Inst0.V_DINT", 3, "int_value", 999)]), "unsupported_metric"),
        (wire.command([("Inst0.V_STRING", 12, "string_value", b"x")]), "unsupported_metric"),
        (wire.command([("Node Control/Reboot", 11, "boolean_value", True)]), "unsupported_metric"),
        (wire.command([("node control/rebirth", 11, "boolean_value", True)]), "unsupported_metric"),
        (wire.command([(None, 11, "alias", 1)]), "unsupported_metric"),
        (
            wire.command(
                [
                    ("Node Control/Rebirth", 11, "boolean_value", True),
                    ("Inst0.V_DINT", 3, "int_value", 1),
                ]
            ),
            "not_a_single_metric",
        ),
        (
            wire.command([("Node Control/Rebirth", 11, "boolean_value", True)] * 2),
            "not_a_single_metric",
        ),
        (b"", "not_a_single_metric"),
        (b"\x12\xff\xff\xff\xff\x0f", "malformed"),
        (b"\x0a\x05abc", "malformed"),
        (b"\xff" * 32, "malformed"),
        (wire.REBIRTH + b"\x00" * MAX_CONTROL_BYTES, "oversized"),
    ],
    ids=[
        "false",
        "null",
        "wrong-datatype",
        "untyped-long",
        "process-write",
        "string-write",
        "other-control",
        "case-variant",
        "alias-only",
        "rebirth-plus-write",
        "two-rebirths",
        "empty",
        "bad-length",
        "truncated",
        "garbage",
        "oversized",
    ],
)
def test_everything_else_is_refused_with_a_reason(payload: bytes, reason: str) -> None:
    assert parse_command(payload) == reason


def _length_delimited(number: int, body: bytes) -> bytes:
    key = bytes([number << 3 | 2]) if number < 16 else bytes([(number << 3 | 2) & 0x7F | 0x80, 1])
    size = len(body)
    prefix = bytearray()
    while True:
        prefix.append(size & 0x7F | (0x80 if size >> 7 else 0))
        size >>= 7
        if not size:
            break
    return key + bytes(prefix) + body


@pytest.mark.parametrize("depth", [150, 1000])
def test_deep_nesting_within_the_size_limit_is_refused(depth: int) -> None:
    metric = b""
    for _ in range(depth):
        template = _length_delimited(2, metric)
        metric = _length_delimited(18, template)
    payload = _length_delimited(2, metric)
    assert len(payload) <= MAX_CONTROL_BYTES
    assert parse_command(payload) == "malformed"


def test_random_inputs_never_raise_and_never_request_a_rebirth() -> None:
    rng = random.Random(184)
    for _ in range(3000):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 64)))
        assert parse_command(data) is not None
