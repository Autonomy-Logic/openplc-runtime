# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Generates fixtures/values/value-vectors.json: debug_read bytes to generic JSON and Sparkplug metric.

Forward direction only. ``spike_codec.py`` checks every vector in the reverse
direction and with an independent protobuf decoder.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import struct
import sys
from dataclasses import dataclass

import sparkplug_b_pb2 as pb

NS_PER_S = 1_000_000_000
NS_PER_DAY = 86_400 * NS_PER_S
MAX_DATE_DAYS = (2**63 - 1) // NS_PER_DAY
EPOCH = dt.datetime(1970, 1, 1)
STRING_CAP = 126

SPARKPLUG_TYPES = {
    "BOOL": ("Boolean", 11),
    "SINT": ("Int8", 1),
    "INT": ("Int16", 2),
    "DINT": ("Int32", 3),
    "LINT": ("Int64", 4),
    "USINT": ("UInt8", 5),
    "UINT": ("UInt16", 6),
    "UDINT": ("UInt32", 7),
    "ULINT": ("UInt64", 8),
    "BYTE": ("UInt8", 5),
    "WORD": ("UInt16", 6),
    "DWORD": ("UInt32", 7),
    "LWORD": ("UInt64", 8),
    "REAL": ("Float", 9),
    "LREAL": ("Double", 10),
    "STRING": ("String", 12),
    "WSTRING": ("Bytes", 17),
    "TIME": ("Int64", 4),
    "TOD": ("Int64", 4),
    "DATE": ("Int64", 4),
    "DT": ("Int64", 4),
}
ENCODING = {
    "WSTRING": "utf16le",
    "TIME": "int64-ns-duration",
    "TOD": "int64-ns-since-midnight",
    "DATE": "int64-ns-since-unix-epoch-utc-midnight",
    "DT": "int64-ns-since-unix-epoch",
}
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

Native = bool | int | float | str | bytes | None


def shortest_f32(value: float) -> str:
    """Shortest decimal that round-trips through float32, rendered with Python float repr."""
    for precision in range(1, 10):
        text = f"{value:.{precision}g}"
        try:
            if struct.unpack("<f", struct.pack("<f", float(text)))[0] == value:
                return repr(float(text))
        except OverflowError:
            continue
    raise AssertionError("float32 needs at most 9 significant digits")


def fmt_clock(ns_of_day: int) -> str:
    seconds, frac = divmod(ns_of_day, NS_PER_S)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}.{frac:09d}"


def decode(iec: str, raw: bytes) -> tuple[str | None, Native]:
    """Return (generic JSON value text, native value for Sparkplug); (None, None) means null."""
    if iec == "BOOL":
        return ({0: "false", 1: "true"}.get(raw[0]), {0: False, 1: True}.get(raw[0]))
    if iec == "STRING":
        count = raw[0]
        if count > STRING_CAP:
            return None, None
        try:
            text = raw[1 : 1 + count].decode("utf-8")
        except UnicodeDecodeError:
            return None, None
        return json.dumps(text, ensure_ascii=False), text
    if iec == "WSTRING":
        count = raw[0]
        if count > STRING_CAP:
            return None, None
        payload = raw[1 : 1 + 2 * count]
        return json.dumps(base64.b64encode(payload).decode("ascii")), payload
    (value,) = struct.unpack(STRUCT[iec], raw)
    if iec in ("REAL", "LREAL"):
        if not math.isfinite(value):
            return None, None
        return (shortest_f32(value) if iec == "REAL" else repr(value)), value
    if iec in ("LINT", "ULINT", "LWORD", "TIME"):
        return json.dumps(str(value)), value
    if iec == "TOD":
        return (json.dumps(fmt_clock(value)), value) if 0 <= value < NS_PER_DAY else (None, None)
    if iec == "DATE":
        # STruC++ stores DATE as signed days since the Unix epoch.
        if not -MAX_DATE_DAYS <= value <= MAX_DATE_DAYS:
            return None, None
        day = (EPOCH + dt.timedelta(days=value)).date()
        return json.dumps(day.isoformat()), value * NS_PER_DAY
    if iec == "DT":
        days, ns_of_day = divmod(value, NS_PER_DAY)
        try:
            day = (EPOCH + dt.timedelta(days=days)).date()
        except OverflowError:
            return None, None
        return json.dumps(f"{day.isoformat()}T{fmt_clock(ns_of_day)}Z"), value
    return str(value), value


def properties(iec: str) -> list[tuple[str, str]]:
    props = [("iecType", iec), ("encoding", ENCODING.get(iec, "native"))]
    if iec in ("TIME", "TOD", "DATE", "DT"):
        props.append(("unit", "ns"))
    if iec in ("DATE", "DT"):
        props.append(("epoch", "unix"))
    return props


def metric(iec: str, native: Native, birth: bool) -> pb.Payload.Metric:
    m = pb.Payload.Metric()
    m.name = "v"
    m.datatype = SPARKPLUG_TYPES[iec][1]
    if birth:
        for key, text in properties(iec):
            m.properties.keys.append(key)
            pv = m.properties.values.add()
            pv.type = 12
            pv.string_value = text
    if native is None:
        m.is_null = True
    elif isinstance(native, bool):
        m.boolean_value = native
    elif isinstance(native, str):
        m.string_value = native
    elif isinstance(native, bytes):
        m.bytes_value = native
    elif isinstance(native, float):
        if iec == "REAL":
            m.float_value = native
        else:
            m.double_value = native
    elif SPARKPLUG_TYPES[iec][1] in (4, 8):
        m.long_value = native & 0xFFFFFFFFFFFFFFFF
    else:
        m.int_value = native & 0xFFFFFFFF
    return m


@dataclass(frozen=True)
class Case:
    id: str
    iec: str
    raw: bytes


def p(fmt: str, value: float) -> bytes:
    return struct.pack(fmt, value)


def string_window(payload: bytes, count: int, wide: bool) -> bytes:
    width = 253 if wide else 127
    body = bytes([count]) + payload
    return body + b"\x00" * (width - len(body))


def wide(*units: int) -> bytes:
    return b"".join(struct.pack("<H", u) for u in units)


def date_ns(y: int, m: int, d: int) -> int:
    return (dt.datetime(y, m, d) - EPOCH).days * NS_PER_DAY


CASES = [
    Case("bool-false", "BOOL", b"\x00"),
    Case("bool-true", "BOOL", b"\x01"),
    Case("bool-invalid-byte-null", "BOOL", b"\x02"),
    Case("sint-min", "SINT", p("<b", -128)),
    Case("sint-max", "SINT", p("<b", 127)),
    Case("int-min", "INT", p("<h", -32768)),
    Case("int-max", "INT", p("<h", 32767)),
    Case("dint-min", "DINT", p("<i", -2147483648)),
    Case("dint-max", "DINT", p("<i", 2147483647)),
    Case("lint-min", "LINT", p("<q", -(2**63))),
    Case("lint-max", "LINT", p("<q", 2**63 - 1)),
    Case("lint-above-safe-integer", "LINT", p("<q", 2**53 + 1)),
    Case("usint-max", "USINT", p("<B", 255)),
    Case("uint-max", "UINT", p("<H", 65535)),
    Case("udint-max", "UDINT", p("<I", 2**32 - 1)),
    Case("ulint-max", "ULINT", p("<Q", 2**64 - 1)),
    Case("byte-bits", "BYTE", p("<B", 0xA5)),
    Case("word-bits", "WORD", p("<H", 0xBEEF)),
    Case("dword-bits", "DWORD", p("<I", 0xDEADBEEF)),
    Case("lword-max", "LWORD", p("<Q", 2**64 - 1)),
    Case("lword-high-bit", "LWORD", p("<Q", 2**63)),
    Case("real-tenth", "REAL", p("<f", 0.1)),
    Case("real-negative-zero", "REAL", p("<f", -0.0)),
    Case("real-integral", "REAL", p("<f", 100.0)),
    Case("real-max", "REAL", p("<f", 3.4028234663852886e38)),
    Case("real-min-subnormal", "REAL", p("<f", 1.401298464324817e-45)),
    Case("real-nan-null", "REAL", b"\x00\x00\xc0\x7f"),
    Case("real-inf-null", "REAL", b"\x00\x00\x80\x7f"),
    Case("lreal-tenth", "LREAL", p("<d", 0.1)),
    Case("lreal-negative-zero", "LREAL", p("<d", -0.0)),
    Case("lreal-large", "LREAL", p("<d", 1e308)),
    Case("lreal-min-subnormal", "LREAL", p("<d", 5e-324)),
    Case("lreal-neg-inf-null", "LREAL", b"\x00\x00\x00\x00\x00\x00\xf0\xff"),
    Case("string-empty", "STRING", string_window(b"", 0, False)),
    Case(
        "string-escaping",
        "STRING",
        string_window('Olá "x"\n\\\u0001'.encode(), len('Olá "x"\n\\\u0001'.encode()), False),
    ),
    Case("string-max-window", "STRING", string_window(b"a" * 126, 126, False)),
    Case("string-invalid-utf8-null", "STRING", string_window(b"\xff", 1, False)),
    Case("string-count-over-cap-null", "STRING", string_window(b"", 127, False)[:127]),
    Case("wstring-empty", "WSTRING", string_window(b"", 0, True)),
    Case("wstring-surrogate-pair", "WSTRING", string_window(wide(0x41, 0xD83D, 0xDE00), 3, True)),
    Case("wstring-unpaired-high", "WSTRING", string_window(wide(0xD800), 1, True)),
    Case("wstring-unpaired-low", "WSTRING", string_window(wide(0x61, 0xDC00, 0x62), 3, True)),
    Case("time-one-ns", "TIME", p("<q", 1)),
    Case("time-negative", "TIME", p("<q", -1_500_000_001)),
    Case("time-max", "TIME", p("<q", 2**63 - 1)),
    Case("tod-midnight", "TOD", p("<q", 0)),
    Case("tod-last-ns", "TOD", p("<q", NS_PER_DAY - 1)),
    Case("tod-out-of-range-null", "TOD", p("<q", NS_PER_DAY)),
    Case("tod-negative-null", "TOD", p("<q", -1)),
    Case("date-epoch", "DATE", p("<q", 0)),
    Case("date-before-epoch", "DATE", p("<q", -1)),
    Case("date-2026-10-09", "DATE", p("<q", date_ns(2026, 10, 9) // NS_PER_DAY)),
    Case("date-beyond-ns-range-null", "DATE", p("<q", MAX_DATE_DAYS + 1)),
    Case("dt-epoch", "DT", p("<q", 0)),
    Case("dt-one-ns-before-epoch", "DT", p("<q", -1)),
    Case(
        "dt-nanoseconds",
        "DT",
        p("<q", date_ns(2026, 10, 9) + (12 * 3600 + 34 * 60 + 56) * NS_PER_S + 123_456_789),
    ),
    Case("dt-int64-min", "DT", p("<q", -(2**63))),
]


def build() -> dict[str, object]:
    vectors: list[dict[str, object]] = []
    for case in CASES:
        value_text, native = decode(case.iec, case.raw)
        name, type_id = SPARKPLUG_TYPES[case.iec]
        vectors.append(
            {
                "id": case.id,
                "iecType": case.iec,
                "raw": case.raw.hex(),
                "generic": '{"value":' + (value_text if value_text is not None else "null") + "}",
                "sparkplug": {
                    "datatype": type_id,
                    "datatypeName": name,
                    "isNull": native is None,
                    "properties": dict(properties(case.iec)),
                    "birthMetric": metric(case.iec, native, True)
                    .SerializeToString(deterministic=True)
                    .hex(),
                    "dataMetric": metric(case.iec, native, False)
                    .SerializeToString(deterministic=True)
                    .hex(),
                },
            }
        )
    return {
        "contract": "openplc-mqtt-value-vectors",
        "version": 1,
        "raw": "bytes written by debug_read(arr, elem, dest), little-endian; STRING/WSTRING are the full debug window",
        "metric": "Payload.Metric with name 'v'; timestamp omitted; deterministic protobuf serialization",
        "vectors": vectors,
    }


if __name__ == "__main__":
    with open(sys.argv[1], "w", encoding="utf-8") as out:
        json.dump(build(), out, ensure_ascii=True, indent=2)
        out.write("\n")
