# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Exact conversion of debug_read bytes against the shared value vectors."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest
from contract.runtime_config import SPARKPLUG_DATATYPE
from mqtt_reader.values import (
    DEBUG_STRING_CAP,
    LeafValue,
    convert_debug_read,
    convert_window,
    float32_text,
)

VECTORS_FILE = (
    Path(__file__).resolve().parents[4]
    / "core/src/drivers/plugins/python/mqtt/contract/fixtures/values/value-vectors.json"
)
VECTORS = json.loads(VECTORS_FILE.read_text(encoding="utf-8"))["vectors"]
WireValue = int | bytes


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, pos


def _fields(data: bytes) -> dict[int, WireValue]:
    """Minimal protobuf wire reader, independent of the code under test."""
    out: dict[int, WireValue] = {}
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            out[number], pos = _varint(data, pos)
        elif wire == 1:
            out[number], pos = data[pos : pos + 8], pos + 8
        elif wire == 5:
            out[number], pos = data[pos : pos + 4], pos + 4
        elif wire == 2:
            length, pos = _varint(data, pos)
            out[number], pos = data[pos : pos + length], pos + length
        else:
            raise AssertionError("unexpected wire type")
    return out


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value >> (bits - 1) else value


def _metric_value(iec: str, metric: dict[int, WireValue]) -> object:
    """Value carried by a Sparkplug data metric, reinterpreted by its datatype."""
    datatype = metric[4]
    if 7 in metric:
        return None
    if 10 in metric:
        raw = metric[10]
        assert isinstance(raw, int)
        return _signed(raw & 0xFFFFFFFF, 32) if datatype in (1, 2, 3) else raw
    if 11 in metric:
        raw = metric[11]
        assert isinstance(raw, int)
        return _signed(raw, 64) if datatype == 4 else raw
    if 12 in metric:
        raw = metric[12]
        assert isinstance(raw, bytes)
        return struct.unpack("<f", raw)[0]
    if 13 in metric:
        raw = metric[13]
        assert isinstance(raw, bytes)
        return struct.unpack("<d", raw)[0]
    if 14 in metric:
        return bool(metric[14])
    if 15 in metric:
        raw = metric[15]
        assert isinstance(raw, bytes)
        return raw.decode("utf-8")
    if 16 in metric:
        return metric[16]
    if iec == "STRING":
        return ""
    if iec == "WSTRING":
        return b""
    return False if iec == "BOOL" else 0


def _envelope(value: LeafValue) -> str:
    return '{"value":' + (value.json_text if value.json_text is not None else "null") + "}"


@pytest.mark.parametrize("vector", VECTORS, ids=[v["id"] for v in VECTORS])
def test_window_conversion_matches_every_vector(vector: dict[str, object]) -> None:
    iec = str(vector["iecType"])
    value = convert_window(iec, bytes.fromhex(str(vector["raw"])))
    sparkplug = vector["sparkplug"]
    assert isinstance(sparkplug, dict)
    assert _envelope(value) == vector["generic"]
    assert value.is_null is sparkplug["isNull"]
    assert (value.native is None) is value.is_null
    assert SPARKPLUG_DATATYPE[iec] == sparkplug["datatype"]
    metric = _fields(bytes.fromhex(sparkplug["dataMetric"]))
    assert metric[4] == sparkplug["datatype"]
    expected = _metric_value(iec, metric)
    if isinstance(expected, float):
        assert isinstance(value.native, float)
        assert struct.pack("<d", value.native) == struct.pack("<d", expected)
    else:
        assert value.native == expected and type(value.native) is type(expected)


@pytest.mark.parametrize("vector", VECTORS, ids=[v["id"] for v in VECTORS])
def test_generic_json_parses_back_to_the_exact_value(vector: dict[str, object]) -> None:
    iec = str(vector["iecType"])
    value = convert_window(iec, bytes.fromhex(str(vector["raw"])))
    if value.is_null:
        return
    assert value.json_text is not None
    parsed = json.loads(value.json_text)
    if iec in ("LINT", "ULINT", "LWORD", "TIME"):
        assert isinstance(parsed, str) and int(parsed) == value.native
    elif iec == "WSTRING":
        assert base64.b64decode(parsed, validate=True) == value.native
    elif iec in ("REAL", "LREAL"):
        fmt = "<f" if iec == "REAL" else "<d"
        assert struct.pack(fmt, parsed) == value.source
    elif iec in ("TOD", "DATE", "DT"):
        assert isinstance(parsed, str)
    else:
        assert parsed == value.native and type(parsed) is type(value.native)


@pytest.mark.parametrize(
    ("iec", "raw", "text"),
    [
        ("LINT", struct.pack("<q", -(2**63)), '"-9223372036854775808"'),
        ("LINT", struct.pack("<q", 2**63 - 1), '"9223372036854775807"'),
        ("ULINT", struct.pack("<Q", 2**64 - 1), '"18446744073709551615"'),
        ("LWORD", struct.pack("<Q", 2**64 - 1), '"18446744073709551615"'),
        ("ULINT", struct.pack("<Q", 2**53 + 1), '"9007199254740993"'),
        ("SINT", struct.pack("<b", -128), "-128"),
        ("UDINT", struct.pack("<I", 2**32 - 1), "4294967295"),
        ("DINT", struct.pack("<i", -(2**31)), "-2147483648"),
        ("TIME", struct.pack("<q", -(2**63)), '"-9223372036854775808"'),
        ("TIME", struct.pack("<q", -1), '"-1"'),
        ("TOD", struct.pack("<q", 1), '"00:00:00.000000001"'),
        ("DT", struct.pack("<q", 2**63 - 1), '"2262-04-11T23:47:16.854775807Z"'),
        ("DT", struct.pack("<q", -1_500_000_001), '"1969-12-31T23:59:58.499999999Z"'),
        ("DATE", struct.pack("<q", -365), '"1969-01-01"'),
        ("DATE", struct.pack("<q", 19782), '"2024-02-29"'),
        ("DATE", struct.pack("<q", 106_751), '"2262-04-11"'),
        ("DATE", struct.pack("<q", -106_751), '"1677-09-22"'),
    ],
)
def test_integer_and_temporal_extremes_are_exact(iec: str, raw: bytes, text: str) -> None:
    value = convert_debug_read(iec, raw)
    assert value.json_text == text
    assert value.issue is None


def test_wstring_code_units_are_preserved_without_storage_padding() -> None:
    units = struct.pack("<5H", 0xD83D, 0xDE00, 0xDC00, 0x0041, 0xD800)
    raw = bytes([5]) + units + bytes(253 - 1 - len(units))
    value = convert_debug_read("WSTRING", raw)
    assert value.native == units
    assert value.json_text is not None
    assert base64.b64decode(json.loads(value.json_text)) == units
    assert value.source == raw[: 1 + len(units)]


@pytest.mark.parametrize(("iec", "unit"), [("STRING", b"a"), ("WSTRING", b"a\x00")])
def test_full_debug_window_is_null_instead_of_truncated(iec: str, unit: bytes) -> None:
    full = bytes([DEBUG_STRING_CAP]) + unit * DEBUG_STRING_CAP
    window = convert_window(iec, full)
    read = convert_debug_read(iec, full)
    assert window.issue is None
    assert read.is_null and read.json_text is None and read.native is None
    assert read.issue is not None
    assert (read.issue.code, read.issue.category) == (
        "string_exceeds_read_window",
        "read_conversion",
    )
    shorter = bytes([DEBUG_STRING_CAP - 1]) + full[1:]
    assert convert_debug_read(iec, shorter).issue is None


@pytest.mark.parametrize(
    ("iec", "raw", "code", "detail"),
    [
        ("BOOL", b"\x02", "conversion_failed", "bool_out_of_range"),
        ("REAL", struct.pack("<f", float("nan")), "conversion_failed", "float_not_finite"),
        ("REAL", struct.pack("<f", float("-inf")), "conversion_failed", "float_not_finite"),
        ("LREAL", struct.pack("<d", float("inf")), "conversion_failed", "float_not_finite"),
        ("STRING", bytes([1, 0xC3]) + bytes(125), "conversion_failed", "utf8_invalid"),
        ("STRING", bytes([127]) + bytes(126), "conversion_failed", "string_count_over_cap"),
        ("WSTRING", bytes([200]) + bytes(252), "conversion_failed", "string_count_over_cap"),
        ("TOD", struct.pack("<q", 86_400 * 10**9), "conversion_failed", "tod_out_of_range"),
        ("TOD", struct.pack("<q", -1), "conversion_failed", "tod_out_of_range"),
        ("DATE", struct.pack("<q", 106_752), "conversion_failed", "datetime_out_of_range"),
        ("DATE", struct.pack("<q", -106_752), "conversion_failed", "datetime_out_of_range"),
        ("DINT", b"\x00\x00\x00", "read_failed", "size_mismatch"),
        ("LINT", bytes(9), "read_failed", "size_mismatch"),
        ("STRING", bytes(126), "read_failed", "size_mismatch"),
        ("WSTRING", bytes([126]) + bytes(10), "read_failed", "size_mismatch"),
    ],
)
def test_unrepresentable_values_become_diagnosed_nulls(
    iec: str, raw: bytes, code: str, detail: str
) -> None:
    value = convert_debug_read(iec, raw)
    assert value.native is None and value.json_text is None and value.source is None
    assert value.issue is not None
    assert (value.issue.code, value.issue.detail) == (code, detail)
    assert value.issue.message and raw.hex() not in value.issue.message


def test_null_and_recovery_are_changes_but_null_to_null_is_not() -> None:
    good = convert_debug_read("REAL", struct.pack("<f", 1.5))
    nan = convert_debug_read("REAL", struct.pack("<f", float("nan")))
    other_nan = convert_debug_read("REAL", b"\x01\x00\xc0\x7f")
    again = convert_debug_read("REAL", struct.pack("<f", 1.5))
    assert good.identity != nan.identity
    assert nan.identity == other_nan.identity == ("REAL", None)
    assert again.identity == good.identity


def test_change_key_keeps_bits_that_equality_would_merge() -> None:
    zero = convert_debug_read("LREAL", struct.pack("<d", 0.0))
    negative_zero = convert_debug_read("LREAL", struct.pack("<d", -0.0))
    big = convert_debug_read("ULINT", struct.pack("<Q", 2**64 - 1))
    big_minus_one = convert_debug_read("ULINT", struct.pack("<Q", 2**64 - 2))
    assert zero.native == negative_zero.native
    assert zero.identity != negative_zero.identity
    assert big.identity != big_minus_one.identity


def test_string_change_key_ignores_bytes_past_the_length() -> None:
    first = convert_debug_read("STRING", bytes([2]) + b"ok" + bytes(124))
    padded = convert_debug_read("STRING", bytes([2]) + b"ok" + b"\xff" * 124)
    assert first.identity == padded.identity


@pytest.mark.parametrize(
    "number", [0.1, 1e-45, 3.4028234663852886e38, 1.1754943508222875e-38, 16777217.0, -2.5]
)
def test_float32_text_is_shortest_round_trip(number: float) -> None:
    source = struct.pack("<f", number)
    text = float32_text(source)
    assert struct.pack("<f", float(text)) == source
    digits = len(text.replace("-", "").split("e")[0].replace(".", "").strip("0")) or 1
    assert digits <= 9


def test_unknown_type_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        convert_window("XWORD", b"\x00")
