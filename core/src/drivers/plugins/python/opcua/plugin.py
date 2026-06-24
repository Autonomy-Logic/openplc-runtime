"""
OPC UA Plugin Entry Point.

This module provides the plugin interface required by the OpenPLC runtime:
- init(args_capsule): Initialize the plugin
- start_loop(): Start the OPC UA server
- stop_loop(): Stop the OPC UA server
- cleanup(): Clean up resources

This is a thin entry point that delegates to the modular components.
"""

import asyncio
import logging
import os
import sys
import threading
from typing import Optional

# Add current and parent directories to path for module access
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)

if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Import shared modules
from shared import (
    SafeBufferAccess,
    SafeLoggingAccess,
    safe_extract_runtime_args_from_capsule,
)
from shared.plugin_config_decode.opcua_config_model import OpcuaConfig

# Import local modules (use absolute imports for runtime compatibility)
try:
    # Try relative imports first (when loaded as package)
    from .config import load_config
    from .opcua_logging import get_logger, log_debug, log_error, log_info, log_warn
    from .server import OpcuaServerManager
except ImportError:
    # Fall back to absolute imports (when loaded directly by runtime)
    from config import load_config
    from opcua_logging import get_logger, log_debug, log_error, log_info, log_warn
    from server import OpcuaServerManager


# Plugin state
_runtime_args = None
_buffer_accessor: Optional[SafeBufferAccess] = None
_config: Optional[OpcuaConfig] = None
_server_manager: Optional[OpcuaServerManager] = None
_server_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_loop: Optional[asyncio.AbstractEventLoop] = None


class _PermissionDenialFilter(logging.Filter):
    """Quiet asyncua's "Error while processing message" traceback when
    the underlying cause is a UaError raised by our pre-read /
    pre-write permission callback.

    The permission system in callbacks.py raises
    ua.UaStatusCodeError(BadUserAccessDenied) to deny a read/write —
    that's the documented asyncua API for rejecting a request, and the
    wire response carries the protocol-correct BadUserAccessDenied
    status code. asyncua's process_message however catches the
    exception with logger.exception(), so the runtime log gets a full
    Python traceback for what is really an expected, well-handled
    permission denial.

    This filter drops the traceback for those specific cases — the
    one-line WARN log emitted by callbacks.py
    ("DENY write for user … on node …") still goes through, so the
    operator has the audit trail without the stack-trace noise.
    Genuine errors from process_message (decode failures, broken
    requests, etc.) keep their traceback.
    """

    # callbacks.py denies a read/write by raising
    # ua.UaStatusCodeError(BadUserAccessDenied); its str() ends in
    # "(BadUserAccessDenied)". Match that to drop the noisy traceback.
    _DENIAL_MARKERS = (
        "BadUserAccessDenied",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc = record.exc_info[1]
            if exc is not None:
                msg = str(exc)
                if any(marker in msg for marker in self._DENIAL_MARKERS):
                    return False  # drop the record
        return True


def _install_asyncua_log_filter() -> None:
    """Install the permission-denial filter on asyncua's processor logger."""
    logging.getLogger("asyncua.server.uaprocessor").addFilter(_PermissionDenialFilter())


def init(args_capsule) -> bool:
    """
    Initialize the OPC UA plugin.

    Called once when the plugin is loaded by the runtime.

    Args:
        args_capsule: PyCapsule containing runtime arguments

    Returns:
        True if initialization successful, False otherwise
    """
    global _runtime_args

    log_info("OPC UA Plugin initializing...")

    try:
        # Extract runtime arguments
        _runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
        if not _runtime_args:
            log_error(f"Failed to extract runtime args: {error_msg}")
            return False

        # Initialize logging with runtime accessor
        logging_accessor = SafeLoggingAccess(_runtime_args)
        if logging_accessor.is_valid:
            get_logger().initialize(logging_accessor)
            log_debug("Logging initialized with runtime accessor")

        _install_asyncua_log_filter()

        log_info("OPC UA Plugin initialized successfully")
        return True

    except Exception as e:
        log_error(f"Initialization error: {e}")
        return False


def start_loop() -> bool:
    """
    Start the OPC UA server.

    Called after successful initialization to start the server.

    Returns:
        True if server started successfully, False otherwise
    """
    global _buffer_accessor, _config, _server_manager, _server_thread

    log_info("Starting OPC UA server...")

    try:
        if not _runtime_args:
            log_error("Plugin not initialized")
            return False

        # Create buffer accessor
        _buffer_accessor = SafeBufferAccess(_runtime_args)
        if not _buffer_accessor.is_valid:
            log_error(f"Failed to create buffer accessor: {_buffer_accessor.error_msg}")
            return False

        log_debug("Buffer accessor created")

        # Load configuration
        config_path, config_error = _buffer_accessor.get_config_path()
        if not config_path:
            log_error(f"Failed to get config path: {config_error}")
            return False

        _config = load_config(config_path)
        if not _config:
            log_error("Failed to load configuration")
            return False

        # Create server manager
        plugin_dir = os.path.dirname(__file__)
        _server_manager = OpcuaServerManager(_config, _buffer_accessor, plugin_dir)

        # Reset stop event
        _stop_event.clear()

        # Start server in background thread
        _server_thread = threading.Thread(
            target=_run_server_thread, daemon=True, name="opcua-server"
        )
        _server_thread.start()

        log_debug("OPC UA server thread started")
        return True

    except Exception as e:
        log_error(f"Failed to start server: {e}")
        return False


def stop_loop() -> bool:
    """
    Stop the OPC UA server.

    Uses a two-phase approach:
    1. Graceful (2s): signal the stop event and wait for the async server to
       shut down cleanly via its monitor task. The sync loop preempts
       between sync directions, so one cycle (~100 ms) is the typical
       graceful exit budget; the 2 s window covers asyncua's internal
       teardown of listening sockets / connected clients.
    2. Forced (3s): if the thread is still alive, cancel all asyncio tasks on
       the event loop and wait again.

    The previous timeout (10 s graceful + 5 s forced) made the editor's
    Stop button feel broken — the runtime appeared frozen for ~10 s
    while asyncua's server.stop() waited on connected clients to
    disconnect. The forced path always succeeded eventually, so the
    grace period was just dead time.

    Returns:
        True if the server thread stopped, False if it survived both phases.
    """
    global _server_thread

    log_info("Stopping OPC UA server...")

    try:
        # Phase 1: Graceful stop — signal and wait briefly.
        _stop_event.set()

        if _server_thread and _server_thread.is_alive():
            _server_thread.join(timeout=2.0)

        if _server_thread and _server_thread.is_alive():
            # Phase 2: force-cancel the asyncio event loop.
            log_warn("Graceful stop did not complete in 2s, forcing cancellation...")
            loop = _loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(_cancel_all_tasks, loop)
            _server_thread.join(timeout=3.0)

        if _server_thread and _server_thread.is_alive():
            log_error("OPC UA server thread did not stop after forced cancellation")
            _server_thread = None
            return False

        _server_thread = None
        log_info("OPC UA server stopped")
        return True

    except Exception as e:
        log_error(f"Error stopping server: {e}")
        return False


def cleanup() -> bool:
    """
    Clean up plugin resources.

    Called when the plugin is being unloaded.

    Returns:
        True if cleanup successful, False otherwise
    """
    global _runtime_args, _buffer_accessor, _config, _server_manager, _server_thread

    log_info("Cleaning up OPC UA plugin...")

    try:
        # Stop server if running
        stop_loop()

        # Clear references
        _runtime_args = None
        _buffer_accessor = None
        _config = None
        _server_manager = None
        _server_thread = None

        log_info("Cleanup completed")
        return True

    except Exception as e:
        log_error(f"Cleanup error: {e}")
        return False


def _cancel_all_tasks(loop):
    """Cancel all running tasks on the event loop.

    Intended to be called from stop_loop() via loop.call_soon_threadsafe().
    """
    for task in asyncio.all_tasks(loop):
        task.cancel()


def _run_server_thread() -> None:
    """
    Server thread main function.

    Runs the async server in a new event loop and stores a reference to the
    loop so that stop_loop() can force-cancel tasks if the graceful shutdown
    times out.
    """
    global _loop

    async def _run_with_stop_check():
        """Run server with stop event monitoring."""
        global _loop
        _loop = asyncio.get_running_loop()

        # Create stop monitoring task
        async def _monitor_stop():
            while not _stop_event.is_set():
                await asyncio.sleep(0.1)

            # Stop requested
            if _server_manager:
                await _server_manager.stop()

        # Run server and stop monitor concurrently
        monitor_task = asyncio.create_task(_monitor_stop())

        try:
            await _server_manager.run()
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
        log_error(f"Server thread error: {e}")
    finally:
        _loop = None


# For backwards compatibility, also export as module-level functions
# that match the old plugin interface
__all__ = ["init", "start_loop", "stop_loop", "cleanup"]
