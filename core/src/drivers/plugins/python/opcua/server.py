"""
OPC UA Server Manager.

This module provides the main server orchestration for the OPC UA plugin.
It coordinates all server components and manages the server lifecycle.
"""

import asyncio
import os
import sys
import traceback
from datetime import datetime
from typing import Optional, Dict, Any

from asyncua import Server, ua

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
        await manager.run()  # Blocks until stop() is called
        await manager.stop()
    """

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

        # Security manager
        self.security_manager = OpcuaSecurityManager(config, plugin_dir)

        # User manager for authentication
        self.user_manager = OpenPLCUserManager(config)

        # Client endpoint suggestions (populated after setup)
        self._client_endpoints: Dict[str, str] = {}

        # Address space builder (initialized in _create_address_space)
        self.address_space_builder: Optional[AddressSpaceBuilder] = None

        # Node mappings (populated by address space builder)
        self.variable_nodes: Dict[int, VariableNode] = {}
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

        Blocks until stop() is called or an error occurs.
        """
        try:
            log_info("OpcuaServerManager starting...")

            # Setup server
            if not await self._setup_server():
                log_error("Failed to setup server")
                return

            # Create address space (nodes)
            if not await self._create_address_space():
                log_error("Failed to create address space")
                return

            # Register permission callbacks (AFTER address space, BEFORE start)
            if not await self._register_callbacks():
                log_warn("Failed to register permission callbacks - continuing without access control")

            # Initialize synchronization manager
            if not await self._initialize_sync_manager():
                log_error("Failed to initialize sync manager")
                return

            # Start server
            if not await self._start_server():
                log_error("Failed to start server")
                return

            # Run main loop
            log_info("Server running, entering main loop...")
            await self._main_loop()

        except Exception as e:
            log_error(f"Error in server manager: {e}")
            traceback.print_exc()
        finally:
            await self._cleanup()

    async def stop(self) -> None:
        """
        Stop the server gracefully.

        Sets running flag to False and waits for main loop to exit.
        """
        log_info("Stop requested...")
        self.running = False

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

        Returns:
            True if setup successful, False otherwise
        """
        try:
            # Create server with user manager for authentication
            self.server = Server(user_manager=self.user_manager)

            # Normalize and set endpoint URL (BEFORE init)
            try:
                normalized_endpoint = normalize_endpoint_url(
                    self.config.server.endpoint_url
                )
                self.server.set_endpoint(normalized_endpoint)
                self._client_endpoints = suggest_client_endpoints(normalized_endpoint)
                log_info(f"Server endpoint set to: {normalized_endpoint}")
            except Exception as e:
                log_warn(f"Endpoint normalization failed, using raw URL: {e}")
                self.server.set_endpoint(self.config.server.endpoint_url)

            # Set server name and URIs (BEFORE init)
            self.server.set_server_name(self.config.server.name)
            self.server.application_uri = self.config.server.application_uri

            # Configure security (BEFORE init)
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
            await self.server.init()
            log_info("OPC-UA server initialized")

            # Register namespace (AFTER init)
            self.namespace_idx = await self.server.register_namespace(
                self.config.address_space.namespace_uri
            )
            log_info(
                f"Registered namespace: {self.config.address_space.namespace_uri} "
                f"(index: {self.namespace_idx})"
            )

            # Set build info (AFTER init)
            await self.server.set_build_info(
                product_uri=self.config.server.product_uri,
                manufacturer_name="Autonomy Logic",
                product_name="OpenPLC Runtime",
                software_version="1.0.0",
                build_number="1.0.0.0",
                build_date=datetime.now()
            )

            log_info("OPC-UA server setup completed successfully")
            return True

        except Exception as e:
            log_error(f"Failed to setup OPC-UA server: {e}")
            traceback.print_exc()
            return False

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

            log_info(f"OPC-UA server started on {self.config.server.endpoint_url}")

            # Print alternative endpoints for client connection
            if self._client_endpoints:
                log_info("Alternative client endpoints:")
                for scenario, endpoint in self._client_endpoints.items():
                    if endpoint:
                        log_info(f"  {scenario}: {endpoint}")

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
                is_running=lambda: self.running,
                cycle_time_seconds=cycle_time
            )
        else:
            # Fallback: simple keepalive loop (no sync)
            log_warn("No sync manager - running without synchronization")
            while self.running:
                try:
                    await asyncio.sleep(cycle_time)
                except asyncio.CancelledError:
                    log_info("Main loop cancelled")
                    break

    async def _cleanup(self) -> None:
        """
        Clean up resources.

        Stops the server and releases resources.
        """
        try:
            if self.server and self.running:
                await self.server.stop()
                log_info("OPC-UA server stopped")

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
                self.config
            )

            if not await self.address_space_builder.build():
                return False

            # Copy mappings from builder for easy access
            self.variable_nodes = self.address_space_builder.variable_nodes
            self.node_permissions = self.address_space_builder.node_permissions
            self.nodeid_to_variable = self.address_space_builder.nodeid_to_variable

            log_info(f"Address space created with {len(self.variable_nodes)} nodes")
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
            self.sync_manager = SynchronizationManager(
                buffer_accessor=self.buffer_accessor,
                variable_nodes=self.variable_nodes,
                server=self.server  # Pass server for subscription support
            )

            if not await self.sync_manager.initialize():
                log_warn("Sync manager initialization failed - sync may be limited")
                # Don't fail completely - sync manager can still work with batch ops

            log_info("Synchronization manager initialized")
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
                log_info("No node permissions configured - skipping callback registration")
                return True

            self.callback_handler = PermissionCallbackHandler(
                node_permissions=self.node_permissions,
                nodeid_to_variable=self.nodeid_to_variable
            )

            if not await self.callback_handler.register(self.server):
                log_warn("Callback registration returned False")
                return False

            log_info("Permission callback handler initialized")
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
            log_info("=== ENDPOINT VERIFICATION ===")
            endpoints = await self.server.get_endpoints()
            log_info(f"Total endpoints created: {len(endpoints)}")

            for i, endpoint in enumerate(endpoints):
                log_info(f"Endpoint {i+1}:")
                log_info(f"  URL: {endpoint.EndpointUrl}")
                log_info(f"  Security Policy: {endpoint.SecurityPolicyUri}")
                log_info(f"  Security Mode: {endpoint.SecurityMode}")
                log_info(f"  User Tokens: {len(endpoint.UserIdentityTokens)}")

            log_info("=== END ENDPOINT VERIFICATION ===")
        except Exception as e:
            log_error(f"Error during endpoint verification: {e}")
