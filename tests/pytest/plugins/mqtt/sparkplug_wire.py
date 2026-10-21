# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Sparkplug B payload reader written from the wire format, independent of the product codec.

Field numbers follow ``sparkplug_b.proto`` (Payload, Metric, PropertySet,
PropertyValue). Unknown fields fail the parse, so an extra field the
product starts emitting is noticed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

Raw = int | bytes


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def fields(data: bytes) -> list[tuple[int, Raw]]:
    out: list[tuple[int, Raw]] = []
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
            out.append((number, value))
        elif wire == 1:
            out.append((number, data[pos : pos + 8]))
            pos += 8
        elif wire == 5:
            out.append((number, data[pos : pos + 4]))
            pos += 4
        elif wire == 2:
            size, pos = _varint(data, pos)
            out.append((number, data[pos : pos + size]))
            pos += size
        else:
            raise ValueError(f"unexpected wire type {wire}")
    if pos != len(data):
        raise ValueError("truncated message")
    return out


@dataclass
class Metric:
    name: str | None = None
    timestamp: int | None = None
    datatype: int | None = None
    is_null: bool = False
    properties: dict[str, str] = field(default_factory=dict)
    value_field: str | None = None
    value: bool | int | float | str | bytes | None = None
    field_numbers: list[int] = field(default_factory=list)


@dataclass
class Payload:
    timestamp: int | None = None
    seq: int | None = None
    metrics: list[Metric] = field(default_factory=list)
    field_numbers: list[int] = field(default_factory=list)

    def metric(self, name: str) -> Metric:
        (found,) = [m for m in self.metrics if m.name == name]
        return found


def _property_set(data: bytes) -> dict[str, str]:
    keys: list[str] = []
    values: list[str] = []
    for number, raw in fields(data):
        assert isinstance(raw, bytes)
        if number == 1:
            keys.append(raw.decode("utf-8"))
        elif number == 2:
            entries = dict(fields(raw))
            assert entries.get(1) == 12, "property type is not String"
            text = entries[8]
            assert isinstance(text, bytes) and set(entries) == {1, 8}
            values.append(text.decode("utf-8"))
        else:
            raise ValueError(f"unexpected PropertySet field {number}")
    assert len(keys) == len(values)
    return dict(zip(keys, values))


_VALUE_FIELDS = {
    10: "int_value",
    11: "long_value",
    12: "float_value",
    13: "double_value",
    14: "boolean_value",
    15: "string_value",
    16: "bytes_value",
}


def metric(data: bytes) -> Metric:
    out = Metric()
    for number, raw in fields(data):
        out.field_numbers.append(number)
        if number == 1 and isinstance(raw, bytes):
            out.name = raw.decode("utf-8")
        elif number == 3 and isinstance(raw, int):
            out.timestamp = raw
        elif number == 4 and isinstance(raw, int):
            out.datatype = raw
        elif number == 7 and isinstance(raw, int):
            out.is_null = bool(raw)
        elif number == 9 and isinstance(raw, bytes):
            out.properties = _property_set(raw)
        elif number in _VALUE_FIELDS:
            assert out.value_field is None, "two value fields"
            out.value_field = _VALUE_FIELDS[number]
            if number == 12:
                assert isinstance(raw, bytes)
                (out.value,) = struct.unpack("<f", raw)
            elif number == 13:
                assert isinstance(raw, bytes)
                (out.value,) = struct.unpack("<d", raw)
            elif number == 14:
                out.value = bool(raw)
            elif number == 15:
                assert isinstance(raw, bytes)
                out.value = raw.decode("utf-8")
            else:
                out.value = raw
        else:
            raise ValueError(f"unexpected Metric field {number}")
    return out


def payload(data: bytes) -> Payload:
    out = Payload()
    for number, raw in fields(data):
        out.field_numbers.append(number)
        if number == 1 and isinstance(raw, int):
            out.timestamp = raw
        elif number == 2 and isinstance(raw, bytes):
            out.metrics.append(metric(raw))
        elif number == 3 and isinstance(raw, int):
            out.seq = raw
        else:
            raise ValueError(f"unexpected Payload field {number}")
    return out


def signed64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


def _key(number: int, wire: int) -> bytes:
    return _encode_varint(number << 3 | wire)


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _length_delimited(number: int, body: bytes) -> bytes:
    return _key(number, 2) + _encode_varint(len(body)) + body


def command(
    metrics: list[tuple[str | None, int | None, str, int | bool | bytes]],
    *,
    seq: int | None = None,
) -> bytes:
    """Hand-built NCMD payload: each metric is (name, datatype, value field, value)."""
    body = b""
    for name, datatype, kind, value in metrics:
        entry = b""
        if name is not None:
            entry += _length_delimited(1, name.encode("utf-8"))
        if datatype is not None:
            entry += _key(4, 0) + _encode_varint(datatype)
        if kind == "boolean_value":
            entry += _key(14, 0) + _encode_varint(int(bool(value)))
        elif kind == "is_null":
            entry += _key(7, 0) + _encode_varint(1)
        elif kind == "long_value":
            assert isinstance(value, int)
            entry += _key(11, 0) + _encode_varint(value & 0xFFFF_FFFF_FFFF_FFFF)
        elif kind == "int_value":
            assert isinstance(value, int)
            entry += _key(10, 0) + _encode_varint(value & 0xFFFF_FFFF)
        elif kind == "string_value":
            assert isinstance(value, bytes)
            entry += _length_delimited(15, value)
        elif kind == "alias":
            assert isinstance(value, int)
            entry += _key(2, 0) + _encode_varint(value)
        body += _length_delimited(2, entry)
    if seq is not None:
        body += _key(3, 0) + _encode_varint(seq)
    return body


REBIRTH = command([("Node Control/Rebirth", 11, "boolean_value", True)])
