# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Value vectors checked in reverse (output back to raw) and through independent protobuf decoders."""

from __future__ import annotations

import base64
import json
import math
import os
import re
import struct
import subprocess
from pathlib import Path

import pytest
import sparkplug_b_pb2 as pb

CONTRACT = Path(os.environ.get("SPIKE_CONTRACT", "/contract"))
VECTORS = json.loads((CONTRACT / "fixtures" / "values" / "value-vectors.json").read_text())[
    "vectors"
]
PROTO_DIR = "/opt/sparkplug"
NS_PER_S = 1_000_000_000
NS_PER_DAY = 86_400 * NS_PER_S
DATE_RE = re.compile(r"^(-?\d{4})-(\d{2})-(\d{2})$")
CLOCK_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{9})$")
DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{9})Z$")
STRUCT = {
    "SINT": "<b",
    "INT": "<h",
    "DINT": "<i",
    "LINT": "<q",
    "USINT": "<B",
    "UINT": "<H",
    "UDINT": "<I",
    "ULINT": "<Q",
    "BYTE": "<B",
    "WORD": "<H",
    "DWORD": "<I",
    "LWORD": "<Q",
    "REAL": "<f",
    "LREAL": "<d",
    "TIME": "<q",
    "TOD": "<q",
    "DATE": "<q",
    "DT": "<q",
}

JsonScalar = bool | int | float | str | None


def days_from_civil(y: int, m: int, d: int) -> int:
    """Howard Hinnant's algorithm; independent of datetime."""
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def clock_ns(h: str, mi: str, s: str, frac: str) -> int:
    return ((int(h) * 60 + int(mi)) * 60 + int(s)) * NS_PER_S + int(frac)


def reverse_generic(iec: str, value: JsonScalar) -> bytes:
    """Rebuild the debug_read bytes (value part) from a parsed generic payload."""
    if iec == "BOOL":
        assert isinstance(value, bool)
        return bytes([int(value)])
    if iec == "STRING":
        assert isinstance(value, str)
        return value.encode("utf-8")
    if iec == "WSTRING":
        assert isinstance(value, str)
        return base64.b64decode(value, validate=True)
    if iec in ("LINT", "ULINT", "LWORD", "TIME"):
        assert isinstance(value, str) and re.fullmatch(r"-?\d+", value)
        return struct.pack(STRUCT[iec], int(value))
    if iec == "TOD":
        assert isinstance(value, str)
        m = CLOCK_RE.fullmatch(value)
        assert m
        return struct.pack("<q", clock_ns(*m.groups()))
    if iec == "DATE":
        assert isinstance(value, str)
        m = DATE_RE.fullmatch(value)
        assert m
        return struct.pack("<q", days_from_civil(*(int(g) for g in m.groups())))
    if iec == "DT":
        assert isinstance(value, str)
        m = DT_RE.fullmatch(value)
        assert m
        g = m.groups()
        ns = days_from_civil(int(g[0]), int(g[1]), int(g[2])) * NS_PER_DAY + clock_ns(
            g[3], g[4], g[5], g[6]
        )
        return struct.pack("<q", ns)
    if iec in ("REAL", "LREAL"):
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        return struct.pack(STRUCT[iec], float(value))
    assert isinstance(value, int) and not isinstance(value, bool)
    return struct.pack(STRUCT[iec], value)


def value_part(iec: str, raw: bytes) -> bytes:
    if iec == "STRING":
        return raw[1 : 1 + raw[0]]
    if iec == "WSTRING":
        return raw[1 : 1 + 2 * raw[0]]
    return raw


def protoc_decode(protoc: str, metric_hex: str) -> str:
    payload = pb.Payload()
    payload.metrics.add().ParseFromString(bytes.fromhex(metric_hex))
    wire = payload.SerializeToString()
    out = subprocess.run(
        [
            protoc,
            f"--proto_path={PROTO_DIR}",
            "--decode=org.eclipse.tahu.protobuf.Payload",
            "sparkplug_b.proto",
        ],
        input=wire,
        capture_output=True,
        check=True,
    )
    return out.stdout.decode("utf-8")


def test_vector_file_shape() -> None:
    assert len(VECTORS) >= 50
    assert len({v["id"] for v in VECTORS}) == len(VECTORS)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["id"])
def test_generic_payload_reverses_to_raw(vector: dict[str, object]) -> None:
    iec = str(vector["iecType"])
    raw = bytes.fromhex(str(vector["raw"]))
    text = str(vector["generic"])
    envelope = json.loads(text)
    assert list(envelope) == ["value"]
    assert json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) == text
    if envelope["value"] is None:
        assert str(vector["id"]).endswith("-null")
        return
    assert not str(vector["id"]).endswith("-null")
    if iec in ("REAL", "LREAL"):
        (expected,) = struct.unpack(STRUCT[iec], raw)
        assert math.isfinite(expected)
        assert reverse_generic(iec, envelope["value"]) == raw
        return
    assert reverse_generic(iec, envelope["value"]) == value_part(iec, raw)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["id"])
@pytest.mark.parametrize(
    "protoc",
    ["/usr/bin/protoc", "/opt/protoc36/bin/protoc"],
    ids=["protoc-3.21-debian", "protoc-36.2"],
)
def test_sparkplug_metric_decodes_independently(vector: dict[str, object], protoc: str) -> None:
    spec = vector["sparkplug"]
    assert isinstance(spec, dict)
    iec = str(vector["iecType"])
    raw = bytes.fromhex(str(vector["raw"]))
    text = protoc_decode(protoc, str(spec["birthMetric"]))
    assert f"datatype: {spec['datatype']}" in text
    for key, prop in spec["properties"].items():
        assert f'keys: "{key}"' in text and f'string_value: "{prop}"' in text
    if spec["isNull"]:
        assert "is_null: true" in text
        assert (
            pb.Payload.Metric.FromString(bytes.fromhex(str(spec["birthMetric"]))).WhichOneof(
                "value"
            )
            is None
        )
        return
    value_part_raw = value_part(iec, raw)
    if iec in ("SINT", "INT", "DINT"):
        (v,) = struct.unpack(STRUCT[iec], raw)
        assert f"int_value: {v & 0xFFFFFFFF}\n" in text
    elif iec in ("USINT", "UINT", "UDINT", "BYTE", "WORD", "DWORD"):
        (v,) = struct.unpack(STRUCT[iec], raw)
        assert f"int_value: {v}\n" in text
    elif iec in ("LINT", "TIME", "TOD", "DATE", "DT"):
        (v,) = struct.unpack("<q", raw)
        if iec == "DATE":
            v *= NS_PER_DAY
        assert f"long_value: {v & 0xFFFFFFFFFFFFFFFF}\n" in text
    elif iec in ("ULINT", "LWORD"):
        (v,) = struct.unpack("<Q", raw)
        assert f"long_value: {v}\n" in text
    elif iec == "BOOL":
        assert f"boolean_value: {'true' if raw[0] else 'false'}" in text
    elif iec in ("REAL", "LREAL"):
        m = re.search(r"(float|double)_value: (\S+)", text)
        assert m and m.group(1) == ("float" if iec == "REAL" else "double")
        decoded = float(m.group(2))
        if iec == "REAL":
            assert struct.pack("<f", decoded) == raw
        else:
            assert struct.pack("<d", decoded) == raw
    elif iec == "STRING":
        reparsed = pb.Payload.Metric.FromString(bytes.fromhex(str(spec["birthMetric"])))
        assert reparsed.string_value.encode("utf-8") == value_part_raw
        assert "string_value:" in text
    elif iec == "WSTRING":
        reparsed = pb.Payload.Metric.FromString(bytes.fromhex(str(spec["birthMetric"])))
        assert reparsed.bytes_value == value_part_raw
        assert "bytes_value:" in text or value_part_raw == b""
    data_text = protoc_decode(protoc, str(spec["dataMetric"]))
    assert "properties" not in data_text and f"datatype: {spec['datatype']}" in data_text


def test_metric_wire_is_deterministic_across_serializations() -> None:
    for vector in VECTORS:
        spec = vector["sparkplug"]
        metric = pb.Payload.Metric.FromString(bytes.fromhex(spec["birthMetric"]))
        assert metric.SerializeToString(deterministic=True).hex() == spec["birthMetric"]


def test_full_birth_size_is_computable_against_the_payload_limit() -> None:
    payload = pb.Payload()
    payload.timestamp = 1_791_590_400_000
    payload.seq = 0
    for i in range(1000):
        m = payload.metrics.add()
        m.name = f"{i:04d}" + "n" * 508
        m.timestamp = 1_791_590_400_000
        m.datatype = 12
        for key, text in (("iecType", "STRING"), ("encoding", "native")):
            m.properties.keys.append(key)
            pv = m.properties.values.add()
            pv.type = 12
            pv.string_value = text
        m.string_value = "s" * 254
    size = payload.ByteSize()
    assert size == len(payload.SerializeToString())
    assert (
        size > 262_144
    ), "1000 maximal STRING metrics do not fit; producer and runtime must reject, not truncate"
