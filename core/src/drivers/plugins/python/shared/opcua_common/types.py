"""Shared OPC-UA type definitions (no asyncua dependency).

VariableMetadata is a plain dataclass describing a PLC debug leaf
(arr, elem) and its byte size. It lives here — separate from the
Server's VariableNode (which carries an asyncua Node) — so the memory
bridge can be used without importing asyncua.
"""

from dataclasses import dataclass
from typing import Tuple


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
