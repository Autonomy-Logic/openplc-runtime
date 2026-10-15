# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Interface between the connection state machine and a publication mode.

The supervisor owns the connection, the schedule and the retries; a strategy
only turns samples and inbound control messages into outbound messages. It
learns which messages the transport accepted or dropped through
``after_publish``, so it never treats a dropped message as sent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from mqtt_reader.reader import SampleResult


@dataclass(frozen=True)
class OutboundMessage:
    topic: str
    payload: bytes = field(repr=False)
    qos: Literal[0, 1]
    retain: bool
    key: int | None = None


@dataclass(frozen=True)
class InboundMessage:
    topic: str
    payload: bytes = field(repr=False)


@dataclass(frozen=True)
class PublishOutcome:
    """Accepted means handed to the transport, not delivered."""

    accepted: tuple[OutboundMessage, ...]
    dropped: tuple[OutboundMessage, ...]


class PublicationStrategy(Protocol):
    @property
    def tick_interval_ms(self) -> int:
        """Period of ``on_sample`` calls while connected."""

    def will(self) -> OutboundMessage | None:
        """Last will of the next connection attempt; asked again before every attempt."""

    def subscriptions(self) -> Sequence[tuple[str, int]]: ...

    def on_connected(self, sample: SampleResult, now_ms: int) -> Sequence[OutboundMessage]:
        """Messages that announce the current state of every leaf on a new connection."""

    def on_sample(self, sample: SampleResult, now_ms: int) -> Sequence[OutboundMessage]: ...

    def on_inbound(self, message: InboundMessage, now_ms: int) -> Sequence[OutboundMessage]: ...

    def after_publish(self, outcome: PublishOutcome) -> None: ...

    def on_disconnected(self) -> None:
        """The connection is gone; nothing sent on it may be assumed to have arrived."""

    def on_shutdown(self, now_ms: int) -> Sequence[OutboundMessage]:
        """Messages sent before an intentional disconnect."""
