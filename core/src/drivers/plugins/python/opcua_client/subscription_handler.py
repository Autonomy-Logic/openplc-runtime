"""
Subscription data-change handler (remote -> PLC).

asyncua invokes datachange_notification on the client's event loop whenever a
subscribed remote node changes. We convert the remote value to the PLC datatype
and soft-write it into the PLC leaf (arr, elem) via the debug-write journal.

Writes are gated on a PLC-loaded predicate so notifications that arrive while
no program is loaded are dropped silently instead of spamming the log with
"no program" failures.
"""

import os
import sys
from typing import Any, Callable, Dict

_current_dir = os.path.dirname(os.path.abspath(__file__))
_python_dir = os.path.dirname(_current_dir)
for _p in (_current_dir, _python_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared.opcua_common.opcua_memory import debug_write_value  # noqa: E402
from shared.opcua_common.opcua_utils import convert_value_for_plc  # noqa: E402

try:
    from .opcua_client_logging import log_debug, log_error
except ImportError:
    from opcua_client_logging import log_debug, log_error


class SubscriptionDataHandler:
    """asyncua DataChangeNotificationHandler: remote node change -> PLC leaf."""

    def __init__(
        self,
        args: Any,
        node_map: Dict[str, Any],
        is_plc_loaded: Callable[[], bool],
    ):
        """
        Args:
            args: PluginRuntimeArgs ctypes struct (debug_write surface).
            node_map: remote NodeId string -> NodeMapping.
            is_plc_loaded: predicate; when False, notifications are dropped.
        """
        self.args = args
        self.node_map = node_map
        self.is_plc_loaded = is_plc_loaded

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        try:
            mapping = self._lookup(node)
            if mapping is None:
                return
            if not self.is_plc_loaded():
                return
            # convert_value_for_plc yields a (tv_sec, tv_nsec) tuple for
            # TIME-family types, which debug_write_value packs into the
            # IEC_TIMESPEC leaf directly — no int64-ns bridge needed.
            plc_value = convert_value_for_plc(mapping.datatype, val)
            ok = debug_write_value(
                self.args, mapping.arr, mapping.elem, mapping.datatype, plc_value
            )
            if not ok:
                # 0x82 (queue full) / 0x81 (no program) / encode failure — all
                # transient or already-gated; debug-level so it never spams.
                log_debug(
                    f"debug_write({mapping.arr},{mapping.elem}) not applied "
                    f"(node {mapping.remote_node_id})"
                )
        except Exception as e:
            log_error(f"datachange_notification failed for {node}: {e}")

    def _lookup(self, node: Any):
        # Match by NodeId string; fall back to str(node) for safety.
        try:
            key = node.nodeid.to_string()
        except Exception:
            key = str(node)
        mapping = self.node_map.get(key)
        if mapping is None:
            mapping = self.node_map.get(str(node))
        return mapping
