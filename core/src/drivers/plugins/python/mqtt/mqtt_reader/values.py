# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Exact conversion of ``debug_read`` bytes into protocol-neutral leaf values.

``convert_window`` decodes one debug window as the value vectors define it.
``convert_debug_read`` adds the rule for strings read through ``debug_read``,
which clamps the length prefix to the window: a full window cannot be told
apart from a longer string, so it becomes null instead of a truncated value.
Nothing here produces a zero, a truncated string or a rounded number for a
value that cannot be represented exactly; such values are null with a
``ReadIssue``. Standard library only.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import struct
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from contract.constants import DEBUG_SIZE

Native: TypeAlias = bool | int | float | str | bytes | None
ReadIssueCode = Literal["read_failed", "conversion_failed", "string_exceeds_read_window"]

READ_ISSUE_CATEGORY: Final = "read_conversion"
DEBUG_STRING_CAP: Final = 126
NS_PER_SECOND: Final = 1_000_000_000
NS_PER_DAY: Final = 86_400 * NS_PER_SECOND
# STruC++ stores DATE as signed days; the published value is int64 ns at UTC midnight.
MAX_DATE_DAYS: Final = (2**63 - 1) // NS_PER_DAY
_EPOCH_ORDINAL: Final = dt.date(1970, 1, 1).toordinal()
_FLOAT32_MAX_DIGITS: Final = 9

_STRUCT: Final[dict[str, str]] = {
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
_DECIMAL_STRING_TYPES: Final = frozenset({"LINT", "ULINT", "LWORD", "TIME"})
_ISSUE_TEXT: Final[dict[str, str]] = {
    "no_value": "the runtime returned no value for this address",
    "size_mismatch": "the runtime returned a value of unexpected size",
    "thunk_error": "the runtime read call failed",
    "bool_out_of_range": "BOOL byte is neither 0 nor 1",
    "float_not_finite": "floating point value is not finite",
    "utf8_invalid": "STRING bytes are not valid UTF-8",
    "string_count_over_cap": "string length prefix exceeds the read window",
    "string_window_full": "string fills the read window and may be longer",
    "tod_out_of_range": "TOD value is outside one day",
    "datetime_out_of_range": "date value is outside the supported calendar",
}


@dataclass(frozen=True)
class ReadIssue:
    """Why a leaf value is null. Carries no process value."""

    code: ReadIssueCode
    detail: str

    @property
    def category(self) -> str:
        return READ_ISSUE_CATEGORY

    @property
    def message(self) -> str:
        return _ISSUE_TEXT.get(self.detail, self.detail)


@dataclass(frozen=True)
class LeafValue:
    """One sampled leaf.

    ``native`` is the exact value for typed encoders (Python ``int`` for every
    integer and temporal type, ``float`` for REAL/LREAL, ``str`` for STRING,
    UTF-16LE ``bytes`` for WSTRING); ``json_text`` is the canonical generic
    JSON value. Both are ``None`` exactly when the value is null.
    """

    iec_type: str
    native: Native
    json_text: str | None
    source: bytes | None
    issue: ReadIssue | None

    @property
    def is_null(self) -> bool:
        return self.issue is not None

    @property
    def identity(self) -> tuple[str, bytes | None]:
        """Comparison key for change detection: type plus source bytes, null as ``None``."""
        return (self.iec_type, None if self.is_null else self.source)


def null_value(iec_type: str, code: ReadIssueCode, detail: str) -> LeafValue:
    return LeafValue(iec_type, None, None, None, ReadIssue(code, detail))


def _value(iec_type: str, native: Native, json_text: str, source: bytes) -> LeafValue:
    return LeafValue(iec_type, native, json_text, source, None)


def float32_text(source: bytes) -> str:
    """Shortest decimal (at most 9 digits) that round-trips to the same float32 bits."""
    (value,) = struct.unpack("<f", source)
    for digits in range(1, _FLOAT32_MAX_DIGITS + 1):
        candidate = float(f"{value:.{digits}g}")
        try:
            if struct.pack("<f", candidate) == source:
                return repr(candidate)
        except OverflowError:
            continue
    return repr(value)


def clock_text(ns_of_day: int) -> str:
    seconds, fraction = divmod(ns_of_day, NS_PER_SECOND)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{fraction:09d}"


def _calendar_day(days: int) -> dt.date | None:
    try:
        return dt.date.fromordinal(_EPOCH_ORDINAL + days)
    except (OverflowError, ValueError):
        return None


def _convert_string(iec_type: str, raw: bytes) -> LeafValue:
    count = raw[0]
    if count > DEBUG_STRING_CAP:
        return null_value(iec_type, "conversion_failed", "string_count_over_cap")
    if iec_type == "STRING":
        source = raw[: 1 + count]
        try:
            text = source[1:].decode("utf-8")
        except UnicodeDecodeError:
            return null_value(iec_type, "conversion_failed", "utf8_invalid")
        return _value(iec_type, text, json.dumps(text, ensure_ascii=False), source)
    source = raw[: 1 + 2 * count]
    units = source[1:]
    return _value(iec_type, units, json.dumps(base64.b64encode(units).decode("ascii")), source)


def _convert_float(iec_type: str, raw: bytes) -> LeafValue:
    (value,) = struct.unpack(_STRUCT[iec_type], raw)
    if not math.isfinite(value):
        return null_value(iec_type, "conversion_failed", "float_not_finite")
    text = float32_text(raw) if iec_type == "REAL" else repr(value)
    return _value(iec_type, value, text, raw)


def _convert_temporal(iec_type: str, value: int, raw: bytes) -> LeafValue:
    if iec_type == "TOD":
        if not 0 <= value < NS_PER_DAY:
            return null_value(iec_type, "conversion_failed", "tod_out_of_range")
        return _value(iec_type, value, json.dumps(clock_text(value)), raw)
    if iec_type == "DATE":
        day = _calendar_day(value) if -MAX_DATE_DAYS <= value <= MAX_DATE_DAYS else None
        if day is None:
            return null_value(iec_type, "conversion_failed", "datetime_out_of_range")
        return _value(iec_type, value * NS_PER_DAY, json.dumps(day.isoformat()), raw)
    days, ns_of_day = divmod(value, NS_PER_DAY)
    day = _calendar_day(days)
    if day is None:
        return null_value(iec_type, "conversion_failed", "datetime_out_of_range")
    return _value(iec_type, value, json.dumps(f"{day.isoformat()}T{clock_text(ns_of_day)}Z"), raw)


def convert_window(iec_type: str, raw: bytes) -> LeafValue:
    """Decode one complete debug window of ``iec_type`` (length must equal its debug size)."""
    expected = DEBUG_SIZE.get(iec_type)
    if expected is None:
        raise ValueError("unsupported IEC type")
    if len(raw) != expected:
        return null_value(iec_type, "read_failed", "size_mismatch")
    if iec_type == "BOOL":
        if raw[0] > 1:
            return null_value(iec_type, "conversion_failed", "bool_out_of_range")
        flag = raw[0] == 1
        return _value(iec_type, flag, "true" if flag else "false", raw)
    if iec_type in ("STRING", "WSTRING"):
        return _convert_string(iec_type, raw)
    if iec_type in ("REAL", "LREAL"):
        return _convert_float(iec_type, raw)
    (number,) = struct.unpack(_STRUCT[iec_type], raw)
    if iec_type in ("TOD", "DATE", "DT"):
        return _convert_temporal(iec_type, number, raw)
    if iec_type in _DECIMAL_STRING_TYPES:
        return _value(iec_type, number, json.dumps(str(number)), raw)
    return _value(iec_type, number, str(number), raw)


def convert_debug_read(iec_type: str, raw: bytes) -> LeafValue:
    """Decode bytes returned by ``debug_read``, whose string prefix saturates at the window."""
    if iec_type in ("STRING", "WSTRING") and raw and raw[0] == DEBUG_STRING_CAP:
        if len(raw) != DEBUG_SIZE[iec_type]:
            return null_value(iec_type, "read_failed", "size_mismatch")
        return null_value(iec_type, "string_exceeds_read_window", "string_window_full")
    return convert_window(iec_type, raw)
