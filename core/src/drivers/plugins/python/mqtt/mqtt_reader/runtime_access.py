# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""``DebugAccess`` over the runtime's ``plugin_runtime_args_t`` debugger thunks.

The struct is copied by value at construction, as the plugin ABI requires.
Reads land in one private buffer of ``READ_BUFFER_BYTES``; ``LeafReader``
serializes its samples, so the buffer is never shared between two reads.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from typing import Final

from shared.plugin_runtime_args import PluginRuntimeArgs

from .reader import READ_BUFFER_BYTES

_REQUIRED: Final = (
    "image_lock",
    "image_unlock",
    "debug_array_count",
    "debug_elem_count",
    "debug_size",
    "debug_read",
)


def _holding_gil(
    function: object, restype: type | None, argtypes: tuple[type, ...]
) -> Callable[..., int | None]:
    """The same C function, called without releasing the GIL.

    Used only for calls made while the image lock is held: a released GIL
    could let another Python thread run, and the scan would wait for the lock.
    ``image_lock`` itself keeps releasing the GIL, since other plugins hold the
    image lock while they wait for the GIL.
    """
    address = ctypes.cast(function, ctypes.c_void_p).value
    return ctypes.PYFUNCTYPE(restype, *argtypes)(address)


class RuntimeDebugAccess:
    def __init__(self, runtime_args: PluginRuntimeArgs) -> None:
        missing = [name for name in _REQUIRED if not getattr(runtime_args, name)]
        if missing:
            raise ValueError(f"runtime arguments lack {', '.join(missing)}")
        self._args = PluginRuntimeArgs.from_buffer_copy(runtime_args)
        self._buffer = (ctypes.c_uint8 * READ_BUFFER_BYTES)()
        self._dest = ctypes.cast(self._buffer, ctypes.POINTER(ctypes.c_uint8))
        self._read_holding_gil = _holding_gil(
            self._args.debug_read,
            ctypes.c_uint16,
            (ctypes.c_uint8, ctypes.c_uint16, ctypes.POINTER(ctypes.c_uint8)),
        )
        self._unlock_holding_gil = _holding_gil(self._args.image_unlock, None, ())

    def lock(self) -> None:
        self._args.image_lock()

    def unlock(self) -> None:
        self._unlock_holding_gil()

    def array_count(self) -> int:
        return int(self._args.debug_array_count())

    def elem_count(self, arr: int) -> int:
        return int(self._args.debug_elem_count(arr))

    def size(self, arr: int, elem: int) -> int:
        return int(self._args.debug_size(arr, elem))

    def read(self, arr: int, elem: int) -> bytes:
        written = int(self._read_holding_gil(arr, elem, self._dest))
        if written > READ_BUFFER_BYTES:
            raise OverflowError("debug_read reported more bytes than the read buffer")
        if written <= 0:
            return b""
        return ctypes.string_at(self._buffer, written)
