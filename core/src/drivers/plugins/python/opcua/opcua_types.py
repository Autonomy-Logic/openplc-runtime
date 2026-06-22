"""OPC-UA Server plugin type definitions.

VariableNode is server-specific (it carries an asyncua Node). VariableMetadata
is shared with the OPC-UA Client plugin and now lives in
shared/opcua_common/types.py — it is re-exported here so existing
`from .opcua_types import VariableMetadata, VariableNode` imports keep working.
"""

import os
import sys
from dataclasses import dataclass
from typing import Optional

from asyncua.common.node import Node

_python_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _python_dir not in sys.path:
    sys.path.insert(0, _python_dir)

from shared.opcua_common.types import VariableMetadata  # noqa: E402,F401


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
