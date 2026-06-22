"""
OPC-UA Client Plugin Entry Point.

Implements the plugin interface required by the OpenPLC runtime:
- init(args_capsule): extract runtime args, wire up logging.
- start_loop(): load config, start one daemon thread + asyncio loop per remote
  server, each running an OpcuaClientManager.
- stop_loop(): two-phase shutdown (graceful 2s, then forced task cancel 3s).
- cleanup(): stop everything and drop references.

The runtime acts as an OPC-UA CLIENT here, connecting OUT to remote servers.
Reads (remote -> PLC) are subscription-driven; writes (PLC -> remote) are
change-detected per cycle. See client.py for the per-server orchestration.
"""

import asyncio
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
for _p in (_current_dir, _parent_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared import (  # noqa: E402
    SafeBufferAccess,
    SafeLoggingAccess,
    safe_extract_runtime_args_from_capsule,
)

try:
    from .client import OpcuaClientManager
    from .config import load_client_config
    from .opcua_client_logging import get_logger, log_error, log_info, log_warn
except ImportError:
    from client import OpcuaClientManager
    from config import load_client_config
    from opcua_client_logging import get_logger, log_error, log_info, log_warn


# Plugin state
_runtime_args = None
_buffer_accessor: Optional[SafeBufferAccess] = None
_config = None
_stop_event = threading.Event()
# Per-remote-server runtime state: name -> {"thread", "loop", "manager"}.
_servers: Dict[str, Dict[str, Any]] = {}


def init(args_capsule) -> bool:
    """Initialize the plugin (called once when loaded)."""
    global _runtime_args

    log_info("OPC-UA Client Plugin initializing...")
    try:
        _runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
        if not _runtime_args:
            log_error(f"Failed to extract runtime args: {error_msg}")
            return False

        logging_accessor = SafeLoggingAccess(_runtime_args)
        if logging_accessor.is_valid:
            get_logger().initialize(logging_accessor)

        log_info("OPC-UA Client Plugin initialized successfully")
        return True
    except Exception as e:
        log_error(f"Initialization error: {e}")
        return False


def start_loop() -> bool:
    """Load config and start a manager thread per remote server."""
    global _buffer_accessor, _config

    log_info("Starting OPC-UA client...")
    try:
        if not _runtime_args:
            log_error("Plugin not initialized")
            return False

        _buffer_accessor = SafeBufferAccess(_runtime_args)
        if not _buffer_accessor.is_valid:
            log_error(f"Failed to create buffer accessor: {_buffer_accessor.error_msg}")
            return False

        config_path, config_error = _buffer_accessor.get_config_path()
        if not config_path:
            log_error(f"Failed to get config path: {config_error}")
            return False

        _config = load_client_config(config_path)
        if not _config:
            log_error("Failed to load configuration")
            return False

        _stop_event.clear()
        _servers.clear()
        plugin_dir = os.path.dirname(os.path.abspath(__file__))

        for server_cfg in _config.servers:
            manager = OpcuaClientManager(server_cfg, _buffer_accessor.runtime_args, plugin_dir)
            thread = threading.Thread(
                target=_run_server_thread,
                args=(server_cfg.name,),
                daemon=True,
                name=f"opcua-client-{server_cfg.name}",
            )
            _servers[server_cfg.name] = {"thread": thread, "loop": None, "manager": manager}
            thread.start()
            log_info(f"Started client thread for remote server '{server_cfg.name}'")

        if not _servers:
            log_error("No remote servers configured")
            return False

        return True
    except Exception as e:
        log_error(f"Failed to start client: {e}")
        return False


def stop_loop() -> bool:
    """Two-phase shutdown across all server threads under shared deadlines."""
    log_info("Stopping OPC-UA client...")
    try:
        if not _servers:
            return True

        _stop_event.set()

        # Phase 1: graceful — the managers poll the stop event and disconnect.
        deadline = time.time() + 2.0
        for info in _servers.values():
            thread = info["thread"]
            if thread and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.time()))

        # Phase 2: force-cancel the asyncio tasks of any thread still alive.
        survivors = [
            info for info in _servers.values() if info["thread"] and info["thread"].is_alive()
        ]
        if survivors:
            log_warn("Graceful stop incomplete in 2s, forcing cancellation...")
            for info in survivors:
                loop = info["loop"]
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(_cancel_all_tasks, loop)
            deadline2 = time.time() + 3.0
            for info in survivors:
                thread = info["thread"]
                if thread and thread.is_alive():
                    thread.join(timeout=max(0.0, deadline2 - time.time()))

        any_alive = any(info["thread"].is_alive() for info in _servers.values() if info["thread"])
        _servers.clear()
        if any_alive:
            log_error("Some OPC-UA client threads did not stop")
            return False

        log_info("OPC-UA client stopped")
        return True
    except Exception as e:
        log_error(f"Error stopping client: {e}")
        return False


def cleanup() -> bool:
    """Stop everything and clear references (called on unload)."""
    global _runtime_args, _buffer_accessor, _config

    log_info("Cleaning up OPC-UA client plugin...")
    try:
        stop_loop()
        _runtime_args = None
        _buffer_accessor = None
        _config = None
        _servers.clear()
        log_info("Cleanup completed")
        return True
    except Exception as e:
        log_error(f"Cleanup error: {e}")
        return False


def _cancel_all_tasks(loop) -> None:
    """Cancel all tasks on the loop (called via call_soon_threadsafe)."""
    for task in asyncio.all_tasks(loop):
        task.cancel()


def _run_server_thread(name: str) -> None:
    """Run one remote server's manager on a dedicated asyncio event loop."""
    info = _servers.get(name)
    if info is None:
        return
    manager = info["manager"]

    async def _run_with_stop_check():
        info["loop"] = asyncio.get_running_loop()

        async def _monitor_stop():
            while not _stop_event.is_set():
                await asyncio.sleep(0.1)
            await manager.stop()

        monitor_task = asyncio.create_task(_monitor_stop())
        try:
            await manager.run(lambda: not _stop_event.is_set())
        except asyncio.CancelledError:
            pass
        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_run_with_stop_check())
    except Exception as e:
        log_error(f"Client thread '{name}' error: {e}")
    finally:
        info["loop"] = None


__all__ = ["init", "start_loop", "stop_loop", "cleanup"]
