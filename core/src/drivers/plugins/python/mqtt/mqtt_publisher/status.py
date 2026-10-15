# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Single authority over the integration state reported to the runtime.

``StatusTracker`` holds the state, the current sanitized error and the
counters; ``snapshot`` returns the ``status`` document of
``mqtt-api.v1.schema.json``. Every publisher writes through a ``StatusWriter``
bound to an epoch: starting, stopping or replacing the integration opens a new
epoch, so anything a previous publisher reports afterwards is ignored.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field
from typing import Final, Literal

from contract.runtime_config import (
    CONFIG_ERROR_CODES,
    ERROR_CATEGORY,
    NON_CONFIG_ERROR_CATEGORY,
    MqttRuntimeConfig,
)

from .clock import Clock

State = Literal[
    "disabled",
    "plc_stopped",
    "connecting",
    "connected",
    "retry_wait",
    "config_error",
    "credential_error",
]
StatusDocument = dict[str, object]

STATUS_CONTRACT: Final = "openplc-mqtt-status"
STATUS_VERSION: Final = 1
MAX_MESSAGE_CHARS: Final = 512

TRANSPORT_ERROR_CATEGORY: Final[dict[str, str]] = {
    "network_connect_timeout": "network",
    "network_connection_lost": "network",
    "network_connection_refused": "network",
    "network_dns_failure": "network",
    "network_keepalive_timeout": "network",
    "network_unreachable": "network",
    "auth_broker_unavailable": "authentication",
    "auth_client_id_rejected": "authentication",
    "auth_rejected": "authentication",
    "broker_certificate_expired": "certificate",
    "broker_certificate_hostname_mismatch": "certificate",
    "broker_certificate_invalid": "certificate",
    "broker_certificate_untrusted": "certificate",
    "client_certificate_expired": "certificate",
    "client_certificate_rejected": "certificate",
    "tls_protocol_unsupported": "certificate",
    "broker_packet_limit": "limit",
    "inbound_packet_too_large": "limit",
    "payload_too_large": "limit",
    "read_failed": "read_conversion",
    "conversion_failed": "read_conversion",
    "string_exceeds_read_window": "read_conversion",
    "internal_error": "internal",
}
_CREDENTIAL_STATE_CATEGORIES: Final = frozenset({"credential", "certificate"})


def category_of(code: str) -> str:
    if code in TRANSPORT_ERROR_CATEGORY:
        return TRANSPORT_ERROR_CATEGORY[code]
    if code in NON_CONFIG_ERROR_CATEGORY:
        return NON_CONFIG_ERROR_CATEGORY[code]
    if code in CONFIG_ERROR_CODES:
        return ERROR_CATEGORY
    return "internal"


def activation_state(code: str) -> State:
    """State for an activation that failed with ``code``."""
    return (
        "credential_error" if category_of(code) in _CREDENTIAL_STATE_CATEGORIES else "config_error"
    )


def iso_ms(wall_ms: int) -> str:
    moment = dt.datetime.fromtimestamp(wall_ms // 1000, dt.timezone.utc)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{wall_ms % 1000:03d}Z"


class LogLimiter:  # pylint: disable=too-few-public-methods
    """Lets an identical line through at most once per interval."""

    def __init__(self, interval_s: float, max_keys: int = 64) -> None:
        self._interval = interval_s
        self._max_keys = max_keys
        self._last: dict[str, float] = {}

    def allow(self, key: str, now: float) -> bool:
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return False
        if last is None and len(self._last) >= self._max_keys:
            oldest = min(self._last, key=self._last.__getitem__)
            del self._last[oldest]
        self._last[key] = now
        return True


@dataclass
class _Error:
    code: str
    message: str
    since_ms: int
    count: int = 1


@dataclass
class _Stats:
    leaves: int = 0
    last_sample_ms: int | None = None
    last_forwarded_ms: int | None = None
    published: int = 0
    dropped: int = 0
    read_errors: int = 0


@dataclass
class _Sparkplug:
    bd_seq: int
    seq: int
    last_birth_ms: int | None


@dataclass
class _Snapshot:
    state: State = "disabled"
    since_ms: int = 0
    config: MqttRuntimeConfig | None = None
    error: _Error | None = None
    attempt: int = 0
    next_retry_at: float | None = None
    stats: _Stats = field(default_factory=_Stats)
    sparkplug: _Sparkplug | None = None


class StatusTracker:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._epoch = 0
        self._data = _Snapshot(since_ms=clock.wall_ms())

    @property
    def state(self) -> State:
        with self._lock:
            return self._data.state

    def reset(
        self,
        state: State,
        config: MqttRuntimeConfig | None,
        error: tuple[str, str] | None = None,
    ) -> StatusWriter:
        """Open a new epoch in ``state``; earlier writers stop having any effect."""
        with self._lock:
            self._epoch += 1
            now = self._clock.wall_ms()
            data = _Snapshot(state=state, since_ms=now, config=config)
            if config is not None:
                data.stats.leaves = len(config.leaves)
            if error is not None:
                data.error = _Error(error[0], error[1][:MAX_MESSAGE_CHARS], now)
            self._data = data
            return StatusWriter(self, self._epoch)

    def _apply(self, epoch: int, change: _Change) -> bool:
        with self._lock:
            if epoch != self._epoch:
                return False
            change.apply(self._data, self._clock.wall_ms())
            return True

    def snapshot(self) -> StatusDocument:
        with self._lock:
            data = self._data
            doc: StatusDocument = {
                "contract": STATUS_CONTRACT,
                "version": STATUS_VERSION,
                "configured": data.config is not None,
                "state": data.state,
                "since": iso_ms(data.since_ms),
                "error": None,
            }
            if data.error is not None:
                doc["error"] = {
                    "category": category_of(data.error.code),
                    "code": data.error.code,
                    "message": data.error.message,
                    "since": iso_ms(data.error.since_ms),
                    "count": data.error.count,
                }
            if data.config is None:
                return doc
            config = data.config
            doc["mode"] = config.mode
            doc["programMd5"] = config.program_md5
            doc["broker"] = {
                "host": config.connection.host,
                "port": config.connection.port,
                "tls": config.connection.tls.enabled,
            }
            next_retry: int | None = None
            if data.state == "retry_wait" and data.next_retry_at is not None:
                remaining = data.next_retry_at - self._clock.monotonic()
                next_retry = max(0, round(remaining * 1000))
            doc["reconnect"] = {"attempt": data.attempt, "nextRetryInMs": next_retry}
            stats = data.stats
            doc["stats"] = {
                "leaves": stats.leaves,
                "lastSampleAt": _optional_iso(stats.last_sample_ms),
                "lastForwardedAt": _optional_iso(stats.last_forwarded_ms),
                "published": stats.published,
                "dropped": stats.dropped,
                "readErrors": stats.read_errors,
            }
            if data.sparkplug is not None:
                doc["sparkplug"] = {
                    "bdSeq": data.sparkplug.bd_seq,
                    "seq": data.sparkplug.seq,
                    "lastBirthAt": _optional_iso(data.sparkplug.last_birth_ms),
                }
            return doc


def _optional_iso(wall_ms: int | None) -> str | None:
    return None if wall_ms is None else iso_ms(wall_ms)


def _set_error(data: _Snapshot, now: int, code: str, message: str) -> None:
    text = message[:MAX_MESSAGE_CHARS]
    current = data.error
    if current is not None and current.code == code and current.message == text:
        current.count += 1
    else:
        data.error = _Error(code, text, now)


@dataclass(frozen=True)
class _Change:
    kind: str
    code: str = ""
    message: str = ""
    count: int = 0
    attempt: int = 0
    next_retry_at: float | None = None
    sparkplug: tuple[int, int, int | None] | None = None

    def apply(self, data: _Snapshot, now: int) -> None:
        if self.kind == "connecting":
            _move(data, "connecting", now)
            data.next_retry_at = None
        elif self.kind == "connected":
            _move(data, "connected", now)
            data.error = None
            data.attempt = 0
            data.next_retry_at = None
        elif self.kind == "retry":
            _move(data, "retry_wait", now)
            _set_error(data, now, self.code, self.message)
            data.attempt = self.attempt
            data.next_retry_at = self.next_retry_at
        elif self.kind == "failed":
            _move(data, activation_state(self.code), now)
            _set_error(data, now, self.code, self.message)
            data.next_retry_at = None
        elif self.kind == "issue":
            _set_error(data, now, self.code, self.message)
        elif self.kind == "clear_read_issue":
            if data.error is not None and category_of(data.error.code) == "read_conversion":
                data.error = None
        elif self.kind == "sampled":
            data.stats.last_sample_ms = now
            data.stats.read_errors += self.count
        elif self.kind == "forwarded":
            data.stats.published += self.count
            data.stats.last_forwarded_ms = now
        elif self.kind == "dropped":
            data.stats.dropped += self.count
        elif self.kind == "sparkplug" and self.sparkplug is not None:
            data.sparkplug = _Sparkplug(*self.sparkplug)


def _move(data: _Snapshot, state: State, now: int) -> None:
    if data.state != state:
        data.state = state
        data.since_ms = now


class StatusWriter:
    """What one publisher may report; inert once its epoch has passed."""

    def __init__(self, tracker: StatusTracker, epoch: int) -> None:
        self._tracker = tracker
        self._epoch = epoch

    def _send(self, change: _Change) -> bool:
        return self._tracker._apply(self._epoch, change)  # pylint: disable=protected-access

    def connecting(self) -> bool:
        return self._send(_Change("connecting"))

    def connected(self) -> bool:
        return self._send(_Change("connected"))

    def retry(self, code: str, message: str, attempt: int, next_retry_at: float) -> bool:
        return self._send(
            _Change("retry", code, message, attempt=attempt, next_retry_at=next_retry_at)
        )

    def failed(self, code: str, message: str) -> bool:
        return self._send(_Change("failed", code, message))

    def issue(self, code: str, message: str) -> bool:
        return self._send(_Change("issue", code, message))

    def clear_read_issue(self) -> bool:
        return self._send(_Change("clear_read_issue"))

    def sampled(self, read_errors: int) -> bool:
        return self._send(_Change("sampled", count=read_errors))

    def forwarded(self, count: int) -> bool:
        return count <= 0 or self._send(_Change("forwarded", count=count))

    def dropped(self, count: int) -> bool:
        return count <= 0 or self._send(_Change("dropped", count=count))

    def sparkplug(self, bd_seq: int, seq: int, last_birth_ms: int | None) -> bool:
        return self._send(_Change("sparkplug", sparkplug=(bd_seq, seq, last_birth_ms)))
