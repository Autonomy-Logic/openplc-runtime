# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Time source of the publisher: monotonic for scheduling, wall clock only for reporting."""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_ms(self) -> int: ...

    def wait(self, event: threading.Event, timeout_s: float) -> bool:
        """Block until ``event`` is set or ``timeout_s`` elapses; True when the event is set."""


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def wait(self, event: threading.Event, timeout_s: float) -> bool:
        return event.wait(max(0.0, timeout_s))
