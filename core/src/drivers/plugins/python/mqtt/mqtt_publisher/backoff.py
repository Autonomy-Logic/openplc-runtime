# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Reconnect delay policy: 1, 2, 4, 8, 16, 30 s then 30 s, plus 0-20% jitter.

The escalation restarts only after a connection stayed up for the reset period;
a connection that drops sooner keeps escalating, so a broker that accepts and
immediately closes cannot pull the client into a tight loop.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence

from contract.constants import BACKOFF_BASES_S, BACKOFF_JITTER, BACKOFF_RESET_AFTER_MS


class Backoff:
    def __init__(
        self,
        rng: Callable[[], float] = random.random,
        bases_s: Sequence[float] = BACKOFF_BASES_S,
        jitter: float = BACKOFF_JITTER,
        reset_after_s: float = BACKOFF_RESET_AFTER_MS / 1000,
    ) -> None:
        if not bases_s:
            raise ValueError("at least one backoff base is required")
        self._rng = rng
        self._bases = tuple(float(base) for base in bases_s)
        self._jitter = jitter
        self._reset_after = reset_after_s
        self._attempt = 0
        self._connected_at: float | None = None

    @property
    def attempt(self) -> int:
        """Consecutive failed attempts since the last reset."""
        return self._attempt

    def next_delay(self) -> float:
        base = self._bases[min(self._attempt, len(self._bases) - 1)]
        fraction = min(max(self._rng(), 0.0), 1.0)
        self._attempt += 1
        return base * (1.0 + self._jitter * fraction)

    def connected(self, now: float) -> None:
        self._connected_at = now

    def disconnected(self, now: float) -> bool:
        """Record a drop at ``now``; True when a stable connection restarted the escalation."""
        stable = self._connected_at is not None and now - self._connected_at >= self._reset_after
        self._connected_at = None
        if stable:
            self._attempt = 0
        return stable
