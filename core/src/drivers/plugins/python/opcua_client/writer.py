"""
PLC -> remote writer (poll + change-detect).

There is no subscription mechanism for local PLC variables, so the writer reads
each plc_to_remote leaf via the debug surface once per cycle and writes it to
the remote node only when it changed since the last cycle (a per-leaf cache,
ported from the server's _has_value_changed). This avoids hammering the remote
server with redundant writes.
"""

import os
import sys
from typing import Any, Dict, List, Tuple

from asyncua import ua

_current_dir = os.path.dirname(os.path.abspath(__file__))
_python_dir = os.path.dirname(_current_dir)
for _p in (_current_dir, _python_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared.opcua_common.opcua_memory import debug_read_value  # noqa: E402
from shared.opcua_common.opcua_utils import (  # noqa: E402
    convert_value_for_opcua,
    map_plc_to_opcua_type,
)

try:
    from .opcua_client_logging import log_debug, log_error
except ImportError:
    from opcua_client_logging import log_debug, log_error


Addr = Tuple[int, int]


class RuntimeToRemoteWriter:
    """Pushes changed PLC leaf values to their mapped remote nodes."""

    def __init__(self, client: Any, args: Any, write_mappings: List[Any]):
        self.client = client
        self.args = args
        self.mappings = write_mappings
        self.nodes = {id(m): client.get_node(m.remote_node_id) for m in write_mappings}
        self._cache: Dict[Addr, Any] = {}

    async def write_once(self) -> None:
        """One pass over all plc_to_remote mappings."""
        for m in self.mappings:
            try:
                value = debug_read_value(self.args, m.arr, m.elem, m.datatype)
                if value is None:
                    continue
                addr = (m.arr, m.elem)
                if not self._changed(addr, value):
                    continue
                opcua_value = convert_value_for_opcua(m.datatype, value)
                variant_type = map_plc_to_opcua_type(m.datatype)
                await self.nodes[id(m)].write_value(ua.Variant(opcua_value, variant_type))
                self._cache[addr] = value
                log_debug(f"PLC->remote {m.remote_node_id} = {opcua_value}")
            except Exception as e:
                log_error(f"write to remote node {m.remote_node_id} failed: {e}")

    def _changed(self, addr: Addr, new_value: Any) -> bool:
        cached = self._cache.get(addr)
        if addr not in self._cache:
            return True
        if isinstance(new_value, float) and isinstance(cached, float):
            return abs(new_value - cached) > 1e-6
        return new_value != cached

    def reset(self) -> None:
        """Drop the change-detect cache so the next pass re-pushes everything
        (used after a reconnect)."""
        self._cache.clear()
