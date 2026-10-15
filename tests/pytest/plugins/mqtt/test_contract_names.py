# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Name rules against the vectors shared byte-for-byte with the Editor/Web producers."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from contract.names import (
    DecodedIndex,
    DecodedName,
    IndexToken,
    NameToken,
    SourceToken,
    TokenRole,
    collision_key,
    decode_segment,
    default_metric_name,
    default_topic,
    default_topic_prefix,
    encode_index_segment,
    encode_name_segment,
    find_destination_conflicts,
    validate_client_id,
    validate_metric_name,
    validate_node_id,
    validate_topic,
)

_FIXTURES = (
    Path(__file__).resolve().parents[4] / "core/src/drivers/plugins/python/mqtt/contract/fixtures"
)
VECTORS = json.loads((_FIXTURES / "names" / "name-vectors.json").read_text(encoding="utf-8"))

JsonValue = str | dict[str, str | int]
_ROLES: dict[str, TokenRole] = {
    "instance": "instance",
    "program": "program",
    "variable": "variable",
    "field": "field",
}
_MODES: dict[str, Literal["generic", "sparkplug_b"]] = {
    "generic": "generic",
    "sparkplug_b": "sparkplug_b",
}


def _value(raw: JsonValue) -> str:
    if isinstance(raw, str):
        return raw
    unit = raw["repeat"]
    count = raw["count"]
    assert isinstance(unit, str) and isinstance(count, int)
    return unit * count


def _tokens(raw: list[dict[str, str | list[int]]]) -> list[SourceToken]:
    out: list[SourceToken] = []
    for token in raw:
        if token["role"] == "index":
            indices = token["indices"]
            assert isinstance(indices, list)
            out.append(IndexToken(tuple(indices)))
        else:
            role = token["role"]
            name = token["name"]
            assert isinstance(role, str) and isinstance(name, str)
            out.append(NameToken(_ROLES[role], name))
    return out


def test_vectors_header() -> None:
    assert VECTORS["contract"] == "openplc-mqtt-name-vectors"
    assert VECTORS["version"] == 1


@pytest.mark.parametrize("case", VECTORS["nameSegments"], ids=lambda c: c["segment"])
def test_name_segment_round_trip(case: dict[str, str]) -> None:
    assert encode_name_segment(case["token"]) == case["segment"]
    assert decode_segment(case["segment"]) == DecodedName(case["token"])


@pytest.mark.parametrize("case", VECTORS["invalidNameTokens"], ids=lambda c: c["error"])
def test_invalid_name_token(case: dict[str, str]) -> None:
    with pytest.raises(ValueError, match=case["error"]):
        encode_name_segment(case["token"])


@pytest.mark.parametrize("case", VECTORS["indexSegments"], ids=lambda c: c["segment"])
def test_index_segment_round_trip(case: dict[str, str | list[int]]) -> None:
    indices = case["indices"]
    assert isinstance(indices, list)
    assert encode_index_segment(indices) == case["segment"]
    assert decode_segment(str(case["segment"])) == DecodedIndex(tuple(indices))


def test_index_segment_rejects_non_integers() -> None:
    with pytest.raises(ValueError, match="index_invalid"):
        encode_index_segment([True])
    with pytest.raises(ValueError, match="index_empty"):
        encode_index_segment([])


@pytest.mark.parametrize("segment", VECTORS["invalidSegments"])
def test_invalid_segment(segment: str) -> None:
    with pytest.raises(ValueError, match="segment_invalid"):
        decode_segment(segment)


def test_name_and_index_segments_are_disjoint() -> None:
    for case in VECTORS["nameSegments"]:
        assert isinstance(decode_segment(case["segment"]), DecodedName)
    for case in VECTORS["indexSegments"]:
        assert isinstance(decode_segment(case["segment"]), DecodedIndex)


@pytest.mark.parametrize("case", VECTORS["defaults"], ids=lambda c: c["topic"])
def test_default_destinations(
    case: dict[str, str | list[dict[str, str | list[int]]]],
) -> None:
    tokens_raw = case["tokens"]
    client_id = case["clientId"]
    assert isinstance(tokens_raw, list) and isinstance(client_id, str)
    tokens = _tokens(tokens_raw)
    topic = default_topic(default_topic_prefix(client_id), tokens)
    assert topic == case["topic"]
    assert validate_topic(topic) is None
    assert default_metric_name(tokens) == case["metric"]
    assert validate_metric_name(default_metric_name(tokens)) is None
    decoded = [decode_segment(s) for s in topic.split("/")[2:]]
    expected = [
        DecodedIndex(t.indices) if isinstance(t, IndexToken) else DecodedName(t.name)
        for t in tokens
    ]
    assert decoded == expected


@pytest.mark.parametrize("case", VECTORS["invalidSources"], ids=lambda c: c["error"])
def test_invalid_sources(
    case: dict[str, str | list[dict[str, str | list[int]]]],
) -> None:
    tokens_raw = case["tokens"]
    assert isinstance(tokens_raw, list)
    with pytest.raises(ValueError, match=str(case["error"])):
        default_topic("p", _tokens(tokens_raw))
    with pytest.raises(ValueError, match=str(case["error"])):
        default_metric_name(_tokens(tokens_raw))


def _check(validator: Callable[[str], str | None], cases: list[dict[str, JsonValue]]) -> None:
    for case in cases:
        value = _value(case["value"])
        assert validator(value) == case["error"], repr(case["value"])


def test_topics() -> None:
    _check(validate_topic, VECTORS["topics"])


def test_metrics() -> None:
    _check(validate_metric_name, VECTORS["metrics"])


def test_node_ids() -> None:
    _check(validate_node_id, VECTORS["nodeIds"])


def test_client_ids() -> None:
    _check(validate_client_id, VECTORS["clientIds"])


@pytest.mark.parametrize("case", VECTORS["collisionKeys"], ids=lambda c: c["key"])
def test_collision_keys(case: dict[str, str]) -> None:
    assert collision_key(case["value"]) == case["key"]


@pytest.mark.parametrize("case", VECTORS["conflicts"], ids=lambda c: c["mode"])
def test_conflicts(
    case: dict[str, str | list[str] | list[dict[str, str | int]]],
) -> None:
    mode = case["mode"]
    destinations = case["destinations"]
    assert isinstance(mode, str) and isinstance(destinations, list)
    found = [
        {"code": c.code, "first": c.first, "second": c.second}
        for c in find_destination_conflicts(_MODES[mode], [str(d) for d in destinations])
    ]
    assert found == case["conflicts"]
