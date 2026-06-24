"""
OpcuaClientManager — orchestrates one remote OPC-UA server connection.

Lifecycle (per remote server, on its own asyncio loop/thread):

  supervisor loop (run):
    connect -> subscribe read nodes -> run the write/liveness loop
    on any error/disconnect: drop the session and, if reconnect is enabled and
    we are still running, back off and retry with a fresh Client + subscription
    (subscriptions do NOT survive a new session, so they are always rebuilt).

  write/liveness loop (_loop):
    each cycle: gate on PLC-loaded (debug_array_count), probe the connection
    (check_connection raises if the session dropped -> supervisor reconnect),
    then push changed PLC leaves to their remote nodes.

Reads (remote -> PLC) are event-driven via the subscription handler; the loop
only drives writes and liveness. On the no-PLC -> loaded transition we prime
the read mappings once so the PLC picks up current remote values without
waiting for the next remote change.
"""

import asyncio
import os
import sys
from typing import Any, Callable, Dict, List

_current_dir = os.path.dirname(os.path.abspath(__file__))
_python_dir = os.path.dirname(_current_dir)
for _p in (_current_dir, _python_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared.opcua_common.opcua_memory import debug_write_value  # noqa: E402
from shared.opcua_common.opcua_utils import convert_value_for_plc  # noqa: E402

try:
    from .connection import ClientConnectionManager
    from .opcua_client_logging import log_debug, log_error, log_info
    from .subscription_handler import SubscriptionDataHandler
    from .writer import RuntimeToRemoteWriter
except ImportError:
    from connection import ClientConnectionManager
    from opcua_client_logging import log_debug, log_error, log_info
    from subscription_handler import SubscriptionDataHandler
    from writer import RuntimeToRemoteWriter


_DEFAULT_CYCLE_MS = 100
_SLEEP_STEP_S = 0.1


class OpcuaClientManager:
    """Manages a single remote OPC-UA server: connection, subscription, sync."""

    def __init__(self, server_cfg: Any, args: Any, plugin_dir: str):
        self.cfg = server_cfg
        self.args = args
        self.plugin_dir = plugin_dir

        self.read_mappings: List[Any] = [
            m for m in server_cfg.mappings if m.direction == "remote_to_plc"
        ]
        self.write_mappings: List[Any] = [
            m for m in server_cfg.mappings if m.direction == "plc_to_remote"
        ]

        self.running = True
        self._plc_loaded = False
        self._logged_no_plc = False
        self._subscription = None

        self.conn = ClientConnectionManager(server_cfg, plugin_dir)

    # -----------------------------------------------------------------
    # Supervisor
    # -----------------------------------------------------------------

    async def run(self, is_running: Callable[[], bool]) -> None:
        """Connect-and-serve with reconnect until is_running() is False."""
        active = lambda: is_running() and self.running  # noqa: E731

        while active():
            try:
                client = await self.conn.connect()
                self.conn.reset_backoff()

                await self._subscribe(client)
                writer = RuntimeToRemoteWriter(client, self.args, self.write_mappings)
                await self._loop(active, client, writer)
                # Clean exit (stop requested) — leave the supervisor.
                break

            except asyncio.CancelledError:
                log_debug(f"'{self.cfg.name}' run cancelled")
                break
            except Exception as e:
                log_error(f"Connection to '{self.cfg.name}' failed/lost: {e}")
                await self._teardown()
                if not active() or not self.cfg.reconnect:
                    break
                await self.conn.backoff_sleep(active)

        await self._teardown()
        log_info(f"Client manager for '{self.cfg.name}' stopped")

    async def stop(self) -> None:
        """Request a graceful stop (also reachable from the owning thread via
        the is_running predicate)."""
        self.running = False
        await self._teardown()

    async def _teardown(self) -> None:
        if self._subscription is not None:
            try:
                await self._subscription.delete()
            except Exception:
                pass
            self._subscription = None
        await self.conn.disconnect()

    # -----------------------------------------------------------------
    # Subscription (remote -> PLC)
    # -----------------------------------------------------------------

    async def _subscribe(self, client: Any) -> None:
        if not self.read_mappings:
            return

        period_ms = min(m.cycle_time_ms for m in self.read_mappings) or _DEFAULT_CYCLE_MS

        node_map: Dict[str, Any] = {}
        nodes = []
        for m in self.read_mappings:
            node = client.get_node(m.remote_node_id)
            nodes.append(node)
            try:
                node_map[node.nodeid.to_string()] = m
            except Exception:
                node_map[str(node)] = m

        handler = SubscriptionDataHandler(self.args, node_map, lambda: self._plc_loaded)
        self._subscription = await client.create_subscription(period_ms, handler)
        await self._subscription.subscribe_data_change(nodes)
        log_info(
            f"Subscribed {len(nodes)} remote node(s) on '{self.cfg.name}' "
            f"(publish interval {period_ms:.0f}ms)"
        )

    # -----------------------------------------------------------------
    # Write + liveness loop (PLC -> remote)
    # -----------------------------------------------------------------

    async def _loop(
        self, active: Callable[[], bool], client: Any, writer: RuntimeToRemoteWriter
    ) -> None:
        period_s = self._write_period_seconds()

        while active():
            # No PLC program -> debug surface returns 0 arrays. Pause sync
            # (the subscription handler is gated separately).
            if self.args.debug_array_count() == 0:
                if not self._logged_no_plc:
                    log_info(f"No PLC program loaded ('{self.cfg.name}'), sync paused")
                    self._logged_no_plc = True
                self._plc_loaded = False
                await self._sleep(active, period_s)
                continue

            if not self._plc_loaded:
                self._plc_loaded = True
                if self._logged_no_plc:
                    log_info(f"PLC program detected ('{self.cfg.name}'), resuming sync")
                    self._logged_no_plc = False
                writer.reset()
                await self._prime_reads(client)

            # Liveness probe: raises if the session dropped -> supervisor reconnects.
            await client.check_connection()

            await writer.write_once()
            await self._sleep(active, period_s)

    async def _prime_reads(self, client: Any) -> None:
        """One-shot read of every remote_to_plc node so the PLC picks up the
        current remote value immediately on program load (subscriptions only
        notify on subsequent changes)."""
        for m in self.read_mappings:
            try:
                node = client.get_node(m.remote_node_id)
                val = await node.read_value()
                # TIME-family yields a (tv_sec, tv_nsec) tuple that
                # debug_write_value packs into the IEC_TIMESPEC leaf directly.
                plc_value = convert_value_for_plc(m.datatype, val)
                debug_write_value(self.args, m.arr, m.elem, m.datatype, plc_value)
            except Exception as e:
                log_debug(f"prime read of {m.remote_node_id} skipped: {e}")

    def _write_period_seconds(self) -> float:
        cycles = [m.cycle_time_ms for m in self.write_mappings]
        if not cycles:
            # No writes — loop only for liveness; a slow cadence is fine.
            return 1.0
        return (min(cycles) or _DEFAULT_CYCLE_MS) / 1000.0

    @staticmethod
    async def _sleep(active: Callable[[], bool], seconds: float) -> None:
        """Interruptible sleep so a stop request is honored within a step."""
        remaining = seconds
        while remaining > 0 and active():
            step = min(_SLEEP_STEP_S, remaining)
            await asyncio.sleep(step)
            remaining -= step
