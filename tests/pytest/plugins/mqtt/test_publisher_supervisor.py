# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""State machine and retry authority under a controlled clock and a scripted network."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from api_schema import check
from contract.runtime_config import MqttRuntimeConfig, decode_runtime_config
from mqtt_publisher.backoff import Backoff
from mqtt_publisher.connection import (
    TRANSPORT_MESSAGES,
    ConnectSpec,
    TransportError,
    transport_error,
)
from mqtt_publisher.status import (
    TRANSPORT_ERROR_CATEGORY,
    LogLimiter,
    StatusTracker,
    activation_state,
    category_of,
)
from mqtt_publisher.strategy import OutboundMessage
from mqtt_publisher.supervisor import (
    Supervisor,
    SupervisorSettings,
    packet_limit_message,
)
from publisher_fakes import (
    WALL_BASE_MS,
    CounterSampler,
    FakeClock,
    FakeNetwork,
    RecordingStrategy,
)

FIXTURES = (
    Path(__file__).resolve().parents[4] / "core/src/drivers/plugins/python/mqtt/contract/fixtures"
)
CONFIG: MqttRuntimeConfig = decode_runtime_config(
    (FIXTURES / "runtime-config/valid/generic-on-change-anonymous-plaintext.json").read_bytes(),
    None,
)
CATALOG = json.loads((FIXTURES / "errors/error-codes.json").read_text(encoding="utf-8"))["codes"]
SPEC = ConnectSpec("broker.example.net", 1883, "plc-linha1", None)
JITTER_MAX = 1.2


class Rig:
    # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        rng: float = 0.0,
        strategy: RecordingStrategy | None = None,
        values: tuple[int, ...] = (1, 2, 3),
    ) -> None:
        self.clock = FakeClock()
        self.network = FakeNetwork(self.clock)
        self.strategy = strategy or RecordingStrategy()
        self.sampler = CounterSampler(values)
        self.tracker = StatusTracker(self.clock)
        self.writer = self.tracker.reset("connecting", CONFIG)
        self.backoff = Backoff(lambda: rng)
        self.logs: list[tuple[str, str]] = []
        self.cancel = threading.Event()
        self.supervisor = Supervisor(
            SPEC,
            self.network.factory,
            self.strategy,
            self.sampler,
            self.writer,
            self.clock,
            self.backoff,
            SupervisorSettings(tick_interval_s=self.strategy.tick_interval_ms / 1000),
            lambda level, message: self.logs.append((level, message)),
        )

    def stop_at(self, when: float) -> None:
        self.clock.at(when, self.cancel.set)

    def run_until(self, when: float) -> None:
        self.stop_at(when)
        self.supervisor.run(self.cancel)

    @property
    def status(self) -> dict[str, object]:
        return self.tracker.snapshot()


def _retry_sleeps(rig: Rig) -> list[float]:
    return [round(s, 6) for s in rig.clock.sleeps]


# Backoff policy


def test_backoff_bases_cap_and_jitter_bounds() -> None:
    low = Backoff(lambda: 0.0)
    high = Backoff(lambda: 1.0)
    assert [low.next_delay() for _ in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert [round(high.next_delay(), 9) for _ in range(7)] == [1.2, 2.4, 4.8, 9.6, 19.2, 36, 36]
    assert Backoff(lambda: 5.0).next_delay() == pytest.approx(1.2)
    assert Backoff(lambda: -1.0).next_delay() == 1.0


def test_backoff_resets_only_after_a_stable_connection() -> None:
    backoff = Backoff(lambda: 0.0)
    for _ in range(4):
        backoff.next_delay()
    backoff.connected(100.0)
    assert backoff.disconnected(159.9) is False
    assert backoff.next_delay() == 16
    backoff.connected(200.0)
    assert backoff.disconnected(260.0) is True
    assert backoff.attempt == 0 and backoff.next_delay() == 1
    assert backoff.disconnected(300.0) is False


def test_backoff_requires_bases() -> None:
    with pytest.raises(ValueError):
        Backoff(bases_s=())


# Retry authority and states


def test_unreachable_broker_retries_on_the_contract_schedule() -> None:
    rig = Rig(rng=0.5)
    rig.network.up = False
    rig.run_until(200.0)
    expected = [base * 1.1 for base in (1, 2, 4, 8, 16, 30, 30, 30, 30)]
    assert _retry_sleeps(rig)[: len(expected)] == pytest.approx(expected)
    assert rig.network.open_timeouts[0] == 10.0
    assert not rig.network.opened
    assert rig.sampler.samples == 0


def test_retry_status_reports_attempt_error_and_next_retry() -> None:
    rig = Rig()
    rig.network.up = False
    rig.run_until(3.5)
    status = rig.status
    assert check(status, "status") == []
    assert status["state"] == "retry_wait"
    error = status["error"]
    assert isinstance(error, dict)
    assert (error["category"], error["code"]) == ("network", "network_connection_refused")
    assert error["count"] == 3 and error["since"] == "2026-10-09T12:00:00.000Z"
    assert status["reconnect"] == {"attempt": 3, "nextRetryInMs": 3500}


def test_long_outage_caps_waits_and_recovery_announces_current_values() -> None:
    rig = Rig(rng=1.0)
    rig.network.up = False

    def restore() -> None:
        rig.network.up = True
        rig.sampler.values = [41, 42, 43]

    rig.clock.at(400.0, restore)
    rig.run_until(440.0)
    assert max(rig.clock.sleeps) == pytest.approx(30 * JITTER_MAX)
    assert len(rig.network.opened) == 1
    announce = rig.network.sessions[-1].sent[:3]
    assert [m.payload for m in announce] == [b"41", b"42", b"43"]
    status = rig.status
    assert status["state"] == "connected" and status["error"] is None
    assert status["reconnect"] == {"attempt": 0, "nextRetryInMs": None}


@pytest.mark.parametrize(("uptime", "next_base"), [(61.0, 1.0), (10.0, 4.0)])
def test_stable_connection_resets_the_escalation(uptime: float, next_base: float) -> None:
    rig = Rig()
    rig.network.open_script.extend(
        [
            transport_error("network_connection_refused"),
            transport_error("network_connection_refused"),
        ]
    )
    rig.clock.at(3.0 + uptime, lambda: setattr(rig.network, "up", False))
    rig.run_until(3.0 + uptime + 0.5)
    assert rig.clock.sleeps[:2] == [1.0, 2.0]
    assert rig.clock.sleeps[-1] == pytest.approx(next_base)


def test_stop_during_retry_wait_cancels_the_retry() -> None:
    rig = Rig()
    rig.network.up = False
    rig.run_until(20.0)
    attempts = len(rig.network.sessions)
    assert rig.cancel.is_set()
    assert rig.clock.now == 20.0
    rig.supervisor.run(rig.cancel)
    assert len(rig.network.sessions) == attempts
    assert all(session.aborted for session in rig.network.sessions)


def test_stop_while_the_connection_is_pending_abandons_it() -> None:
    rig = Rig()
    rig.network.open_script.append("cancel")
    rig.supervisor.run(rig.cancel)
    assert len(rig.network.sessions) == 1
    assert rig.network.sessions[0].aborted and not rig.network.sessions[0].opened
    assert rig.clock.sleeps == []


def test_failed_attempt_after_stop_is_not_retried() -> None:
    rig = Rig()
    rig.network.open_script.append("fail_cancel")
    rig.supervisor.run(rig.cancel)
    assert len(rig.network.sessions) == 1 and rig.network.sessions[0].aborted
    assert rig.clock.sleeps == []


def test_connected_session_samples_on_the_monotonic_schedule() -> None:
    rig = Rig()
    rig.run_until(1.0)
    session = rig.network.sessions[0]
    assert session.subscriptions == [("ctl/rebirth", 0)]
    assert rig.sampler.samples == 11
    assert len(session.sent) == 33
    status = rig.status
    stats = status["stats"]
    assert isinstance(stats, dict)
    assert stats["published"] == 33 and stats["dropped"] == 0
    assert stats["lastSampleAt"] is not None and stats["lastForwardedAt"] is not None
    assert check(status, "status") == []


def test_late_ticks_are_skipped_not_caught_up() -> None:
    rig = Rig()
    rig.network.service_stretch = 3.5
    rig.run_until(1.0)
    assert rig.supervisor.ticks_skipped > 0
    assert rig.sampler.samples <= 5


def test_intentional_stop_sends_final_messages_and_disconnects_within_budget() -> None:
    rig = Rig()
    rig.run_until(0.55)
    session = rig.network.sessions[0]
    assert session.closed and not session.aborted
    assert [m.topic for m in session.final] == ["state"]
    assert rig.strategy.shutdowns == 1 and rig.strategy.disconnects == 1
    assert rig.network.close_timeouts == [2.0]
    assert rig.network.close_timeouts[0] < rig.supervisor.settings.shutdown_budget_s


# Delivery accounting


def test_unacknowledged_messages_are_never_counted_as_forwarded() -> None:
    rig = Rig(strategy=RecordingStrategy(qos=1))
    rig.network.ack = False
    rig.clock.at(0.35, lambda: setattr(rig.network, "up", False))
    rig.run_until(0.4)
    stats = rig.status["stats"]
    assert isinstance(stats, dict)
    first = rig.network.sessions[0]
    assert first.aborted and len(first.sent) == 12
    assert stats["published"] == 0 and stats["lastForwardedAt"] is None
    assert stats["dropped"] == 12
    assert rig.strategy.disconnects == 1


def test_reconnection_announces_current_state_and_replays_nothing() -> None:
    rig = Rig()
    rig.network.ack = False
    rig.sampler.values = [1, 1, 1]

    def drop() -> None:
        rig.network.up = False

    def restore() -> None:
        rig.sampler.values = [7, 8, 9]
        rig.network.ack = True
        rig.network.up = True

    rig.clock.at(0.25, drop)
    rig.clock.at(0.9, restore)
    rig.run_until(1.5)
    old, new = rig.network.opened[0], rig.network.opened[1]
    assert {m.payload for m in old.sent} == {b"1"}
    assert [m.payload for m in new.sent[:3]] == [b"7", b"8", b"9"]
    assert b"1" not in {m.payload for m in new.sent}
    values = {m.payload for m in rig.network.delivered if m.topic.startswith("v/")}
    assert values == {b"7", b"8", b"9"}


def test_slow_broker_bounds_the_backlog_and_skips_stale_ticks() -> None:
    rig = Rig()
    rig.network.ack = False
    rig.network.capacity = 5
    backlog: list[int] = []
    rig.clock.at(0.95, lambda: backlog.append(len(rig.network.sessions[0].pending)))
    rig.run_until(1.0)
    assert backlog == [5]
    assert rig.sampler.samples == 2
    assert rig.supervisor.ticks_skipped >= 8
    outcome = rig.strategy.outcomes[1]
    assert len(outcome.accepted) == 2 and [m.key for m in outcome.dropped] == [2]
    stats = rig.status["stats"]
    assert isinstance(stats, dict)
    assert stats["dropped"] == 1 + 5 and stats["published"] == 0


def test_oversized_message_is_dropped_and_diagnosed() -> None:
    strategy = RecordingStrategy()
    strategy.payload_size = 262_145
    rig = Rig(strategy=strategy)
    rig.run_until(0.05)
    status = rig.status
    error = status["error"]
    assert isinstance(error, dict)
    assert (error["code"], error["category"]) == ("payload_too_large", "limit")
    assert rig.network.sessions[0].sent == []
    assert len(rig.strategy.outcomes[0].dropped) == 3


def test_inbound_messages_reach_the_strategy_and_its_replies_are_sent() -> None:
    rig = Rig()

    def inject() -> None:
        rig.network.sessions[0].inbound.append(
            __import__("mqtt_publisher.strategy").strategy.InboundMessage("ctl/rebirth", b"x")
        )

    rig.clock.at(0.15, inject)
    rig.run_until(0.3)
    assert [m.topic for m in rig.strategy.inbound] == ["ctl/rebirth"]
    assert "ctl/ack" in [m.topic for m in rig.network.sessions[0].sent]


# Diagnostics


def test_identical_errors_are_logged_at_most_once_per_30_seconds() -> None:
    rig = Rig()
    rig.network.up = False
    rig.run_until(300.0)
    refused = [m for _, m in rig.logs if m.startswith("network_connection_refused")]
    assert len(rig.network.sessions) > 10
    assert 1 <= len(refused) <= 300 / 30 + 1
    status_error = rig.status["error"]
    assert isinstance(status_error, dict) and status_error["count"] == len(rig.network.sessions)


def test_log_limiter_bounds_its_memory_and_lets_new_lines_through() -> None:
    limiter = LogLimiter(30.0, max_keys=2)
    assert limiter.allow("a", 0.0) and not limiter.allow("a", 29.9) and limiter.allow("a", 30.0)
    assert limiter.allow("b", 1.0) and limiter.allow("c", 2.0)
    assert limiter.allow("b", 3.0)
    assert not limiter.allow("a", 31.0)


def test_read_issues_become_a_sanitized_error_and_clear_on_recovery() -> None:
    rig = Rig(values=(123456789, 2))
    rig.sampler.null_at = {0}
    rig.clock.at(0.55, rig.sampler.null_at.clear)
    rig.stop_at(0.5)
    rig.supervisor.run(rig.cancel)
    status = rig.status
    error = status["error"]
    assert isinstance(error, dict)
    assert (error["category"], error["code"]) == ("read_conversion", "read_failed")
    assert "123456789" not in json.dumps(status) and "123456789" not in str(rig.logs)
    stats = status["stats"]
    assert isinstance(stats, dict) and stats["readErrors"] == rig.sampler.samples
    rig2 = Rig()
    rig2.sampler.null_at = {0}
    rig2.clock.at(0.25, rig2.sampler.null_at.clear)
    rig2.run_until(0.5)
    assert rig2.status["error"] is None


def test_two_sessions_dropped_right_after_announcing_are_a_packet_limit() -> None:
    rig = Rig()
    rig.network.kills_left = 2
    rig.run_until(3.5)
    codes = [m.split(":")[0] for _, m in rig.logs if not m.startswith("connected")]
    assert codes == ["network_connection_lost", "broker_packet_limit"]
    assert len(rig.network.opened) == 3
    status_error = rig.status["error"]
    assert status_error is None
    rig2 = Rig()
    rig2.network.kills_left = 2
    rig2.run_until(1.5)
    error = rig2.status["error"]
    assert isinstance(error, dict)
    assert (error["code"], error["category"]) == ("broker_packet_limit", "limit")
    assert error["message"] == packet_limit_message(1)


def test_a_late_drop_is_an_ordinary_connection_loss() -> None:
    rig = Rig()
    for when in (2.0, 5.5):
        rig.clock.at(
            when,
            lambda: setattr(
                rig.network.sessions[-1], "lost", transport_error("network_connection_lost")
            ),
        )
    rig.run_until(6.0)
    codes = {m.split(":")[0] for _, m in rig.logs if not m.startswith("connected")}
    assert codes == {"network_connection_lost"}


def test_strategy_failure_stops_the_publisher_as_an_internal_error() -> None:
    strategy = RecordingStrategy()
    strategy.fail_on_sample = True
    rig = Rig(strategy=strategy)
    rig.stop_at(30.0)
    rig.supervisor.run(rig.cancel)
    assert not rig.cancel.is_set()
    status = rig.status
    assert status["state"] == "config_error"
    error = status["error"]
    assert isinstance(error, dict) and (error["code"], error["category"]) == (
        "internal_error",
        "internal",
    )
    assert any("RuntimeError" in m for _, m in rig.logs)


def test_settings_refuse_a_close_timeout_outside_the_budget() -> None:
    with pytest.raises(ValueError):
        SupervisorSettings(tick_interval_s=0.1, close_timeout_s=5.0)
    with pytest.raises(ValueError):
        SupervisorSettings(tick_interval_s=0.0)


def test_will_is_requested_before_every_attempt() -> None:
    will = OutboundMessage("death", b"x", 1, False)
    rig = Rig(strategy=RecordingStrategy(will=will))
    rig.network.open_script.extend([transport_error("auth_rejected")])
    rig.run_until(1.5)
    assert rig.strategy.wills == 2
    assert all(session.spec.will == will for session in rig.network.sessions)


# Status authority


def test_status_documents_match_the_api_schema_in_every_state() -> None:
    clock = FakeClock()
    tracker = StatusTracker(clock)
    assert tracker.snapshot() == {
        "contract": "openplc-mqtt-status",
        "version": 1,
        "configured": False,
        "state": "disabled",
        "since": "2026-10-09T12:00:00.000Z",
        "error": None,
    }
    for state in ("plc_stopped", "connecting", "connected"):
        tracker.reset(state, CONFIG)  # type: ignore[arg-type]
        assert check(tracker.snapshot(), "status") == []
    writer = tracker.reset("config_error", CONFIG, ("config_address_invalid", "x" * 600))
    doc = tracker.snapshot()
    assert check(doc, "status") == []
    assert isinstance(doc["error"], dict) and len(doc["error"]["message"]) == 512
    writer.sparkplug(7, 42, WALL_BASE_MS)
    assert check(tracker.snapshot(), "status") == []
    tracker.reset("credential_error", CONFIG, ("credential_missing", "missing"))
    doc = tracker.snapshot()
    assert check(doc, "status") == [] and doc["state"] == "credential_error"
    assert "sparkplug" not in doc


def test_a_replaced_epoch_ignores_its_late_reports() -> None:
    clock = FakeClock()
    tracker = StatusTracker(clock)
    old = tracker.reset("connecting", CONFIG)
    tracker.reset("plc_stopped", CONFIG)
    assert old.connected() is False
    assert old.forwarded(5) is False
    assert old.retry("network_connection_lost", "m", 1, 3.0) is False
    doc = tracker.snapshot()
    assert doc["state"] == "plc_stopped" and doc["error"] is None
    stats = doc["stats"]
    assert isinstance(stats, dict) and stats["published"] == 0


def test_error_categories_follow_the_shared_catalog() -> None:
    for code, category in TRANSPORT_ERROR_CATEGORY.items():
        assert CATALOG[code] == category, code
    for code in TRANSPORT_MESSAGES:
        assert code in CATALOG and category_of(code) == CATALOG[code]
        assert len(TRANSPORT_MESSAGES[code]) <= 512
    assert category_of("config_schema") == "configuration"
    assert category_of("unknown_code") == "internal"
    assert activation_state("credential_missing") == "credential_error"
    assert activation_state("client_key_mismatch") == "credential_error"
    assert activation_state("config_address_invalid") == "config_error"
    assert activation_state("internal_error") == "config_error"


def test_transport_errors_carry_fixed_messages_only() -> None:
    error = transport_error("auth_rejected")
    assert isinstance(error, TransportError)
    assert error.message == TRANSPORT_MESSAGES["auth_rejected"]
