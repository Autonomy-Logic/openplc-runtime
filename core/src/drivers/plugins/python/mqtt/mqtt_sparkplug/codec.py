# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Sparkplug B payloads of an Edge Node: NBIRTH, NDATA, NDEATH and the NCMD it accepts.

Metrics carry their full name and datatype (no aliases, templates or device
children). Values map from ``LeafValue.native`` as the contract value vectors
define: Int8/16/32 and UInt8/16/32 in ``int_value``, Int64/UInt64 in
``long_value``, both as two's complement; a null leaf is ``is_null`` with no
value field. Serialization is deterministic.

The only command accepted is a single ``Node Control/Rebirth`` Boolean metric
set to true; anything else is refused before it reaches any state. Inbound
payloads are bounded before they are parsed.

Imported while the plugin venv leads ``sys.path``; the pinned protobuf runtime
is checked first, so another copy fails the import instead of the wire.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from google import protobuf as _protobuf

PINNED_PROTOBUF: Final = "7.36.2"
if _protobuf.__version__ != PINNED_PROTOBUF:
    raise ImportError(f"protobuf {PINNED_PROTOBUF} is required, found {_protobuf.__version__}")

# pylint: disable=wrong-import-position
from contract.runtime_config import SPARKPLUG_DATATYPE, birth_properties
from google.protobuf.message import DecodeError
from mqtt_reader.values import LeafValue

from . import sparkplug_b_pb2 as pb

# pylint: enable=wrong-import-position

Payload = pb.Payload  # pylint: disable=no-member
Metric = Payload.Metric

NAMESPACE: Final = "spBv1.0"
BD_SEQ_METRIC: Final = "bdSeq"
REBIRTH_METRIC: Final = "Node Control/Rebirth"
MAX_CONTROL_BYTES: Final = 16_384
SEQ_MODULUS: Final = 256

DATATYPE_UNKNOWN: Final = 0
DATATYPE_INT64: Final = 4
DATATYPE_FLOAT: Final = 9
DATATYPE_DOUBLE: Final = 10
DATATYPE_BOOLEAN: Final = 11
DATATYPE_STRING: Final = 12
DATATYPE_BYTES: Final = 17
_INT32_TYPES: Final = frozenset({1, 2, 3, 5, 6, 7})
_INT64_TYPES: Final = frozenset({4, 8})
_MASK32: Final = 0xFFFF_FFFF
_MASK64: Final = 0xFFFF_FFFF_FFFF_FFFF

MessageKind = Literal["NBIRTH", "NDATA", "NDEATH", "NCMD"]
CommandRefusal = Literal[
    "oversized",
    "malformed",
    "not_a_single_metric",
    "unsupported_metric",
    "rebirth_not_true",
]


def node_topic(group_id: str, kind: MessageKind, edge_node_id: str) -> str:
    return f"{NAMESPACE}/{group_id}/{kind}/{edge_node_id}"


def _set_value(metric: Metric, value: LeafValue) -> None:
    datatype = SPARKPLUG_DATATYPE[value.iec_type]
    native = value.native
    if value.is_null:
        metric.is_null = True
    elif datatype == DATATYPE_BOOLEAN and isinstance(native, bool):
        metric.boolean_value = native
    elif isinstance(native, bool):
        raise ValueError("boolean value for a non-Boolean metric")
    elif datatype in _INT64_TYPES and isinstance(native, int):
        metric.long_value = native & _MASK64
    elif datatype in _INT32_TYPES and isinstance(native, int):
        metric.int_value = native & _MASK32
    elif datatype == DATATYPE_FLOAT and isinstance(native, float):
        metric.float_value = native
    elif datatype == DATATYPE_DOUBLE and isinstance(native, float):
        metric.double_value = native
    elif datatype == DATATYPE_STRING and isinstance(native, str):
        metric.string_value = native
    elif datatype == DATATYPE_BYTES and isinstance(native, bytes):
        metric.bytes_value = native
    else:
        raise ValueError("leaf value does not match its Sparkplug datatype")


def fill_leaf_metric(
    metric: Metric,
    name: str,
    value: LeafValue,
    *,
    birth: bool,
    timestamp_ms: int | None,
) -> None:
    """One leaf metric; ``birth`` adds the IEC type, encoding, unit and epoch properties."""
    metric.name = name
    if timestamp_ms is not None:
        metric.timestamp = timestamp_ms
    metric.datatype = SPARKPLUG_DATATYPE[value.iec_type]
    if birth:
        for key, text in birth_properties(value.iec_type):
            metric.properties.keys.append(key)
            prop = metric.properties.values.add()
            prop.type = DATATYPE_STRING
            prop.string_value = text
    _set_value(metric, value)


def leaf_metric(
    name: str, value: LeafValue, *, birth: bool, timestamp_ms: int | None = None
) -> bytes:
    metric = Metric()
    fill_leaf_metric(metric, name, value, birth=birth, timestamp_ms=timestamp_ms)
    return metric.SerializeToString(deterministic=True)


def _bd_seq_metric(payload: Payload, bd_seq: int, timestamp_ms: int | None) -> None:
    if not 0 <= bd_seq < SEQ_MODULUS:
        raise ValueError("bdSeq is outside 0-255")
    metric = payload.metrics.add()
    metric.name = BD_SEQ_METRIC
    if timestamp_ms is not None:
        metric.timestamp = timestamp_ms
    metric.datatype = DATATYPE_INT64
    metric.long_value = bd_seq


def encode_birth(
    bd_seq: int, names: Sequence[str], values: Sequence[LeafValue], timestamp_ms: int
) -> bytes:
    """Complete NBIRTH: seq 0, bdSeq, the rebirth control and every leaf with its properties."""
    if len(names) != len(values):
        raise ValueError("every leaf needs one value")
    payload = Payload()
    payload.timestamp = timestamp_ms
    payload.seq = 0
    _bd_seq_metric(payload, bd_seq, timestamp_ms)
    rebirth = payload.metrics.add()
    rebirth.name = REBIRTH_METRIC
    rebirth.timestamp = timestamp_ms
    rebirth.datatype = DATATYPE_BOOLEAN
    rebirth.boolean_value = False
    for name, value in zip(names, values):
        fill_leaf_metric(payload.metrics.add(), name, value, birth=True, timestamp_ms=timestamp_ms)
    return payload.SerializeToString(deterministic=True)


def encode_data(seq: int, changed: Sequence[tuple[str, LeafValue]], timestamp_ms: int) -> bytes:
    """NDATA with the changed leaves only, by name and datatype."""
    if not 0 <= seq < SEQ_MODULUS:
        raise ValueError("seq is outside 0-255")
    if not changed:
        raise ValueError("an NDATA message needs at least one metric")
    payload = Payload()
    payload.timestamp = timestamp_ms
    payload.seq = seq
    for name, value in changed:
        fill_leaf_metric(payload.metrics.add(), name, value, birth=False, timestamp_ms=timestamp_ms)
    return payload.SerializeToString(deterministic=True)


def encode_death(bd_seq: int, timestamp_ms: int | None) -> bytes:
    """NDEATH: only the bdSeq of the session it ends; no seq. The will carries no timestamp."""
    payload = Payload()
    if timestamp_ms is not None:
        payload.timestamp = timestamp_ms
    _bd_seq_metric(payload, bd_seq, None)
    return payload.SerializeToString(deterministic=True)


def parse_command(payload: bytes) -> CommandRefusal | None:
    """``None`` when ``payload`` is exactly a rebirth request, otherwise why it is refused."""
    if len(payload) > MAX_CONTROL_BYTES:
        return "oversized"
    try:
        message = Payload.FromString(payload)
    except DecodeError:
        return "malformed"
    if len(message.metrics) != 1:
        return "not_a_single_metric"
    metric = message.metrics[0]
    if (
        not metric.HasField("name")
        or metric.name != REBIRTH_METRIC
        or metric.datatype not in (DATATYPE_UNKNOWN, DATATYPE_BOOLEAN)
    ):
        return "unsupported_metric"
    if metric.is_null or metric.WhichOneof("value") != "boolean_value" or not metric.boolean_value:
        return "rebirth_not_true"
    return None


__all__ = [
    "BD_SEQ_METRIC",
    "MAX_CONTROL_BYTES",
    "NAMESPACE",
    "PINNED_PROTOBUF",
    "REBIRTH_METRIC",
    "SEQ_MODULUS",
    "encode_birth",
    "encode_data",
    "encode_death",
    "fill_leaf_metric",
    "leaf_metric",
    "node_topic",
    "parse_command",
]
