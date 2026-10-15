# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""In-memory stand-in for the runtime debugger thunks, built from a debug map."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = (
    Path(__file__).resolve().parents[4]
    / "core/src/drivers/plugins/python/mqtt/contract/fixtures/runtime-config"
)
DEBUG_MAP = FIXTURES / "debug-map.json"


@dataclass
class FakeProgram:
    """Implements ``DebugAccess``; records how reads relate to the image lock."""

    counts: list[int]
    sizes: dict[tuple[int, int], int]
    values: dict[tuple[int, int], bytes] = field(default_factory=dict)
    depth: int = 0
    windows: int = 0
    reads_in_window: int = 0
    max_reads_per_window: int = 0
    reads_outside_lock: int = 0
    calls_outside_lock: int = 0
    raise_on: set[tuple[int, int]] = field(default_factory=set)
    empty_on: set[tuple[int, int]] = field(default_factory=set)
    short_on: set[tuple[int, int]] = field(default_factory=set)
    loaded: bool = True

    @classmethod
    def from_debug_map(cls, path: Path = DEBUG_MAP) -> FakeProgram:
        doc = json.loads(path.read_text(encoding="utf-8"))
        counts = [entry["count"] for entry in doc["arrays"]]
        sizes = {(leaf["arrayIdx"], leaf["elemIdx"]): leaf["size"] for leaf in doc["leaves"]}
        return cls(counts, sizes)

    def lock(self) -> None:
        self.depth += 1
        self.windows += 1
        self.reads_in_window = 0

    def unlock(self) -> None:
        assert self.depth > 0, "unlock without lock"
        self.depth -= 1

    def _guard(self) -> None:
        if self.depth == 0:
            self.calls_outside_lock += 1

    def array_count(self) -> int:
        self._guard()
        return len(self.counts) if self.loaded else 0

    def elem_count(self, arr: int) -> int:
        self._guard()
        return self.counts[arr] if self.loaded and arr < len(self.counts) else 0

    def size(self, arr: int, elem: int) -> int:
        self._guard()
        return self.sizes.get((arr, elem), 0) if self.loaded else 0

    def read(self, arr: int, elem: int) -> bytes:
        if self.depth == 0:
            self.reads_outside_lock += 1
        self.reads_in_window += 1
        self.max_reads_per_window = max(self.max_reads_per_window, self.reads_in_window)
        key = (arr, elem)
        if key in self.raise_on:
            raise OSError("thunk failed")
        size = self.sizes.get(key, 0)
        if not self.loaded or size == 0 or key in self.empty_on:
            return b""
        raw = self.values.get(key, bytes(size))
        return raw[:-1] if key in self.short_on else raw
