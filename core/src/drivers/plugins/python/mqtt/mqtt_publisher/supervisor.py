# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""The integration's only connection owner and retry authority.

One ``Supervisor`` runs per active configuration, on one ``Publisher``
thread, never on the PLC scan. It connects, announces the current state of
every leaf, samples on a monotonic schedule and hands messages to the
transport; on any failure it drops the session (nothing is replayed), waits
the backoff delay and starts over with a new session. Stopping sets the
cancel event, which every wait observes within ``service_slice_s``.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol

from contract.constants import (
    CONNECT_TIMEOUT_MS,
    IDENTICAL_ERROR_LOG_INTERVAL_MS,
    MAX_PAYLOAD_BYTES,
    SHUTDOWN_BUDGET_MS,
)
from mqtt_reader.reader import SampleResult

from .backoff import Backoff
from .clock import Clock
from .connection import (
    Connection,
    ConnectionFactory,
    ConnectSpec,
    PublishResult,
    TransportCancelled,
    TransportError,
)
from .status import LogLimiter, StatusWriter
from .strategy import OutboundMessage, PublicationStrategy, PublishOutcome

PACKET_LIMIT_SESSIONS: Final = 2
PAYLOAD_TOO_LARGE_MESSAGE: Final = (
    f"a message above the {MAX_PAYLOAD_BYTES}-byte payload limit was not sent"
)
INTERNAL_ERROR_MESSAGE: Final = "the publisher stopped after an internal error"


class Sampler(Protocol):  # pylint: disable=too-few-public-methods
    def sample(self) -> SampleResult: ...


class Logger(Protocol):  # pylint: disable=too-few-public-methods
    def __call__(self, level: str, message: str) -> None: ...


@dataclass(frozen=True)
class SupervisorSettings:
    tick_interval_s: float
    connect_timeout_s: float = CONNECT_TIMEOUT_MS / 1000
    shutdown_budget_s: float = SHUTDOWN_BUDGET_MS / 1000
    close_timeout_s: float = 2.0
    service_slice_s: float = 0.1
    packet_limit_window_s: float = 1.0
    log_interval_s: float = IDENTICAL_ERROR_LOG_INTERVAL_MS / 1000

    def __post_init__(self) -> None:
        if self.tick_interval_s <= 0 or self.service_slice_s <= 0:
            raise ValueError("intervals must be positive")
        if self.close_timeout_s >= self.shutdown_budget_s:
            raise ValueError("the close timeout must leave room inside the shutdown budget")


def packet_limit_message(largest_payload: int) -> str:
    return (
        "the broker closed the connection right after the first messages of "
        f"{PACKET_LIMIT_SESSIONS} consecutive sessions (largest payload {largest_payload} bytes); "
        "its maximum packet size may be lower"
    )


@dataclass
class _Session:
    connection: Connection
    announced_at: float | None = None
    largest_announced: int = 0


class Supervisor:
    # pylint: disable=too-many-instance-attributes,too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        spec: ConnectSpec,
        factory: ConnectionFactory,
        strategy: PublicationStrategy,
        sampler: Sampler,
        status: StatusWriter,
        clock: Clock,
        backoff: Backoff,
        settings: SupervisorSettings,
        log: Logger,
    ) -> None:
        self._spec = spec
        self._factory = factory
        self._strategy = strategy
        self._sampler = sampler
        self._status = status
        self._clock = clock
        self._backoff = backoff
        self._settings = settings
        self._log = log
        self._limiter = LogLimiter(settings.log_interval_s)
        self._suspect_sessions = 0
        self.sessions = 0
        self.ticks_skipped = 0

    @property
    def settings(self) -> SupervisorSettings:
        return self._settings

    def run(self, cancel: threading.Event) -> None:
        try:
            self._run(cancel)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            self._status.failed("internal_error", INTERNAL_ERROR_MESSAGE)
            self._log("error", f"internal_error: {INTERNAL_ERROR_MESSAGE} ({type(exc).__name__})")

    def _run(self, cancel: threading.Event) -> None:
        while not cancel.is_set():
            error = self._attempt(cancel)
            if error is None or not self._wait_retry(cancel, error):
                return

    def _report(self, level: str, code: str, message: str) -> None:
        line = f"{code}: {message}"
        if self._limiter.allow(line, self._clock.monotonic()):
            self._log(level, line)

    def _attempt(self, cancel: threading.Event) -> TransportError | None:
        spec = replace(self._spec, will=self._strategy.will())
        self._status.connecting()
        connection = self._factory(spec)
        try:
            connection.open(cancel, self._settings.connect_timeout_s)
        except TransportCancelled:
            connection.abort()
            return None
        except TransportError as error:
            connection.abort()
            self._suspect_sessions = 0
            return None if cancel.is_set() else error
        return self._serve(_Session(connection), cancel)

    def _serve(self, session: _Session, cancel: threading.Event) -> TransportError | None:
        connection = session.connection
        self.sessions += 1
        self._backoff.connected(self._clock.monotonic())
        self._status.connected()
        self._log("info", f"connected (session {self.sessions})")
        interval = self._settings.tick_interval_s
        try:
            if not cancel.is_set():
                for topic, qos in self._strategy.subscriptions():
                    connection.subscribe(topic, qos)
                announce = self._strategy.on_connected(self._sample(), self._clock.wall_ms())
                self._publish(connection, announce)
                session.announced_at = self._clock.monotonic()
                session.largest_announced = max((len(m.payload) for m in announce), default=0)
            next_tick = self._clock.monotonic() + interval
            while not cancel.is_set():
                remaining = next_tick - self._clock.monotonic()
                connection.service(max(0.0, min(self._settings.service_slice_s, remaining)))
                self._absorb(connection, cancel)
                now = self._clock.monotonic()
                if now >= next_tick and not cancel.is_set():
                    missed = int((now - next_tick) // interval)
                    self.ticks_skipped += missed
                    next_tick += (missed + 1) * interval
                    self._tick(connection)
        except TransportError as error:
            return self._lost(session, error)
        self._shutdown(connection)
        return None

    def _sample(self) -> SampleResult:
        result = self._sampler.sample()
        self._status.sampled(result.read_errors)
        if result.issues:
            issue = result.issues[0].issue
            self._status.issue(issue.code, issue.message)
            self._report("warning", issue.code, f"{issue.message} (leaf values published as null)")
        else:
            self._status.clear_read_issue()
        return result

    def _tick(self, connection: Connection) -> None:
        if connection.backlog_full():
            self.ticks_skipped += 1
            return
        self._publish(connection, self._strategy.on_sample(self._sample(), self._clock.wall_ms()))

    def _publish(self, connection: Connection, messages: Sequence[OutboundMessage]) -> None:
        if not messages:
            return
        accepted: list[OutboundMessage] = []
        dropped: list[OutboundMessage] = []
        for message in messages:
            result = connection.publish(message)
            if result is PublishResult.ACCEPTED:
                accepted.append(message)
                continue
            dropped.append(message)
            if result is PublishResult.TOO_LARGE:
                self._status.issue("payload_too_large", PAYLOAD_TOO_LARGE_MESSAGE)
                self._report("error", "payload_too_large", PAYLOAD_TOO_LARGE_MESSAGE)
        self._strategy.after_publish(PublishOutcome(tuple(accepted), tuple(dropped)))
        self._status.dropped(len(dropped))

    def _absorb(self, connection: Connection, cancel: threading.Event) -> None:
        drained = connection.drain()
        self._status.forwarded(drained.forwarded)
        if drained.inbound_dropped:
            self._report(
                "warning", "control_rate_limited", "inbound messages above the limit dropped"
            )
        for message in drained.inbound:
            if cancel.is_set():
                return
            self._publish(connection, self._strategy.on_inbound(message, self._clock.wall_ms()))

    def _lost(self, session: _Session, error: TransportError) -> TransportError:
        now = self._clock.monotonic()
        connection = session.connection
        self._status.forwarded(connection.drain().forwarded)
        self._status.dropped(connection.abort())
        self._strategy.on_disconnected()
        self._backoff.disconnected(now)
        early = (
            session.announced_at is not None
            and now - session.announced_at <= self._settings.packet_limit_window_s
        )
        if error.code != "network_connection_lost" or not early:
            self._suspect_sessions = 0
            return error
        self._suspect_sessions += 1
        if self._suspect_sessions >= PACKET_LIMIT_SESSIONS:
            return TransportError(
                "broker_packet_limit", packet_limit_message(session.largest_announced)
            )
        return error

    def _shutdown(self, connection: Connection) -> None:
        final = self._strategy.on_shutdown(self._clock.wall_ms())
        lost = connection.close(final, self._settings.close_timeout_s)
        self._status.forwarded(connection.drain().forwarded)
        self._status.dropped(lost)
        self._strategy.on_disconnected()

    def _wait_retry(self, cancel: threading.Event, error: TransportError) -> bool:
        delay = self._backoff.next_delay()
        deadline = self._clock.monotonic() + delay
        self._status.retry(error.code, error.message, self._backoff.attempt, deadline)
        self._report("warning", error.code, error.message)
        while not cancel.is_set():
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                return True
            if self._clock.wait(cancel, remaining):
                return False
        return False


class Publisher:
    """Runs one supervisor on its own thread; ``stop`` joins it within the shutdown budget."""

    def __init__(self, supervisor: Supervisor, name: str) -> None:
        self._supervisor = supervisor
        self._cancel = threading.Event()
        self._thread = threading.Thread(
            target=supervisor.run, args=(self._cancel,), name=name, daemon=True
        )

    @property
    def supervisor(self) -> Supervisor:
        return self._supervisor

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout_s: float | None = None) -> bool:
        """Cancel and join; True when the thread ended within ``timeout_s``."""
        budget = self._supervisor.settings.shutdown_budget_s if timeout_s is None else timeout_s
        self._cancel.set()
        if self._thread.ident is not None:
            self._thread.join(budget)
        return not self._thread.is_alive()
