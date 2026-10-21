# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Sparkplug B Edge Node publication over the supervisor's single connection.

Every connection attempt takes the next process-wide ``bdSeq`` (0-255) and
registers an NDEATH carrying it as the will. Once connected the node publishes
a complete NBIRTH (seq 0) and then NDATA messages with the leaves whose sampled
identity differs from the last value the transport accepted, seq 1-255 then 0.
Nothing is published as NDATA until the NBIRTH of the session was accepted; a
dropped message keeps seq and baseline, so the wire never shows a gap. The
only subscription is the node's own NCMD topic and the only command honoured
is ``Node Control/Rebirth``: requests set one pending flag, answered with a new
NBIRTH on the next sample at least 2 s after the previous birth, on the same
connection and bdSeq. Stopping publishes NDEATH with the session's bdSeq.
Sparkplug messages use the protocol's QoS and retain, never the generic ones.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from contract.constants import (
    IDENTICAL_ERROR_LOG_INTERVAL_MS,
    MAX_LEAVES,
    MAX_PAYLOAD_BYTES,
)
from contract.names import (
    find_destination_conflicts,
    validate_metric_name,
    validate_node_id,
    validate_topic,
)
from contract.runtime_config import (
    SPARKPLUG_DATATYPE,
    Leaf,
    MqttRuntimeConfig,
    SparkplugSettings,
    sparkplug_birth_upper_bound,
)
from mqtt_reader.reader import SampleResult
from mqtt_reader.values import LeafValue
from mqtt_sparkplug.codec import (
    SEQ_MODULUS,
    encode_birth,
    encode_data,
    encode_death,
    node_topic,
    parse_command,
)

from .status import LogLimiter, StatusWriter
from .strategy import InboundMessage, OutboundMessage, PublishOutcome

DATA_QOS: Final = 0
DEATH_QOS: Final = 1
NCMD_QOS: Final = 0
REBIRTH_MIN_INTERVAL_S: Final = 2.0

Identity = tuple[str, bytes | None]
Log = Callable[[str, str], None]


class BdSeqCounter:  # pylint: disable=too-few-public-methods
    """Birth/death sequence of this process: the next CONNECT gets the next value, 0-255."""

    def __init__(self, start: int = 0) -> None:
        if not 0 <= start < SEQ_MODULUS:
            raise ValueError("bdSeq starts within 0-255")
        self._lock = threading.Lock()
        self._next = start

    def take(self) -> int:
        with self._lock:
            value = self._next
            self._next = (value + 1) % SEQ_MODULUS
            return value


PROCESS_BD_SEQ: Final = BdSeqCounter()


@dataclass(frozen=True)
class _Topics:
    birth: str
    data: str
    death: str
    command: str


@dataclass(frozen=True)
class _Pending:
    message: OutboundMessage
    birth: bool
    seq: int
    identities: tuple[tuple[int, Identity], ...]
    timestamp_ms: int


def _check_selection(config: MqttRuntimeConfig) -> SparkplugSettings:
    if config.mode != "sparkplug_b" or config.sparkplug is None:
        raise ValueError("the Sparkplug strategy needs a Sparkplug configuration")
    settings = config.sparkplug
    if validate_node_id(settings.group_id) or validate_node_id(settings.edge_node_id):
        raise ValueError("invalid Sparkplug Group ID or Edge Node ID")
    leaves = config.leaves
    if not 1 <= len(leaves) <= MAX_LEAVES:
        raise ValueError("leaf count is outside the contract limits")
    names = [leaf.destination for leaf in leaves]
    if any(validate_metric_name(name) is not None for name in names):
        raise ValueError("a destination is not a valid metric name")
    if find_destination_conflicts("sparkplug_b", names):
        raise ValueError("two leaves publish the same metric")
    if any(leaf.iec_type not in SPARKPLUG_DATATYPE for leaf in leaves):
        raise ValueError("a leaf type has no Sparkplug datatype")
    if sparkplug_birth_upper_bound(leaves) > MAX_PAYLOAD_BYTES:
        raise ValueError("the complete NBIRTH would exceed the payload limit")
    return settings


class SparkplugStrategy:
    """Publication strategy of the ``sparkplug_b`` mode."""

    # pylint: disable=too-many-instance-attributes,too-many-arguments
    def __init__(
        self,
        config: MqttRuntimeConfig,
        status: StatusWriter | None = None,
        *,
        bd_seq: BdSeqCounter = PROCESS_BD_SEQ,
        log: Log | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = _check_selection(config)
        group, edge = self._settings.group_id, self._settings.edge_node_id
        self._topics = _Topics(
            node_topic(group, "NBIRTH", edge),
            node_topic(group, "NDATA", edge),
            node_topic(group, "NDEATH", edge),
            node_topic(group, "NCMD", edge),
        )
        if any(validate_topic(topic) is not None for topic in vars(self._topics).values()):
            raise ValueError("the Sparkplug topics are not valid MQTT topics")
        self._leaves: tuple[Leaf, ...] = config.leaves
        self._names = tuple(leaf.destination for leaf in config.leaves)
        self._status = status
        self._counter = bd_seq
        self._log = log
        self._monotonic = monotonic
        self._limiter = LogLimiter(IDENTICAL_ERROR_LOG_INTERVAL_MS / 1000)
        self._bd_seq: int | None = None
        self._seq = 0
        self._born = False
        self._baseline: list[Identity | None] = [None] * len(self._leaves)
        self._pending: _Pending | None = None
        self._rebirth_requested = False
        self._last_birth_at: float | None = None
        self._last_birth_ms: int | None = None
        self.births = 0
        self.rebirths_requested = 0
        self.commands_refused = 0

    @property
    def tick_interval_ms(self) -> int:
        return self._settings.sample_interval_ms

    @property
    def bd_seq(self) -> int | None:
        return self._bd_seq

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def command_topic(self) -> str:
        return self._topics.command

    def _report(self) -> None:
        if self._status is not None and self._bd_seq is not None:
            self._status.sparkplug(self._bd_seq, self._seq, self._last_birth_ms)

    def _note(self, level: str, line: str) -> None:
        if self._log is not None and self._limiter.allow(line, self._monotonic()):
            self._log(level, line)

    def _reset_session(self) -> None:
        self._seq = 0
        self._born = False
        self._baseline = [None] * len(self._leaves)
        self._pending = None
        self._rebirth_requested = False

    def _death(self, timestamp_ms: int | None) -> OutboundMessage:
        assert self._bd_seq is not None
        return OutboundMessage(
            self._topics.death, encode_death(self._bd_seq, timestamp_ms), DEATH_QOS, False
        )

    def will(self) -> OutboundMessage | None:
        self._bd_seq = self._counter.take()
        self._reset_session()
        self._report()
        return self._death(None)

    def subscriptions(self) -> Sequence[tuple[str, int]]:
        return ((self._topics.command, NCMD_QOS),)

    def _values(self, sample: SampleResult) -> tuple[LeafValue, ...]:
        values = sample.values
        if len(values) != len(self._leaves):
            raise ValueError("sample does not match the configured leaves")
        for leaf, value in zip(self._leaves, values):
            if leaf.iec_type != value.iec_type:
                raise ValueError("sampled type differs from the configured leaf type")
        return values

    def _birth(self, values: Sequence[LeafValue], now_ms: int) -> list[OutboundMessage]:
        if self._bd_seq is None:
            raise ValueError("no connection attempt registered a bdSeq")
        payload = encode_birth(self._bd_seq, self._names, values, now_ms)
        message = OutboundMessage(self._topics.birth, payload, DATA_QOS, False)
        identities = tuple((index, value.identity) for index, value in enumerate(values))
        self._pending = _Pending(message, True, 0, identities, now_ms)
        return [message]

    def _data(self, values: Sequence[LeafValue], now_ms: int) -> list[OutboundMessage]:
        changed = [
            (index, value)
            for index, value in enumerate(values)
            if self._baseline[index] != value.identity
        ]
        if not changed:
            self._pending = None
            return []
        seq = (self._seq + 1) % SEQ_MODULUS
        payload = encode_data(seq, [(self._names[i], value) for i, value in changed], now_ms)
        message = OutboundMessage(self._topics.data, payload, DATA_QOS, False)
        identities = tuple((index, value.identity) for index, value in changed)
        self._pending = _Pending(message, False, seq, identities, now_ms)
        return [message]

    def _rebirth_due(self) -> bool:
        last = self._last_birth_at
        return last is None or self._monotonic() - last >= REBIRTH_MIN_INTERVAL_S

    def on_connected(self, sample: SampleResult, now_ms: int) -> Sequence[OutboundMessage]:
        return self._birth(self._values(sample), now_ms)

    def on_sample(self, sample: SampleResult, now_ms: int) -> Sequence[OutboundMessage]:
        values = self._values(sample)
        if not self._born or (self._rebirth_requested and self._rebirth_due()):
            return self._birth(values, now_ms)
        return self._data(values, now_ms)

    def on_inbound(self, message: InboundMessage, now_ms: int) -> Sequence[OutboundMessage]:
        refusal = (
            "unexpected_topic"
            if message.topic != self._topics.command
            else parse_command(message.payload)
        )
        if refusal is not None:
            self.commands_refused += 1
            self._note(
                "warning",
                f"control_refused: a Sparkplug command was ignored ({refusal}); "
                "only Node Control/Rebirth is accepted",
            )
            return ()
        self.rebirths_requested += 1
        self._rebirth_requested = True
        self._note("info", "rebirth requested through NCMD")
        return ()

    def after_publish(self, outcome: PublishOutcome) -> None:
        pending, self._pending = self._pending, None
        if pending is None or not any(m is pending.message for m in outcome.accepted):
            return
        for index, identity in pending.identities:
            self._baseline[index] = identity
        self._seq = pending.seq
        if pending.birth:
            self._born = True
            self._rebirth_requested = False
            self._last_birth_at = self._monotonic()
            self._last_birth_ms = pending.timestamp_ms
            self.births += 1
        self._report()

    def on_disconnected(self) -> None:
        self._reset_session()

    def on_shutdown(self, now_ms: int) -> Sequence[OutboundMessage]:
        if self._bd_seq is None:
            return ()
        return (self._death(now_ms),)


def build_sparkplug_strategy(
    config: MqttRuntimeConfig, status: StatusWriter, log: Log | None = None
) -> SparkplugStrategy:
    return SparkplugStrategy(config, status, log=log)


__all__ = [
    "PROCESS_BD_SEQ",
    "BdSeqCounter",
    "SparkplugStrategy",
    "build_sparkplug_strategy",
]
