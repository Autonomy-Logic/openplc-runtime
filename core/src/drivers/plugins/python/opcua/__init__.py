# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
OpenPLC OPC UA Plugin.

This package implements an OPC UA server for the OpenPLC runtime,
providing industrial-grade connectivity using the asyncua library.

Architecture:
    - plugin.py: Entry point with init/start_loop/stop_loop/cleanup
    - config.py: Configuration loading and validation
    - opcua_logging.py: Centralized logging singleton
    - server.py: OpcuaServerManager (main orchestrator)
    - address_space.py: AddressSpaceBuilder (node creation)
    - synchronization.py: SynchronizationManager (bidirectional sync)
    - user_manager.py: OpenPLCUserManager (authentication)
    - callbacks.py: PermissionCallbackHandler (access control)
    - opcua_types.py: Type definitions (VariableNode, VariableMetadata)
    - opcua_utils.py: Utility functions (type mapping, conversion)
    - opcua_security.py: OpcuaSecurityManager (certificates, policies)
    - opcua_memory.py: Direct memory access utilities
    - opcua_endpoints_config.py: Endpoint URL utilities

Usage:
    The plugin is loaded by the OpenPLC runtime plugin system.
    Configuration is provided via JSON file specified in plugins.conf.
"""

# Re-export plugin interface for runtime compatibility
from .plugin import init, start_loop, stop_loop, cleanup

__version__ = "2.0.0"
__all__ = ['init', 'start_loop', 'stop_loop', 'cleanup']
