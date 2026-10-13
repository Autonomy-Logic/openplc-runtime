# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Typed, bounded sampling of the configured leaves from the loaded PLC program.

``bind_leaves`` checks every ``(arr, elem)`` and its size against the program
the runtime has loaded, through the debugger table API only. ``LeafReader``
copies the raw bytes under the image lock in short windows and converts them
after releasing it, so no conversion, serialization or network work ever runs
while the lock is held. The image lock also orders reads against program
unload, which clears the debugger thunks under the same lock.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from contract.runtime_config import ConfigError, Leaf

from .values import LeafValue, ReadIssue, convert_debug_read, null_value

# Small windows bound how long the scan task can wait for the image lock.
LOCK_WINDOW_LEAVES: Final = 4
# Every STruC++ TypeOps.size is a uint8_t, so one read never writes more than 255 bytes.
READ_BUFFER_BYTES: Final = 256
_READ_ERRORS: Final = (ctypes.ArgumentError, OSError, ValueError, TypeError, OverflowError)


class DebugAccess(Protocol):
    """The runtime debugger surface the reader needs."""

    def lock(self) -> None: ...

    def unlock(self) -> None: ...

    def array_count(self) -> int: ...

    def elem_count(self, arr: int) -> int: ...

    def size(self, arr: int, elem: int) -> int: ...

    def read(self, arr: int, elem: int) -> bytes:
        """Copy of the bytes ``debug_read`` wrote; empty when it wrote nothing."""


@dataclass(frozen=True)
class LeafIssue:
    index: int
    issue: ReadIssue


@dataclass(frozen=True)
class SampleResult:
    values: tuple[LeafValue, ...]
    issues: tuple[LeafIssue, ...]

    @property
    def read_errors(self) -> int:
        return len(self.issues)


def _address_error(index: int, member: str, message: str) -> ConfigError:
    return ConfigError("semantic", "config_address_invalid", f"/leaves/{index}/{member}", message)


def _check_leaf(access: DebugAccess, index: int, leaf: Leaf, arrays: int) -> ConfigError | None:
    if leaf.arr >= arrays or leaf.elem >= access.elem_count(leaf.arr):
        return _address_error(index, "address", "address does not exist in the loaded program")
    if access.size(leaf.arr, leaf.elem) != leaf.size:
        return _address_error(index, "size", "size differs from the loaded program at this address")
    return None


def bind_leaves(
    access: DebugAccess, leaves: Sequence[Leaf], window: int = LOCK_WINDOW_LEAVES
) -> LeafReader | ConfigError:
    """Validate every leaf against the loaded program; the first mismatch rejects all."""
    if not leaves:
        return ConfigError("schema", "config_no_leaves", "/leaves", "no leaves are configured")
    if any(leaf.size > READ_BUFFER_BYTES for leaf in leaves):
        return ConfigError("schema", "config_size_mismatch", "/leaves", "leaf exceeds read buffer")
    access.lock()
    try:
        arrays = access.array_count()
        if arrays == 0:
            return ConfigError(
                "semantic", "config_program_mismatch", "/program/md5", "no program is loaded"
            )
        for index, leaf in enumerate(leaves):
            error = _check_leaf(access, index, leaf, arrays)
            if error is not None:
                return error
    except _READ_ERRORS:
        return ConfigError(
            "semantic", "config_program_mismatch", "/program/md5", "program tables are unreadable"
        )
    finally:
        access.unlock()
    return LeafReader(access, tuple(leaves), window)


class LeafReader:
    """Samples a bound leaf set. One sample at a time; safe to call from any thread."""

    def __init__(self, access: DebugAccess, leaves: tuple[Leaf, ...], window: int) -> None:
        if window < 1:
            raise ValueError("lock window must hold at least one leaf")
        self._access = access
        self._leaves = leaves
        self._window = window
        self._sampling = threading.Lock()

    @property
    def leaves(self) -> tuple[Leaf, ...]:
        return self._leaves

    def _read_raw(self) -> list[bytes | None]:
        raws: list[bytes | None] = []
        for start in range(0, len(self._leaves), self._window):
            self._access.lock()
            try:
                for leaf in self._leaves[start : start + self._window]:
                    try:
                        raws.append(self._access.read(leaf.arr, leaf.elem))
                    except _READ_ERRORS:
                        raws.append(None)
            finally:
                self._access.unlock()
        return raws

    def sample(self) -> SampleResult:
        with self._sampling:
            raws = self._read_raw()
        values: list[LeafValue] = []
        issues: list[LeafIssue] = []
        for index, (leaf, raw) in enumerate(zip(self._leaves, raws)):
            value = _convert(leaf, raw)
            values.append(value)
            if value.issue is not None:
                issues.append(LeafIssue(index, value.issue))
        return SampleResult(tuple(values), tuple(issues))


def _convert(leaf: Leaf, raw: bytes | None) -> LeafValue:
    if raw is None:
        return null_value(leaf.iec_type, "read_failed", "thunk_error")
    if not raw:
        return null_value(leaf.iec_type, "read_failed", "no_value")
    if len(raw) != leaf.size:
        return null_value(leaf.iec_type, "read_failed", "size_mismatch")
    return convert_debug_read(leaf.iec_type, raw)
