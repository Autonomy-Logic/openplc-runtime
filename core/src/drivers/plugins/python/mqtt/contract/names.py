# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""MQTT/Sparkplug destination naming rules shared with the Editor/Web producers.

Mirror of ``src/frontend/utils/mqtt/contract/names.ts``. Both implementations
are pinned by ``fixtures/names/name-vectors.json``; change them together.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

MAX_DESTINATION_BYTES = 512
MAX_NODE_ID_BYTES = 128
MAX_CLIENT_ID_CHARS = 64
TOPIC_PREFIX_ROOT = "openplc"
RESERVED_METRIC_NAMES = ("bdSeq",)
RESERVED_METRIC_PREFIXES = ("Node Control/",)

_SAFE_SEGMENT_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_INDEX_SEGMENT = re.compile(r"^-?(0|[1-9][0-9]*)(,-?(0|[1-9][0-9]*))*$")
_PERCENT_TRIPLET = re.compile(r"%[0-9A-F]{2}")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_OUTER_WHITESPACE = frozenset(
    [
        0x09,
        0x0A,
        0x0B,
        0x0C,
        0x0D,
        0x20,
        0x85,
        0xA0,
        0x1680,
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    ]
    + list(range(0x2000, 0x200B))
)

TokenRole = Literal["instance", "program", "variable", "field", "index"]


@dataclass(frozen=True)
class NameToken:
    role: TokenRole
    name: str


@dataclass(frozen=True)
class IndexToken:
    indices: tuple[int, ...]
    role: TokenRole = "index"


SourceToken = NameToken | IndexToken


@dataclass(frozen=True)
class DecodedName:
    name: str


@dataclass(frozen=True)
class DecodedIndex:
    indices: tuple[int, ...]


@dataclass(frozen=True)
class DestinationConflict:
    code: str
    first: int
    second: int


def _is_control(code_point: int) -> bool:
    return code_point <= 0x1F or 0x7F <= code_point <= 0x9F


def _has_lone_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def utf8_length(value: str) -> int:
    return len(value.encode("utf-8"))


def encode_name_segment(name: str) -> str:
    """Reversible, collision-free segment for an identifier token."""
    if name == "":
        raise ValueError("token_empty")
    if _has_lone_surrogate(name):
        raise ValueError("token_invalid_unicode")
    out: list[str] = []
    for position, ch in enumerate(name):
        if ch in _SAFE_SEGMENT_CHARS and not (position == 0 and ch.isdigit()):
            out.append(ch)
        else:
            out.extend(f"%{byte:02X}" for byte in ch.encode("utf-8"))
    return "".join(out)


def encode_index_segment(indices: Sequence[int]) -> str:
    if len(indices) == 0:
        raise ValueError("index_empty")
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in indices):
        raise ValueError("index_invalid")
    return ",".join(str(i) for i in indices)


def decode_segment(segment: str) -> DecodedName | DecodedIndex:
    if _INDEX_SEGMENT.fullmatch(segment):
        if "-0" in segment.split(","):
            raise ValueError("segment_invalid")
        return DecodedIndex(tuple(int(p) for p in segment.split(",")))
    raw = bytearray()
    position = 0
    while position < len(segment):
        ch = segment[position]
        if ch == "%":
            triplet = segment[position : position + 3]
            if not _PERCENT_TRIPLET.fullmatch(triplet):
                raise ValueError("segment_invalid")
            raw.append(int(triplet[1:], 16))
            position += 3
        elif ch in _SAFE_SEGMENT_CHARS:
            raw.extend(ch.encode("ascii"))
            position += 1
        else:
            raise ValueError("segment_invalid")
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("segment_invalid") from exc
    if name == "" or encode_name_segment(name) != segment:
        raise ValueError("segment_invalid")
    return DecodedName(name)


def _segments(tokens: Sequence[SourceToken]) -> list[str]:
    if len(tokens) == 0 or isinstance(tokens[0], IndexToken):
        raise ValueError("source_invalid")
    return [
        (
            encode_index_segment(t.indices)
            if isinstance(t, IndexToken)
            else encode_name_segment(t.name)
        )
        for t in tokens
    ]


def default_topic_prefix(client_id: str) -> str:
    return f"{TOPIC_PREFIX_ROOT}/{client_id}"


def default_topic(prefix: str, tokens: Sequence[SourceToken]) -> str:
    return "/".join([prefix, *_segments(tokens)])


def default_metric_name(tokens: Sequence[SourceToken]) -> str:
    parts: list[str] = []
    for token, segment in zip(tokens, _segments(tokens)):
        if isinstance(token, IndexToken):
            parts.append(f"[{segment}]")
        else:
            parts.append(("." if parts else "") + segment)
    return "".join(parts)


def collision_key(name: str) -> str:
    """Key under which two metric names are considered the same name."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).upper())


def validate_topic(topic: str) -> str | None:
    if topic == "":
        return "topic_empty"
    if _has_lone_surrogate(topic):
        return "topic_invalid_unicode"
    if utf8_length(topic) > MAX_DESTINATION_BYTES:
        return "topic_too_long"
    if any(ch in "+#" or _is_control(ord(ch)) for ch in topic):
        return "topic_invalid_char"
    if topic.startswith("$"):
        return "topic_reserved_prefix"
    if any(segment == "" for segment in topic.split("/")):
        return "topic_empty_segment"
    return None


def validate_metric_name(name: str) -> str | None:
    if name == "":
        return "metric_empty"
    if _has_lone_surrogate(name):
        return "metric_invalid_unicode"
    if utf8_length(name) > MAX_DESTINATION_BYTES:
        return "metric_too_long"
    if any(_is_control(ord(ch)) for ch in name):
        return "metric_invalid_char"
    key = collision_key(name)
    if any(key == collision_key(reserved) for reserved in RESERVED_METRIC_NAMES):
        return "metric_reserved"
    if any(key.startswith(collision_key(prefix)) for prefix in RESERVED_METRIC_PREFIXES):
        return "metric_reserved"
    return None


def validate_node_id(value: str) -> str | None:
    """Sparkplug Group ID / Edge Node ID."""
    if value == "":
        return "node_id_empty"
    if _has_lone_surrogate(value):
        return "node_id_invalid_unicode"
    if utf8_length(value) > MAX_NODE_ID_BYTES:
        return "node_id_too_long"
    if any(ch in "/+#" or _is_control(ord(ch)) for ch in value):
        return "node_id_invalid_char"
    if ord(value[0]) in _OUTER_WHITESPACE or ord(value[-1]) in _OUTER_WHITESPACE:
        return "node_id_outer_whitespace"
    return None


def validate_client_id(value: str) -> str | None:
    return (
        None
        if _CLIENT_ID.fullmatch(value) and len(value) <= MAX_CLIENT_ID_CHARS
        else "client_id_invalid"
    )


def find_destination_conflicts(
    mode: Literal["generic", "sparkplug_b"], destinations: Sequence[str]
) -> list[DestinationConflict]:
    seen: dict[str, int] = {}
    conflicts: list[DestinationConflict] = []
    code = "topic_duplicate" if mode == "generic" else "metric_collision"
    for position, destination in enumerate(destinations):
        key = destination if mode == "generic" else collision_key(destination)
        if key in seen:
            conflicts.append(DestinationConflict(code, seen[key], position))
        else:
            seen[key] = position
    return conflicts
