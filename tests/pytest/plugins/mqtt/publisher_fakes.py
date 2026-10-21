# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Controlled clock, scripted network and recording strategy for supervisor tests."""

from __future__ import annotations

import struct
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from contract.constants import MAX_PAYLOAD_BYTES
from mqtt_publisher.connection import (
    ConnectSpec,
    Drained,
    PublishResult,
    TransportCancelled,
    TransportError,
    transport_error,
)
from mqtt_publisher.strategy import InboundMessage, OutboundMessage, PublishOutcome
from mqtt_reader.reader import LeafIssue, SampleResult
from mqtt_reader.values import LeafValue, convert_window, null_value

WALL_BASE_MS = 1_791_547_200_000


class FakeClock:
    """Time moves only when the code under test waits or services the network."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._hooks: list[tuple[float, Callable[[], None]]] = []

    def monotonic(self) -> float:
        return self.now

    def wall_ms(self) -> int:
        return WALL_BASE_MS + round(self.now * 1000)

    def at(self, when: float, action: Callable[[], None]) -> None:
        self._hooks.append((when, action))
        self._hooks.sort(key=lambda item: item[0])

    def advance(self, seconds: float) -> None:
        target = self.now + max(0.0, seconds)
        while self._hooks and self._hooks[0][0] <= target:
            when, action = self._hooks.pop(0)
            self.now = max(self.now, when)
            action()
        self.now = target

    def wait(self, event: threading.Event, timeout_s: float) -> bool:
        if event.is_set():
            return True
        self.sleeps.append(timeout_s)
        end = self.now + max(0.0, timeout_s)
        while not event.is_set() and self._hooks and self._hooks[0][0] <= end:
            when, action = self._hooks.pop(0)
            self.now = max(self.now, when)
            action()
        if event.is_set():
            return True
        self.now = end
        return False


class RealTime:
    """Lets ``FakeNetwork`` run under a real publisher thread: servicing takes real time."""

    def advance(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


@dataclass
class FakeSession:
    network: FakeNetwork
    spec: ConnectSpec
    opened: bool = False
    closed: bool = False
    aborted: bool = False
    pending: list[OutboundMessage] = field(default_factory=list)
    sent: list[OutboundMessage] = field(default_factory=list)
    final: list[OutboundMessage] = field(default_factory=list)
    subscriptions: list[tuple[str, int]] = field(default_factory=list)
    inbound: deque[InboundMessage] = field(default_factory=deque)
    forwarded: int = 0
    lost: TransportError | None = None
    services: int = 0

    def open(self, cancel: threading.Event, timeout_s: float) -> None:
        self.network.open_timeouts.append(timeout_s)
        script = self.network.open_script.popleft() if self.network.open_script else None
        if script in ("cancel", "fail_cancel"):
            cancel.set()
        if script == "fail_cancel":
            raise transport_error("network_connect_timeout")
        if cancel.is_set():
            raise TransportCancelled()
        if isinstance(script, TransportError):
            raise script
        if not self.network.up:
            raise transport_error("network_connection_refused")
        self.opened = True

    def subscribe(self, topic: str, qos: int) -> None:
        self.subscriptions.append((topic, qos))

    def publish(self, message: OutboundMessage) -> PublishResult:
        if self.closed or self.lost is not None:
            return PublishResult.NOT_CONNECTED
        if len(message.payload) > MAX_PAYLOAD_BYTES:
            return PublishResult.TOO_LARGE
        if self.backlog_full():
            return PublishResult.BACKLOG_FULL
        self.pending.append(message)
        self.sent.append(message)
        return PublishResult.ACCEPTED

    def service(self, timeout_s: float) -> None:
        self.services += 1
        self.network.clock.advance(timeout_s * self.network.service_stretch)
        if self.lost is None and self.network.kills_left > 0:
            self.network.kills_left -= 1
            self.lost = transport_error("network_connection_lost")
        if self.lost is not None:
            raise self.lost
        if not self.network.up:
            self.lost = transport_error("network_connection_lost")
            raise self.lost
        if self.network.ack:
            self.forwarded += len(self.pending)
            self.network.delivered.extend(self.pending)
            self.pending.clear()

    def backlog_full(self) -> bool:
        return len(self.pending) >= self.network.capacity

    def drain(self) -> Drained:
        drained = Drained(self.forwarded, tuple(self.inbound), 0)
        self.forwarded = 0
        self.inbound.clear()
        return drained

    def close(self, final: Sequence[OutboundMessage], timeout_s: float) -> int:
        self.network.close_timeouts.append(timeout_s)
        self.final = list(final)
        if self.network.ack:
            self.forwarded += len(self.pending)
            self.network.delivered.extend(self.pending)
            self.network.delivered.extend(final)
            self.pending.clear()
        self.closed = True
        lost = len(self.pending)
        self.pending.clear()
        return lost

    def abort(self) -> int:
        self.aborted = True
        self.closed = True
        lost = len(self.pending)
        self.pending.clear()
        return lost


class FakeNetwork:
    def __init__(self, clock: FakeClock | RealTime) -> None:
        self.clock = clock
        self.up = True
        self.ack = True
        self.capacity = 1_000
        self.service_stretch = 1.0
        self.kills_left = 0
        self.open_script: deque[TransportError | str | None] = deque()
        self.sessions: list[FakeSession] = []
        self.delivered: list[OutboundMessage] = []
        self.open_timeouts: list[float] = []
        self.close_timeouts: list[float] = []

    def factory(self, spec: ConnectSpec) -> FakeSession:
        session = FakeSession(self, spec)
        self.sessions.append(session)
        return session

    @property
    def opened(self) -> list[FakeSession]:
        return [session for session in self.sessions if session.opened]


def dint(value: int) -> LeafValue:
    return convert_window("DINT", struct.pack("<i", value))


class CounterSampler:
    """Leaves whose values a test sets; counts samples."""

    def __init__(self, values: Sequence[int]) -> None:
        self.values = list(values)
        self.samples = 0
        self.null_at: set[int] = set()

    def sample(self) -> SampleResult:
        self.samples += 1
        values: list[LeafValue] = []
        issues: list[LeafIssue] = []
        for index, value in enumerate(self.values):
            if index in self.null_at:
                leaf = null_value("DINT", "read_failed", "no_value")
                assert leaf.issue is not None
                issues.append(LeafIssue(index, leaf.issue))
            else:
                leaf = dint(value)
            values.append(leaf)
        return SampleResult(tuple(values), tuple(issues))


class RecordingStrategy:
    """Publishes every leaf on connect and every tick; records what the supervisor reported."""

    def __init__(
        self, interval_ms: int = 100, will: OutboundMessage | None = None, qos: int = 0
    ) -> None:
        self._interval = interval_ms
        self._will = will
        self._qos = qos
        self.wills = 0
        self.outcomes: list[PublishOutcome] = []
        self.disconnects = 0
        self.connected_samples: list[SampleResult] = []
        self.inbound: list[InboundMessage] = []
        self.shutdowns = 0
        self.payload_size: int | None = None
        self.fail_on_sample = False

    @property
    def tick_interval_ms(self) -> int:
        return self._interval

    def will(self) -> OutboundMessage | None:
        self.wills += 1
        return self._will

    def subscriptions(self) -> Sequence[tuple[str, int]]:
        return (("ctl/rebirth", 0),)

    def _messages(self, sample: SampleResult) -> list[OutboundMessage]:
        out = []
        for index, value in enumerate(sample.values):
            payload = (value.json_text or "null").encode()
            if self.payload_size is not None:
                payload = payload.ljust(self.payload_size, b" ")
            out.append(OutboundMessage(f"v/{index}", payload, self._qos, False, index))  # type: ignore[arg-type]
        return out

    def on_connected(self, sample: SampleResult, now_ms: int) -> Sequence[OutboundMessage]:
        self.connected_samples.append(sample)
        return self._messages(sample)

    def on_sample(self, sample: SampleResult, now_ms: int) -> Sequence[OutboundMessage]:
        if self.fail_on_sample:
            raise RuntimeError("strategy bug")
        return self._messages(sample)

    def on_inbound(self, message: InboundMessage, now_ms: int) -> Sequence[OutboundMessage]:
        self.inbound.append(message)
        return (OutboundMessage("ctl/ack", b"1", 0, False),)

    def after_publish(self, outcome: PublishOutcome) -> None:
        self.outcomes.append(outcome)

    def on_disconnected(self) -> None:
        self.disconnects += 1

    def on_shutdown(self, now_ms: int) -> Sequence[OutboundMessage]:
        self.shutdowns += 1
        return (OutboundMessage("state", b"offline", 1, False),)
