# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""
OPC UA Server Manager.

This module provides the main server orchestration for the OPC UA plugin.
It coordinates all server components and manages the server lifecycle.
"""

import asyncio
import os
import socket
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    List,
    Optional,
    Dict,
    Any,
    TypeVar,
)

from asyncua import Server, ua

try:
    from asyncua.server.internal_server import InternalServer
    from asyncua.server.standard_address_space import standard_address_space
except ImportError:
    InternalServer = None
    standard_address_space = None

if TYPE_CHECKING:
    from asyncua.server.address_space import NodeManagementService

# Add directories to path for module access
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Import local modules (handle both package and direct loading)
try:
    from .opcua_logging import log_info, log_warn, log_error, log_debug
    from .opcua_security import OpcuaSecurityManager
    from .opcua_endpoints_config import normalize_endpoint_url, suggest_client_endpoints
    from .address_space import AddressSpaceBuilder
    from .synchronization import SynchronizationManager
    from .user_manager import OpenPLCUserManager
    from .callbacks import PermissionCallbackHandler
    from .opcua_types import VariableNode
except ImportError:
    from opcua_logging import log_info, log_warn, log_error, log_debug
    from opcua_security import OpcuaSecurityManager
    from opcua_endpoints_config import normalize_endpoint_url, suggest_client_endpoints
    from address_space import AddressSpaceBuilder
    from synchronization import SynchronizationManager
    from user_manager import OpenPLCUserManager
    from callbacks import PermissionCallbackHandler
    from opcua_types import VariableNode

from shared import SafeBufferAccess
from shared.plugin_config_decode.opcua_config_model import OpcuaConfig

_PhaseResult = TypeVar("_PhaseResult")
_FillItem = TypeVar("_FillItem")


class StartupStopped(asyncio.CancelledError):
    """
    Raised at a start-up check once a stop was requested. It is a
    cancellation, so no handler of Exception on the way reports it as an
    error, and the start-up phase of run() ends as one cancelled by a stop.
    """


class _StopCheckingNodeService:
    """
    The node management service as asyncua's standard address-space fill
    sees it: checks for a stop before each node and each reference it adds,
    whatever the size of the batch.
    """

    def __init__(self, service: "NodeManagementService", check_stop: Callable[[], None]):
        self._service = service
        self._check_stop = check_stop

    def try_add_nodes(
        self, nodes: Iterable[ua.AddNodesItem], check: bool = True
    ) -> Iterator[ua.AddNodesItem]:
        return self._service.try_add_nodes(self._checked(nodes), check=check)

    def try_add_references(
        self, refs: Iterable[ua.AddReferencesItem]
    ) -> Iterator[ua.AddReferencesItem]:
        return self._service.try_add_references(self._checked(refs))

    def _checked(self, items: Iterable[_FillItem]) -> Iterator[_FillItem]:
        for item in items:
            self._check_stop()
            yield item


def _stoppable_internal_server(
    user_manager: OpenPLCUserManager, check_stop: Callable[[], None]
) -> Optional["InternalServer"]:
    """
    Build an asyncua InternalServer whose standard address-space fill
    calls check_stop before each node and reference.

    Returns:
        The internal server, or None when the asyncua internals it relies
        on (InternalServer.load_standard_address_space, node_mgt_service,
        standard_address_space.fill_address_space) are missing
    """
    fill = getattr(standard_address_space, "fill_address_space", None)
    if (
        InternalServer is None
        or fill is None
        or not hasattr(InternalServer, "load_standard_address_space")
    ):
        return None

    class _StoppableInternalServer(InternalServer):
        async def load_standard_address_space(self, shelf_file: Optional[Path] = None) -> None:
            if shelf_file:
                await super().load_standard_address_space(shelf_file)
                return
            fill(_StopCheckingNodeService(self.node_mgt_service, check_stop))

    iserver = _StoppableInternalServer(user_manager=user_manager)
    if getattr(iserver, "node_mgt_service", None) is None:
        return None
    return iserver


class OpcuaServerManager:
    """
    Manages the OPC-UA server lifecycle and coordinates components.

    Responsibilities:
    - Server initialization and configuration
    - Security setup (certificates, policies)
    - Component orchestration (address space, sync, callbacks)
    - Main run loop
    - Graceful shutdown

    Usage:
        manager = OpcuaServerManager(config, buffer_accessor, plugin_dir)
        await manager.run()  # Blocks until a stop is requested
        await manager.stop()  # On the manager's event loop
        manager.request_stop()  # From any thread, even while the start-up blocks the loop
    """

    SERVER_STOP_TIMEOUT_S = 1.0

    def __init__(
        self,
        config: OpcuaConfig,
        buffer_accessor: SafeBufferAccess,
        plugin_dir: str
    ):
        """
        Initialize the server manager.

        Args:
            config: Typed OpcuaConfig instance
            buffer_accessor: SafeBufferAccess for PLC memory operations
            plugin_dir: Directory path for certificates and resources
        """
        self.config = config
        self.buffer_accessor = buffer_accessor
        self.plugin_dir = plugin_dir

        # Server state
        self.server: Optional[Server] = None
        self.running = False
        self.namespace_idx: Optional[int] = None
        self._stop_request = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._phase_task: Optional[asyncio.Future] = None
        self._listener_lock = threading.Lock()
        self._listener: Optional[asyncio.AbstractServer] = None
        self._listener_loop: Optional[asyncio.AbstractEventLoop] = None
        self._listener_sockets: List[socket.socket] = []
        self._listener_closed = False

        # Security manager
        self.security_manager = OpcuaSecurityManager(
            config, plugin_dir, stop_check=self._check_stop
        )

        # User manager for authentication
        self.user_manager = OpenPLCUserManager(config)

        # Client endpoint suggestions (populated after setup)
        self._client_endpoints: Dict[str, str] = {}

        # Address space builder (initialized in _create_address_space)
        self.address_space_builder: Optional[AddressSpaceBuilder] = None

        # Node mappings (populated by address space builder).
        # Variables are keyed by (arr, elem) tuples — the same
        # address the runtime's debug_read / debug_write thunks take.
        self.variable_nodes: Dict[Any, VariableNode] = {}
        self.node_permissions: Dict[str, Any] = {}
        self.nodeid_to_variable: Dict[Any, str] = {}

        # Synchronization manager (initialized after address space)
        self.sync_manager: Optional[SynchronizationManager] = None

        # Permission callback handler (initialized after address space)
        self.callback_handler: Optional[PermissionCallbackHandler] = None

    async def run(self) -> None:
        """
        Initialize, start, and run the server.

        This is the main entry point that:
        1. Sets up the server with security
        2. Creates address space (Phase 2)
        3. Starts the server
        4. Runs the sync loop (Phase 3)

        Blocks until a stop is requested, through stop() or request_stop(),
        or an error occurs. A stop requested before server.start() begins
        means the server never listens: the start-up checks for the request
        before and after each of its steps and inside the long ones (the
        security setup, asyncua's standard address-space fill, the
        address-space build), even while they block the event loop, and ends
        at the first check after the request. A stop requested while
        server.start() runs does not interrupt it: the port opens, and run()
        then goes to its cleanup, which closes it within the grace period. A
        stopped start-up is not an error: run() logs one DEBUG line, cleans up
        and returns.
        """
        self._loop = asyncio.get_running_loop()
        try:
            if self._stop_request.is_set():
                return

            log_info("OpcuaServerManager starting...")

            prepared = await self._run_phase(self._prepare())
            if prepared is False:
                return

            # Start server
            if prepared is None or self._stop_request.is_set():
                log_debug("OPC-UA start-up stopped before the server listened")
                return
            if not await self._start_server():
                log_error("Failed to start server")
                return

            # Run main loop
            if self._stop_request.is_set():
                return
            log_debug("Server running, entering main loop...")
            await self._run_phase(self._main_loop())

        except Exception as e:
            log_error(f"Error in server manager: {e}")
            traceback.print_exc()
        finally:
            await self._cleanup()
            self._loop = None

    async def stop(self) -> None:
        """
        Request the stop of run(), on the manager's event loop.

        Makes the same request as request_stop() and cancels the start-up
        phase still in progress or the main loop, so run() goes straight
        to its cleanup, which closes the client sessions and the listening
        port.
        """
        log_debug("Stop requested...")
        self._stop_request.set()
        self._cancel_phase()

    def request_stop(self) -> None:
        """
        Request the stop of run(), from any thread, at any time.

        Sets a thread-safe flag that the start-up checks without its event
        loop, so a start-up that blocks the loop ends at its next check and
        never listens. While run() is running, the cancellation stop() does
        is also scheduled on its event loop, so a running server goes to
        its cleanup. Never waits on the event loop. Idempotent.
        """
        self._stop_request.set()
        loop = self._loop
        if loop is None:
            return
        if self._runs_on(loop):
            self._cancel_phase()
            return
        try:
            loop.call_soon_threadsafe(self._cancel_phase)
        except RuntimeError as e:
            log_debug(f"Stop not scheduled on the server event loop: {e}")

    def _cancel_phase(self) -> None:
        """Cancel the phase of run() in progress, if any."""
        if self._phase_task is not None and not self._phase_task.done():
            self._phase_task.cancel()

    def _check_stop(self) -> None:
        """
        Start-up check, thread-safe and with no event loop needed.

        Raises:
            StartupStopped: a stop was requested
        """
        if self._stop_request.is_set():
            raise StartupStopped()

    async def _run_phase(self, phase: Awaitable[_PhaseResult]) -> Optional[_PhaseResult]:
        """
        Run one phase of run() as a task that stop() can cancel.

        Returns:
            The phase result, or None when the phase was cancelled by a stop
            or ended at a start-up check
        """
        self._phase_task = asyncio.ensure_future(phase)
        try:
            return await self._phase_task
        except asyncio.CancelledError:
            if not self._stop_request.is_set():
                raise
            return None
        finally:
            self._phase_task = None

    async def _prepare(self) -> bool:
        """
        Set up the server, its address space, permission callbacks and
        sync manager, everything that comes before it listens. Permission
        callbacks are registered after the address space and before the
        server starts. Checks for a stop request before and after each step.

        Returns:
            True if the server is ready to start, False otherwise

        Raises:
            StartupStopped: a stop was requested
        """
        self._check_stop()
        if not await self._setup_server():
            log_error("Failed to setup server")
            return False

        self._check_stop()
        if not await self._create_address_space():
            log_error("Failed to create address space")
            return False

        self._check_stop()
        if not await self._register_callbacks():
            log_warn("Failed to register permission callbacks - continuing without access control")

        self._check_stop()
        if not await self._initialize_sync_manager():
            log_error("Failed to initialize sync manager")
            return False

        self._check_stop()
        return True

    def close_listener(self) -> None:
        """
        Close the listening port now, from any thread, and keep it closed.

        The asyncio listener is closed on its event loop, and the duplicates
        of its sockets are shut down at OS level, which refuses new
        connections even when the event loop is stuck. A listener the
        server opens afterwards is closed as soon as it is opened.
        Idempotent.
        """
        with self._listener_lock:
            self._listener_closed = True
            listener, loop = self._listener, self._listener_loop
            sockets, self._listener_sockets = self._listener_sockets, []
            self._listener, self._listener_loop = None, None

        if listener is not None and loop is not None:
            if self._runs_on(loop):
                listener.close()
            else:
                try:
                    loop.call_soon_threadsafe(listener.close)
                except RuntimeError as e:
                    log_debug(f"Listener close not scheduled on its event loop: {e}")

        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                log_debug(f"Listening socket shutdown: {e}")
            sock.close()

    @staticmethod
    def _runs_on(loop: asyncio.AbstractEventLoop) -> bool:
        """True when called from inside the given running event loop."""
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return False

    def _hold_listener(self) -> None:
        """
        Keep the asyncio listener of the started server and a duplicate of
        each of its sockets, for close_listener().
        """
        listener = getattr(getattr(self.server, "bserver", None), "_server", None)
        if listener is None:
            log_warn("Listening server not found; the port closes only through server.stop()")
            return

        sockets: List[socket.socket] = []
        try:
            for sock in listener.sockets:
                sockets.append(sock.dup())
        except OSError as e:
            log_warn(f"Listening sockets not duplicated, no OS-level close on a stuck stop: {e}")
            for held in sockets:
                held.close()
            sockets = []

        with self._listener_lock:
            self._listener = listener
            self._listener_loop = asyncio.get_running_loop()
            self._listener_sockets = sockets
            closed_already = self._listener_closed

        if closed_already:
            self.close_listener()

    async def _setup_server(self) -> bool:
        """
        Configure and initialize the asyncua Server.

        Order of operations (critical for asyncua):
        1. Create Server instance
        2. Set endpoint URL (BEFORE init)
        3. Set server name (BEFORE init)
        4. Configure security (BEFORE init)
        5. Call server.init()
        6. Register namespace (AFTER init)
        7. Set build info (AFTER init)

        Checks for a stop request before and after the security setup,
        server.init() (and inside its standard address-space fill), the
        namespace registration and the build info.

        Returns:
            True if setup successful, False otherwise

        Raises:
            StartupStopped: a stop was requested
        """
        try:
            # Create server with user manager for authentication
            self.server = self._create_server()

            # Normalize and set endpoint URL (BEFORE init)
            try:
                normalized_endpoint = normalize_endpoint_url(
                    self.config.server.endpoint_url
                )
                self.server.set_endpoint(normalized_endpoint)
                self._client_endpoints = suggest_client_endpoints(normalized_endpoint)
                log_debug(f"Server endpoint set to: {normalized_endpoint}")
            except Exception as e:
                log_warn(f"Endpoint normalization failed, using raw URL: {e}")
                self.server.set_endpoint(self.config.server.endpoint_url)

            # Set server name and URIs (BEFORE init)
            self.server.set_server_name(self.config.server.name)
            self.server.application_uri = self.config.server.application_uri

            # Configure security (BEFORE init)
            self._check_stop()
            await self.security_manager.setup_server_security(
                self.server,
                self.config.server.security_profiles,
                app_uri=self.config.server.application_uri
            )

            # Setup certificate validation for client certificates (BEFORE init)
            if self.config.security.trusted_client_certificates:
                await self.security_manager.setup_certificate_validation(
                    self.server,
                    self.config.security.trusted_client_certificates
                )

            # Initialize the server
            self._check_stop()
            await self.server.init()
            log_debug("OPC-UA server initialized")

            # Register namespace (AFTER init)
            self._check_stop()
            self.namespace_idx = await self.server.register_namespace(
                self.config.address_space.namespace_uri
            )
            log_debug(
                f"Registered namespace: {self.config.address_space.namespace_uri} "
                f"(index: {self.namespace_idx})"
            )

            # Set build info (AFTER init)
            self._check_stop()
            await self.server.set_build_info(
                product_uri=self.config.server.product_uri,
                manufacturer_name="Autonomy Logic",
                product_name="OpenPLC Runtime",
                software_version="1.0.0",
                build_number="1.0.0.0",
                build_date=datetime.now()
            )
            self._check_stop()

            log_debug("OPC-UA server setup completed successfully")
            return True

        except Exception as e:
            log_error(f"Failed to setup OPC-UA server: {e}")
            traceback.print_exc()
            return False

    def _create_server(self) -> Server:
        """
        Create the asyncua Server with the user manager, on an internal
        server whose standard address-space fill checks for a stop request
        before each node and reference. When the asyncua internals this
        relies on are missing, logs a warning and creates a plain Server,
        whose fill a stop request cannot interrupt.
        """
        iserver = _stoppable_internal_server(self.user_manager, self._check_stop)
        if iserver is None:
            log_warn(
                "asyncua internals for a stoppable address-space fill not found; "
                "a stop during the server start-up waits for the fill"
            )
            return Server(user_manager=self.user_manager)
        return Server(iserver=iserver, user_manager=self.user_manager)

    async def _start_server(self) -> bool:
        """
        Start the OPC-UA server.

        Returns:
            True if server started successfully, False otherwise
        """
        try:
            if not self.server:
                log_error("Server not initialized")
                return False

            await self.server.start()
            self.running = True
            self._hold_listener()

            log_info(f"OPC-UA server started on {self.config.server.endpoint_url}")

            # Print alternative endpoints for client connection
            if self._client_endpoints:
                log_debug("Alternative client endpoints:")
                for scenario, endpoint in self._client_endpoints.items():
                    if endpoint:
                        log_debug(f"  {scenario}: {endpoint}")

            return True

        except Exception as e:
            log_error(f"Failed to start OPC-UA server: {e}")
            traceback.print_exc()
            return False

    async def _main_loop(self) -> None:
        """
        Main server loop with bidirectional synchronization.

        Runs the sync manager's unified sync loop which handles:
        1. OPC-UA → Runtime (client writes to PLC)
        2. Runtime → OPC-UA (PLC values to clients)
        """
        cycle_time = self.config.cycle_time_ms / 1000.0

        if self.sync_manager:
            # Run the sync manager's loop (blocks until stopped)
            await self.sync_manager.run(
                is_running=lambda: self.running and not self._stop_request.is_set(),
                cycle_time_seconds=cycle_time
            )
        else:
            # Fallback: simple keepalive loop (no sync)
            log_warn("No sync manager - running without synchronization")
            while self.running and not self._stop_request.is_set():
                try:
                    await asyncio.sleep(cycle_time)
                except asyncio.CancelledError:
                    log_debug("Main loop cancelled")
                    break

    async def _cleanup(self) -> None:
        """
        Clean up resources.

        Closes the listening port first, through close_listener(), then
        stops the server and releases resources. asyncua's server.stop()
        closes the client connections at once, then waits on its session
        and internal-server tasks, up to about 1 s each; it is capped at
        SERVER_STOP_TIMEOUT_S so the whole stop fits in the plugin's 2 s
        grace period whatever the clients do.
        """
        try:
            self.close_listener()

            if self.server and self.running:
                try:
                    await asyncio.wait_for(
                        self.server.stop(), timeout=self.SERVER_STOP_TIMEOUT_S
                    )
                    log_info("OPC-UA server stopped")
                except asyncio.TimeoutError:
                    log_debug(
                        f"OPC-UA server.stop() cut after {self.SERVER_STOP_TIMEOUT_S}s; "
                        "connections and listening port already closed"
                    )

            self.running = False
            self.server = None

            # Clean up security manager resources (temp directories, etc.)
            if self.security_manager:
                self.security_manager.cleanup()

        except Exception as e:
            log_error(f"Error during cleanup: {e}")

    # -------------------------------------------------------------------------
    # Address Space Creation
    # -------------------------------------------------------------------------

    async def _create_address_space(self) -> bool:
        """
        Create OPC-UA nodes from configuration.

        Uses AddressSpaceBuilder to create all variable nodes
        and stores the resulting mappings for synchronization.

        Returns:
            True if address space created successfully
        """
        try:
            self.address_space_builder = AddressSpaceBuilder(
                self.server,
                self.namespace_idx,
                self.config,
                stop_check=self._check_stop
            )

            if not await self.address_space_builder.build():
                return False

            # Copy mappings from builder for easy access
            self.variable_nodes = self.address_space_builder.variable_nodes
            self.node_permissions = self.address_space_builder.node_permissions
            self.nodeid_to_variable = self.address_space_builder.nodeid_to_variable

            log_debug(f"Address space created with {len(self.variable_nodes)} nodes")
            return True

        except Exception as e:
            log_error(f"Failed to create address space: {e}")
            traceback.print_exc()
            return False

    async def _initialize_sync_manager(self) -> bool:
        """
        Initialize the synchronization manager.

        Creates the sync manager and initializes its metadata cache
        for optimized memory access. Passes server reference for
        optimized subscription notifications via write_attribute_value.

        Returns:
            True if initialization successful
        """
        try:
            # SyncManager talks to the runtime's debug_* C function
            # pointers directly via the underlying ctypes struct.
            # buffer_accessor.runtime_args exposes that struct.
            self.sync_manager = SynchronizationManager(
                args=self.buffer_accessor.runtime_args,
                variable_nodes=self.variable_nodes,
                server=self.server,
                buffer_accessor=self.buffer_accessor,
            )

            if not await self.sync_manager.initialize():
                log_warn("Sync manager initialization failed - sync may be limited")
                # Don't fail completely - sync manager can still work with batch ops

            log_debug("Synchronization manager initialized")
            return True

        except Exception as e:
            log_error(f"Failed to initialize sync manager: {e}")
            traceback.print_exc()
            return False

    async def _register_callbacks(self) -> bool:
        """
        Register permission callbacks for access control.

        Creates the callback handler and registers it with the server.
        Must be called AFTER address space creation and BEFORE server start.

        Returns:
            True if callbacks registered successfully
        """
        try:
            # Only register callbacks if we have nodes with permissions
            if not self.node_permissions:
                log_debug("No node permissions configured - skipping callback registration")
                return True

            self.callback_handler = PermissionCallbackHandler(
                node_permissions=self.node_permissions,
                nodeid_to_variable=self.nodeid_to_variable
            )

            if not await self.callback_handler.register(self.server):
                log_warn("Callback registration returned False")
                return False

            log_debug("Permission callback handler initialized")
            return True

        except Exception as e:
            log_error(f"Failed to register callbacks: {e}")
            traceback.print_exc()
            return False

    # -------------------------------------------------------------------------
    # Debug/Diagnostic Methods
    # -------------------------------------------------------------------------

    async def debug_endpoints(self) -> None:
        """Debug method to verify endpoint configuration."""
        try:
            log_debug("=== ENDPOINT VERIFICATION ===")
            endpoints = await self.server.get_endpoints()
            log_debug(f"Total endpoints created: {len(endpoints)}")

            for i, endpoint in enumerate(endpoints):
                log_debug(f"Endpoint {i+1}:")
                log_debug(f"  URL: {endpoint.EndpointUrl}")
                log_debug(f"  Security Policy: {endpoint.SecurityPolicyUri}")
                log_debug(f"  Security Mode: {endpoint.SecurityMode}")
                log_debug(f"  User Tokens: {len(endpoint.UserIdentityTokens)}")

            log_debug("=== END ENDPOINT VERIFICATION ===")
        except Exception as e:
            log_error(f"Error during endpoint verification: {e}")
