"""
OPC-UA ↔ PLC synchronization — request-driven.

Replaces the old unconditional bidirectional poll (which read every
writable node every cycle and wrote it back to the PLC — OpenPLC
bug #2: OPC-UA fighting the program for readwrite variables). The
model is now:

  - READS (client → server): a per-node value_callback returns the
    LIVE PLC value via args.debug_read at read time. No staleness, no
    poll for plain reads.

  - WRITES (client → PLC): a per-node value_setter intercepts an
    actual client Write and forwards ONLY that value to the PLC via
    args.debug_write. The runtime enqueues it on the debug-write
    journal and applies it race-free at the no-task window, so this is
    a soft write the next scan can overwrite (distinct from debugger
    forcing). Variables no client ever writes are never written back.

  - SUBSCRIPTIONS (server → client): asyncua only emits DataChange
    notifications when write_attribute_value runs, so a light push
    loop reads the live PLC value and pushes it — but ONLY for nodes
    that actually have a monitored item (subscription-scoped). Nodes
    with no subscriber cost nothing; their reads are still live via
    the callback.

Our own push re-enters write_attribute_value (and thus the setter);
it is told apart from a real client write by DataValue object
identity (await-safe — an interleaved client write is a different
object), so a push never echoes back into a PLC write.

Variables are addressed by (arr, elem) — the same tuple the editor
resolved against debug-map.json and wrote into opcua_config.json.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Set, Tuple

from asyncua import Server, ua

# Import local modules (handle both package and direct loading)
try:
    from .opcua_logging import log_debug, log_error, log_info, log_warn
    from .opcua_memory import (
        debug_read_value,
        debug_write_value,
        initialize_variable_cache,
    )
    from .opcua_types import VariableMetadata, VariableNode
    from .opcua_utils import (
        convert_value_for_opcua,
        convert_value_for_plc,
        map_plc_to_opcua_type,
    )
except ImportError:
    from opcua_logging import log_debug, log_error, log_info, log_warn
    from opcua_memory import (
        debug_read_value,
        debug_write_value,
        initialize_variable_cache,
    )
    from opcua_types import VariableMetadata, VariableNode
    from opcua_utils import (
        convert_value_for_opcua,
        convert_value_for_plc,
        map_plc_to_opcua_type,
    )


# Address tuple type alias for clarity.
Addr = Tuple[int, int]

_VALUE_ATTR = ua.AttributeIds.Value


class SynchronizationManager:
    """Request-driven OPC-UA ↔ PLC value bridge (see module docstring)."""

    def __init__(
        self,
        args: Any,
        variable_nodes: Dict[Addr, VariableNode],
        server: Optional[Server] = None,
    ):
        """
        Args:
            args: PluginRuntimeArgs ctypes struct — exposes debug_read,
                  debug_write, debug_set, debug_size, debug_array_count.
            variable_nodes: Map (arr, elem) → VariableNode produced by
                            AddressSpaceBuilder.
            server: asyncua Server. Required for the value hooks and the
                    subscription push.
        """
        self.args = args
        self.variable_nodes = variable_nodes
        self.server = server

        # Metadata cache (size + datatype) keyed by (arr, elem). Empty
        # until the first PLC-loaded cycle reaches initialize.
        self.variable_metadata: Dict[Addr, VariableMetadata] = {}

        # Pre-filtered subset of variable_nodes that are writable.
        self._readwrite_nodes: Dict[Addr, VariableNode] = {}

        # Cycle timestamp for OPC-UA subscription notifications.
        self._cycle_timestamp: Optional[datetime] = None

        # Track no-PLC log to avoid spam.
        self._logged_no_plc_warning: bool = False

        # ids() of DataValue objects WE are currently pushing, so the
        # value_setter can tell our own echo from a real client write.
        # Robust across awaits: a client write is a different object.
        self._push_dv_ids: Set[int] = set()

        # Whether per-node subscription introspection is available. Probed
        # once in initialize(); when False the push falls back to all nodes
        # (correctness over the optimization).
        self._sub_scoping: bool = False

    # -----------------------------------------------------------------
    # Setup
    # -----------------------------------------------------------------

    async def initialize(self) -> bool:
        """Partition nodes, probe subscription introspection, and install
        the per-node read callback / write setter hooks."""
        try:
            self._readwrite_nodes = {
                addr: node
                for addr, node in self.variable_nodes.items()
                if node.access_mode == "readwrite"
            }

            self._sub_scoping = self._probe_subscription_scoping()
            self._register_value_hooks()

            log_debug(
                f"Sync manager: {len(self._readwrite_nodes)} readwrite, "
                f"{len(self.variable_nodes) - len(self._readwrite_nodes)} readonly; "
                f"subscription-scoped push: {self._sub_scoping}"
            )
            return True
        except Exception as e:
            log_error(f"Failed to initialize sync manager: {e}")
            return False

    def _aspace(self) -> Any:
        """The low-level asyncua AddressSpace (or None if unavailable)."""
        try:
            return self.server.iserver.aspace
        except Exception:
            return None

    def _probe_subscription_scoping(self) -> bool:
        """Check that we can read a node's datachange_callbacks dict — the
        signal for 'has an active subscription'. Guards against asyncua
        internals shifting between versions."""
        aspace = self._aspace()
        if aspace is None or not hasattr(aspace, "_nodes"):
            return False
        try:
            for node in self.variable_nodes.values():
                nd = aspace._nodes.get(node.node.nodeid)
                if nd is None:
                    continue
                attval = nd.attributes.get(_VALUE_ATTR)
                # The attribute exists and exposes the callbacks dict.
                return attval is not None and hasattr(attval, "datachange_callbacks")
            return False
        except Exception:
            return False

    def _register_value_hooks(self) -> None:
        """Install a live-read value_callback on every node and a
        write-forwarding value_setter on every readwrite node."""
        aspace = self._aspace()
        if aspace is None:
            log_warn("No address space — value hooks not installed; "
                     "falling back to push-only sync")
            return

        # A value_setter is installed on EVERY node — not only readwrite
        # ones. Reason: write_attribute_value() clears value_callback when no
        # setter is present (its else-branch), so our subscription push to a
        # readonly node would otherwise kill that node's live-read callback.
        # The setter forwards to the PLC only for readwrite nodes; on readonly
        # nodes it is a no-op (client writes are already denied upstream by the
        # PreWrite permission callback), but it keeps value_callback alive
        # across pushes.
        for (arr, elem), node in self.variable_nodes.items():
            nodeid = node.node.nodeid
            try:
                aspace.set_attribute_value_callback(
                    nodeid, _VALUE_ATTR, self._make_read_callback(arr, elem, node)
                )
                aspace.set_attribute_value_setter(
                    nodeid, _VALUE_ATTR, self._make_write_setter(arr, elem, node)
                )
            except Exception as e:
                log_error(f"Failed to hook node ({arr},{elem}): {e}")

    def _all_addresses(self) -> list:
        """Expand variable_nodes into the full list of (arr, elem)
        addresses, including every element of every array."""
        addrs: list = []
        for (base_arr, base_elem), node in self.variable_nodes.items():
            length = node.array_length or 0
            if length > 0:
                addrs.extend((base_arr, base_elem + i) for i in range(length))
            else:
                addrs.append((base_arr, base_elem))
        return addrs

    def _populate_metadata(self) -> None:
        """Build the (arr, elem) → metadata cache. Called when the
        plugin transitions from no-PLC to PLC-loaded."""
        addrs = self._all_addresses()
        datatypes: Dict[Addr, str] = {}
        for (base_arr, base_elem), node in self.variable_nodes.items():
            length = node.array_length or 0
            if length > 0:
                for i in range(length):
                    datatypes[(base_arr, base_elem + i)] = node.datatype
            else:
                datatypes[(base_arr, base_elem)] = node.datatype

        self.variable_metadata = initialize_variable_cache(self.args, addrs, datatypes)

    # -----------------------------------------------------------------
    # Reads (client → server): live PLC value at read time
    # -----------------------------------------------------------------

    def _make_read_callback(
        self, base_arr: int, base_elem: int, node: VariableNode
    ) -> Callable[[Any, Any], ua.DataValue]:
        """Build the synchronous value_callback asyncua invokes on a
        client Read. Returns the live PLC value as a DataValue."""
        datatype = node.datatype
        length = node.array_length or 0
        expected_type = map_plc_to_opcua_type(datatype)

        def callback(nodeid: Any, attr: Any) -> ua.DataValue:
            try:
                if length > 0:
                    values = []
                    for i in range(length):
                        v = debug_read_value(self.args, base_arr, base_elem + i, datatype)
                        if v is None:
                            v = self._get_default_value(datatype)
                        values.append(convert_value_for_opcua(datatype, v))
                    variant = ua.Variant(values, expected_type)
                else:
                    v = debug_read_value(self.args, base_arr, base_elem, datatype)
                    if v is None:
                        v = self._get_default_value(datatype)
                    variant = ua.Variant(convert_value_for_opcua(datatype, v), expected_type)
                now = datetime.now(timezone.utc)
                return ua.DataValue(
                    Value=variant,
                    StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
                    SourceTimestamp=now,
                    ServerTimestamp=now,
                )
            except Exception as e:
                log_error(f"Read callback ({base_arr},{base_elem}) failed: {e}")
                return ua.DataValue(
                    StatusCode_=ua.StatusCode(ua.StatusCodes.BadInternalError)
                )

        return callback

    # -----------------------------------------------------------------
    # Writes (client → PLC): forward only real client writes
    # -----------------------------------------------------------------

    def _make_write_setter(
        self, base_arr: int, base_elem: int, node: VariableNode
    ) -> Callable[[Any, Any, ua.DataValue], None]:
        """Build the synchronous value_setter asyncua invokes on a Write.
        Forwards a genuine client write to the PLC; ignores our own
        subscription push (identified by DataValue object identity)."""
        datatype = node.datatype
        length = node.array_length or 0
        writable = node.access_mode == "readwrite"

        def setter(node_data: Any, attr: Any, data_value: ua.DataValue) -> None:
            # Our own PLC→OPC push echoes through here — never write it back.
            if id(data_value) in self._push_dv_ids:
                return
            # Readonly node: keep value_callback alive (the whole reason this
            # setter exists) but never forward to the PLC.
            if not writable:
                return
            try:
                actual = self._extract_opcua_value(data_value)
                if actual is None:
                    return
                if length > 0:
                    if not isinstance(actual, (list, tuple)):
                        return
                    for i, elem_value in enumerate(actual[:length]):
                        plc_value = convert_value_for_plc(datatype, elem_value)
                        self._write_one((base_arr, base_elem + i), datatype, plc_value)
                else:
                    plc_value = convert_value_for_plc(datatype, actual)
                    self._write_one((base_arr, base_elem), datatype, plc_value)
            except Exception as e:
                log_error(f"Write setter ({base_arr},{base_elem}) failed: {e}")

        return setter

    def _write_one(self, addr: Addr, datatype: str, plc_value: Any) -> None:
        """Soft-write one (arr, elem) leaf via args.debug_write (the runtime
        enqueues it on the debug-write journal — race-free).

        TIME-family values arrive as (tv_sec, tv_nsec) tuples from
        convert_value_for_plc; debug_write_value packs them straight into the
        IEC_TIMESPEC leaf, so no int64-ns recombination is needed here.
        """
        ok = debug_write_value(self.args, addr[0], addr[1], datatype, plc_value)
        if not ok:
            log_error(f"debug_write({addr[0]}, {addr[1]}) failed")

    # -----------------------------------------------------------------
    # Subscription push (server → client)
    # -----------------------------------------------------------------

    async def run(
        self,
        is_running: Callable[[], bool],
        cycle_time_seconds: float,
    ) -> None:
        """Push live PLC values to subscribed nodes until is_running is
        False. Plain reads do NOT need this loop (they are served live by
        the value_callback); it exists only to drive DataChange
        notifications for active subscriptions."""
        log_info(f"Starting subscription push loop (cycle: {cycle_time_seconds * 1000:.0f}ms)")

        while is_running():
            try:
                array_count = self.args.debug_array_count()
                if array_count == 0:
                    if not self._logged_no_plc_warning:
                        log_info("No PLC program loaded, sync paused")
                        self._logged_no_plc_warning = True
                    await asyncio.sleep(cycle_time_seconds)
                    continue

                if self._logged_no_plc_warning:
                    log_info("PLC program detected, resuming sync")
                    self._logged_no_plc_warning = False
                if not self.variable_metadata:
                    self._populate_metadata()
                    if not self.variable_metadata:
                        await asyncio.sleep(cycle_time_seconds)
                        continue

                self._cycle_timestamp = datetime.now(timezone.utc)
                await self._push_subscribed(is_running)
                await asyncio.sleep(cycle_time_seconds)

            except asyncio.CancelledError:
                log_debug("Sync loop cancelled")
                break
            except Exception as e:
                log_error(f"Error in sync loop: {e}")
                await asyncio.sleep(0.1)

        log_info("Sync loop stopped")

    async def _push_subscribed(self, is_running: Callable[[], bool]) -> None:
        """Read the live PLC value of every subscribed node and push it so
        asyncua emits DataChange notifications. Subscription-scoped when
        introspection is available; otherwise pushes all nodes."""
        for (base_arr, base_elem), node in self.variable_nodes.items():
            if not is_running():
                break
            if self._sub_scoping and not self._has_subscribers(node):
                continue
            try:
                if node.array_length and node.array_length > 0:
                    await self._push_array_node(node, base_arr, base_elem)
                else:
                    value = debug_read_value(self.args, base_arr, base_elem, node.datatype)
                    if value is None:
                        continue
                    await self._push_scalar_node(node, value)
            except Exception as e:
                log_error(f"Failed to push node ({base_arr},{base_elem}): {e}")

    def _has_subscribers(self, node: VariableNode) -> bool:
        """True if the node's Value attribute has at least one monitored
        item (DataChange callback) registered by a client subscription."""
        aspace = self._aspace()
        if aspace is None:
            return True  # can't tell → push (fail open, reads still cheap)
        try:
            nd = aspace._nodes.get(node.node.nodeid)
            if nd is None:
                return False
            attval = nd.attributes.get(_VALUE_ATTR)
            return bool(attval and attval.datachange_callbacks)
        except Exception:
            return True

    async def _push_value(self, node: VariableNode, variant: ua.Variant) -> None:
        """Push one variant via write_attribute_value, tagging the DataValue
        so our own value_setter ignores it."""
        data_value = ua.DataValue(
            Value=variant,
            StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
            SourceTimestamp=self._cycle_timestamp,
            ServerTimestamp=datetime.now(timezone.utc),
        )
        self._push_dv_ids.add(id(data_value))
        try:
            await self.server.write_attribute_value(node.node.nodeid, data_value)
        finally:
            self._push_dv_ids.discard(id(data_value))

    async def _push_scalar_node(self, node: VariableNode, value: Any) -> None:
        opcua_value = convert_value_for_opcua(node.datatype, value)
        expected_type = map_plc_to_opcua_type(node.datatype)
        await self._push_value(node, ua.Variant(opcua_value, expected_type))

    async def _push_array_node(
        self, node: VariableNode, base_arr: int, base_elem: int
    ) -> None:
        length = node.array_length or 0
        values = []
        for i in range(length):
            v = debug_read_value(self.args, base_arr, base_elem + i, node.datatype)
            if v is None:
                v = self._get_default_value(node.datatype)
            values.append(convert_value_for_opcua(node.datatype, v))
        expected_type = map_plc_to_opcua_type(node.datatype)
        await self._push_value(node, ua.Variant(values, expected_type))

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _get_default_value(datatype: str) -> Any:
        dtype = (datatype or "").upper()
        if dtype == "BOOL":
            return False
        if dtype in ("REAL", "LREAL"):
            return 0.0
        if dtype == "STRING":
            return ""
        return 0

    @staticmethod
    def _extract_opcua_value(opcua_value: Any) -> Any:
        try:
            # DataValue → Variant → python value
            val = opcua_value
            if hasattr(val, "Value"):
                val = val.Value
            if hasattr(val, "Value"):
                val = val.Value
            return val
        except Exception:
            return None
