# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""OPC-UA plugin type definitions."""

from dataclasses import dataclass
from typing import Any, Optional, Tuple
from asyncua.common.node import Node


@dataclass
class VariableNode:
    """Represents an OPC-UA node mapped to a PLC debug variable.

    Variables are addressed by (arr, elem) — the same (uint8_t, uint16_t)
    tuple the runtime's strucpp_debug_* C exports take. Arrays carry the
    base address and a length; element i lives at (arr, elem + i).
    """
    node: Node
    arr: int
    elem: int
    datatype: str
    access_mode: str
    is_array_element: bool = False
    array_index: Optional[int] = None  # 0..length-1 within the array
    array_length: Optional[int] = None  # Length of array (for array nodes only)


@dataclass
class VariableMetadata:
    """Metadata cache for direct memory access via debug_read/debug_write."""
    arr: int
    elem: int
    size: int
    inferred_type: str

    @property
    def addr(self) -> Tuple[int, int]:
        """Convenience accessor for (arr, elem) tuple."""
        return (self.arr, self.elem)
