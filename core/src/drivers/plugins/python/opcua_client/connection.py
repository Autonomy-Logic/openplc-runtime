"""
Remote OPC-UA connection management with bounded exponential backoff.

Async port of the Modbus Master's connect_with_retry pattern: builds the
asyncua Client, applies security, connects, and exposes an interruptible
backoff between reconnect attempts. Sleeps in small increments so a stop
request is honored quickly.
"""

import asyncio
import os
import sys
from typing import Callable, Optional

from asyncua import Client

_current_dir = os.path.dirname(os.path.abspath(__file__))
_python_dir = os.path.dirname(_current_dir)
for _p in (_current_dir, _python_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from .client_security import apply_client_security
    from .opcua_client_logging import log_debug, log_info
except ImportError:
    from client_security import apply_client_security
    from opcua_client_logging import log_debug, log_info


_BACKOFF_START_S = 2.0
_BACKOFF_MAX_S = 30.0
_SLEEP_STEP_S = 0.1


class ClientConnectionManager:
    """Owns the asyncua Client lifecycle for a single remote server."""

    def __init__(self, server_cfg, plugin_dir: str):
        self.cfg = server_cfg
        self.plugin_dir = plugin_dir
        self.client: Optional[Client] = None
        self._backoff = _BACKOFF_START_S

    async def connect(self) -> Client:
        """Build, secure, and connect a fresh Client. Raises on failure."""
        timeout_s = max(1.0, self.cfg.session_timeout_ms / 1000.0)
        client = Client(url=self.cfg.endpoint_url, timeout=timeout_s)

        if not await apply_client_security(
            client, self.cfg.security, self.plugin_dir, self.cfg.name
        ):
            raise RuntimeError(f"security setup failed for '{self.cfg.name}'")

        log_debug(f"Connecting to {self.cfg.endpoint_url} ...")
        await client.connect()
        self.client = client
        log_info(f"Connected to remote server '{self.cfg.name}' ({self.cfg.endpoint_url})")
        return client

    async def disconnect(self) -> None:
        """Best-effort disconnect; never raises."""
        if self.client is None:
            return
        try:
            await self.client.disconnect()
        except Exception as e:
            log_debug(f"disconnect('{self.cfg.name}') ignored error: {e}")
        finally:
            self.client = None

    def reset_backoff(self) -> None:
        self._backoff = _BACKOFF_START_S

    async def backoff_sleep(self, is_running: Callable[[], bool]) -> None:
        """Sleep for the current backoff window, in small interruptible steps,
        then grow the window up to the cap."""
        log_debug(f"Reconnecting to '{self.cfg.name}' in {self._backoff:.0f}s")
        remaining = self._backoff
        while remaining > 0 and is_running():
            step = min(_SLEEP_STEP_S, remaining)
            await asyncio.sleep(step)
            remaining -= step
        self._backoff = min(self._backoff * 2, _BACKOFF_MAX_S)
