# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Generic MQTT publication: one topic per leaf carrying exactly ``{"value":...}``.

On a new connection every leaf is announced with its current value, null
included. ``periodic`` then republishes every leaf on each tick; ``on_change``
publishes a leaf only when its sampled identity (type plus source bytes)
differs from the last value the transport accepted for it, so 64-bit values
and -0.0 are compared without loss and with no deadband. A dropped message
keeps the old baseline and is retried on the next tick while it still
differs; a lost connection clears the baseline so the next session announces
the current state instead of replaying anything. QoS and retain come from the
configuration and apply to every message, including the announcement.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from contract.constants import MAX_LEAVES
from contract.names import find_destination_conflicts, validate_topic
from contract.runtime_config import GenericSettings, Leaf, MqttRuntimeConfig
from mqtt_reader.reader import SampleResult
from mqtt_reader.values import LeafValue

from .status import StatusWriter
from .strategy import InboundMessage, OutboundMessage, PublishOutcome

ENVELOPE_PREFIX: Final = b'{"value":'
ENVELOPE_SUFFIX: Final = b"}"
NULL_JSON: Final = b"null"

Identity = tuple[str, bytes | None]


def envelope(value: LeafValue) -> bytes:
    """Exact generic payload of one sampled value."""
    body = NULL_JSON if value.json_text is None else value.json_text.encode("utf-8")
    return ENVELOPE_PREFIX + body + ENVELOPE_SUFFIX


def _check_selection(config: MqttRuntimeConfig) -> GenericSettings:
    if config.mode != "generic" or config.generic is None:
        raise ValueError("the generic strategy needs a generic configuration")
    leaves = config.leaves
    if not 1 <= len(leaves) <= MAX_LEAVES:
        raise ValueError("leaf count is outside the contract limits")
    destinations = [leaf.destination for leaf in leaves]
    if any(validate_topic(topic) is not None for topic in destinations):
        raise ValueError("a destination is not a valid topic")
    if find_destination_conflicts("generic", destinations):
        raise ValueError("two leaves publish to the same topic")
    return config.generic


class GenericStrategy:
    """Publication strategy of the ``generic`` mode."""

    def __init__(self, config: MqttRuntimeConfig) -> None:
        self._settings = _check_selection(config)
        self._leaves: tuple[Leaf, ...] = config.leaves
        self._baseline: list[Identity | None] = [None] * len(self._leaves)
        self._pending: dict[int, Identity] = {}

    @property
    def tick_interval_ms(self) -> int:
        return self._settings.interval_ms

    @property
    def baseline(self) -> tuple[Identity | None, ...]:
        return tuple(self._baseline)

    def will(self) -> OutboundMessage | None:
        return None

    def subscriptions(self) -> Sequence[tuple[str, int]]:
        return ()

    def _values(self, sample: SampleResult) -> tuple[LeafValue, ...]:
        values = sample.values
        if len(values) != len(self._leaves):
            raise ValueError("sample does not match the configured leaves")
        for leaf, value in zip(self._leaves, values):
            if leaf.iec_type != value.iec_type:
                raise ValueError("sampled type differs from the configured leaf type")
        return values

    def _messages(self, sample: SampleResult, changed_only: bool) -> list[OutboundMessage]:
        settings = self._settings
        messages: list[OutboundMessage] = []
        self._pending.clear()
        for index, value in enumerate(self._values(sample)):
            identity = value.identity
            if changed_only and self._baseline[index] == identity:
                continue
            self._pending[index] = identity
            messages.append(
                OutboundMessage(
                    self._leaves[index].destination,
                    envelope(value),
                    settings.qos,
                    settings.retain,
                    index,
                )
            )
        return messages

    def on_connected(self, sample: SampleResult, now_ms: int) -> Sequence[OutboundMessage]:
        return self._messages(sample, changed_only=False)

    def on_sample(self, sample: SampleResult, now_ms: int) -> Sequence[OutboundMessage]:
        return self._messages(sample, changed_only=self._settings.publish_mode == "on_change")

    def on_inbound(self, message: InboundMessage, now_ms: int) -> Sequence[OutboundMessage]:
        return ()

    def after_publish(self, outcome: PublishOutcome) -> None:
        for message in outcome.accepted:
            if message.key is not None and message.key in self._pending:
                self._baseline[message.key] = self._pending[message.key]
        self._pending.clear()

    def on_disconnected(self) -> None:
        self._baseline = [None] * len(self._leaves)
        self._pending.clear()

    def on_shutdown(self, now_ms: int) -> Sequence[OutboundMessage]:
        return ()


def build_generic_strategy(config: MqttRuntimeConfig, _status: StatusWriter) -> GenericStrategy:
    return GenericStrategy(config)


__all__ = ["GenericStrategy", "build_generic_strategy", "envelope"]
