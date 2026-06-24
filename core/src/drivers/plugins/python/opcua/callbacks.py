"""
OPC UA Permission Callback Handler.

This module provides role-based access control for OPC-UA server operations
via PreRead and PreWrite callbacks.
"""

import os
import sys
from typing import Dict, Any, Optional

from asyncua import Server, ua
from asyncua.server.internal_server import InternalServer
from asyncua.common.callback import CallbackType

# Add directories to path for module access
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Import logging (handle both package and direct loading)
try:
    from .opcua_logging import log_info, log_warn, log_error, log_debug
except ImportError:
    from opcua_logging import log_info, log_warn, log_error, log_debug

from shared.plugin_config_decode.opcua_config_model import VariablePermissions


class PermissionCallbackHandler:
    """
    Handles OPC-UA read/write permission callbacks.

    Uses PreRead and PreWrite callbacks to enforce role-based
    access control on variable nodes.

    Role permissions:
    - 'r' = read allowed
    - 'w' = write allowed
    - 'rw' = read and write allowed

    Usage:
        handler = PermissionCallbackHandler(node_permissions, nodeid_to_variable)
        await handler.register(server)
    """

    def __init__(
        self,
        node_permissions: Dict[str, VariablePermissions],
        nodeid_to_variable: Dict[Any, str]
    ):
        """
        Initialize the permission callback handler.

        Args:
            node_permissions: Dict mapping node_id string to VariablePermissions
            nodeid_to_variable: Dict mapping NodeId to variable name string
        """
        self.node_permissions = node_permissions
        self.nodeid_to_variable = nodeid_to_variable

    async def register(self, server: Server) -> bool:
        """
        Register callbacks with the server.

        Must be called AFTER server.init() but BEFORE server.start().

        Args:
            server: The asyncua Server instance

        Returns:
            True if callbacks registered successfully
        """
        log_debug("=== REGISTERING PERMISSION CALLBACKS ===")

        try:
            if server.iserver is None:
                log_warn("Server iserver is None, cannot register callbacks")
                return False

            # Register PreWrite callback (synchronous method)
            log_debug("Registering PreWrite callback...")
            server.iserver.subscribe_server_callback(
                CallbackType.PreWrite,
                self._on_pre_write
            )
            log_debug("PreWrite callback registered successfully")

            # Register PreRead callback (synchronous method)
            log_debug("Registering PreRead callback...")
            server.iserver.subscribe_server_callback(
                CallbackType.PreRead,
                self._on_pre_read
            )
            log_debug("PreRead callback registered successfully")

            log_debug(f"Permission callbacks registered for {len(self.node_permissions)} nodes")
            return True

        except Exception as e:
            log_error(f"Failed to register callbacks: {e}")

            # Try alternative callback registration method
            log_debug("Trying alternative callback registration...")
            try:
                if hasattr(server, 'subscribe_server_callback'):
                    server.subscribe_server_callback(
                        CallbackType.PreWrite,
                        self._on_pre_write
                    )
                    server.subscribe_server_callback(
                        CallbackType.PreRead,
                        self._on_pre_read
                    )
                    log_debug("Alternative callback registration successful")
                    return True
                else:
                    log_error("No callback registration method found")
                    return False
            except Exception as e2:
                log_error(f"Alternative callback registration also failed: {e2}")
                return False

    async def _on_pre_read(self, event: Any, dispatcher: Any) -> None:
        """
        Callback for pre-read operations with permission enforcement.

        Checks if the user has read permission ('r') for the requested node.
        Raises ua.UaStatusCodeError(BadUserAccessDenied) to deny access.

        Args:
            event: The callback event containing user and request params
            dispatcher: The event dispatcher
        """
        # Extract user from event
        user = getattr(event, 'user', None)

        # The event contains request_params with ReadValueIds
        if not hasattr(event, 'request_params'):
            return
        if not hasattr(event.request_params, 'NodesToRead'):
            return

        # Process each node being read
        for read_value_id in event.request_params.NodesToRead:
            node_id = read_value_id.NodeId
            simple_node_id = self._resolve_node_id(node_id)

            if not simple_node_id:
                continue

            # Get permissions for this node
            permissions = self._get_permissions_for_node(simple_node_id)
            if not permissions:
                continue

            # Check user's read permission
            if user and hasattr(user, 'openplc_role'):
                user_role = self._normalize_role(user.openplc_role)
                username = getattr(user, 'name', 'unknown')
                role_permission = getattr(permissions, user_role, "")

                if "r" not in str(role_permission):
                    log_warn(f"DENY read for user {username} "
                             f"(role: {user_role}) on node {simple_node_id}")
                    raise ua.UaStatusCodeError(ua.StatusCodes.BadUserAccessDenied)
            else:
                # Anonymous or unauthenticated client - check viewer permissions
                viewer_perm = getattr(permissions, 'viewer', '')
                if "r" not in str(viewer_perm):
                    log_warn(f"DENY read for anonymous client on node {simple_node_id}")
                    raise ua.UaStatusCodeError(ua.StatusCodes.BadUserAccessDenied)

    async def _on_pre_write(self, event: Any, dispatcher: Any) -> None:
        """
        Callback for pre-write operations with permission enforcement.

        Checks if the user has write permission ('w') for the requested node.
        Raises ua.UaStatusCodeError(BadUserAccessDenied) to deny access.

        Server-internal writes (is_external=False) are allowed without
        permission checks as they are privileged runtime operations.

        Args:
            event: The callback event containing user and request params
            dispatcher: The event dispatcher
        """
        # Check if this is an internal server operation (runtime sync)
        # ServerItemCallback has is_external=False for internal operations
        is_external = getattr(event, 'is_external', True)
        if not is_external:
            # log_debug("Internal server write operation - bypassing permission check")
            return

        # Extract user from event
        user = getattr(event, 'user', None)

        # The event contains request_params with WriteValues
        if not hasattr(event, 'request_params'):
            return
        if not hasattr(event.request_params, 'NodesToWrite'):
            return

        # Process each node being written
        for write_value in event.request_params.NodesToWrite:
            node_id = write_value.NodeId
            value = write_value.Value.Value if hasattr(write_value, 'Value') else None

            simple_node_id = self._resolve_node_id(node_id)

            if not simple_node_id:
                # Log for debugging
                log_debug(f"NodeId {node_id} not found in mapping")
                continue

            # Get permissions for this node
            permissions = self._get_permissions_for_node(simple_node_id)

            # Check user's write permission
            if user and hasattr(user, 'openplc_role'):
                user_role = self._normalize_role(user.openplc_role)
                username = getattr(user, 'name', 'unknown')

                if permissions:
                    role_permission = getattr(permissions, user_role, "")

                    if "w" not in str(role_permission):
                        log_warn(f"DENY write for user {username} "
                                 f"(role: {user_role}) on node {simple_node_id}: {value}")
                        raise ua.UaStatusCodeError(ua.StatusCodes.BadUserAccessDenied)
                    else:
                        log_debug(f"ALLOW write for user {username} "
                                  f"(role: {user_role}) on node {simple_node_id}: {value}")
                else:
                    # No permissions configured - deny by default (fail-closed)
                    log_warn(f"DENY write for user {username} "
                             f"(role: {user_role}) on node {simple_node_id}: {value} "
                             f"(no permissions configured)")
                    raise ua.UaStatusCodeError(ua.StatusCodes.BadUserAccessDenied)
            else:
                # Anonymous external client user
                if permissions:
                    viewer_perm = getattr(permissions, 'viewer', '')
                    if "w" not in str(viewer_perm):
                        log_warn(f"DENY write for anonymous client on node {simple_node_id}")
                        raise ua.UaStatusCodeError(ua.StatusCodes.BadUserAccessDenied)
                    log_debug(f"ALLOW write for anonymous client on node {simple_node_id}: {value}")
                else:
                    # No permissions configured - deny by default (fail-closed)
                    log_warn(f"DENY write for anonymous client on node {simple_node_id}: {value} "
                             f"(no permissions configured)")
                    raise ua.UaStatusCodeError(ua.StatusCodes.BadUserAccessDenied)

    def _resolve_node_id(self, node_id: Any) -> Optional[str]:
        """
        Resolve NodeId to variable name string.

        Tries multiple resolution strategies:
        1. Direct lookup in nodeid_to_variable mapping
        2. String comparison of NodeId
        3. Parse NodeId string format

        Args:
            node_id: The NodeId to resolve

        Returns:
            Variable name string or None if not found
        """
        # Try direct lookup in mapping
        for mapped_node, var_name in self.nodeid_to_variable.items():
            if node_id == mapped_node:
                return var_name
            if str(node_id) == str(mapped_node):
                return var_name

        # Try to parse NodeId string format
        node_id_str = str(node_id)

        if node_id_str.startswith("ns=") and ";" in node_id_str:
            # Format: ns=2;s=VariableName or ns=2;i=1234
            node_parts = node_id_str.split(";")[-1]
            if "=" in node_parts:
                simple_node_id = node_parts.split("=", 1)[-1]
            else:
                simple_node_id = node_parts

            # Check if this matches any stored node_id
            for stored_node_id in self.node_permissions.keys():
                if stored_node_id == simple_node_id:
                    return simple_node_id
                if stored_node_id.endswith(simple_node_id):
                    return stored_node_id

            return simple_node_id

        # Handle NodeId object with Identifier attribute
        if hasattr(node_id, 'Identifier') and hasattr(node_id, 'NamespaceIndex'):
            return f"ns={node_id.NamespaceIndex};i={node_id.Identifier}"

        return None

    def _get_permissions_for_node(self, simple_node_id: str) -> Optional[VariablePermissions]:
        """
        Get permissions for a node.

        Args:
            simple_node_id: The resolved node ID string

        Returns:
            VariablePermissions or None if not configured
        """
        # Direct lookup
        if simple_node_id in self.node_permissions:
            return self.node_permissions[simple_node_id]

        # Try suffix match for struct fields
        for stored_node_id, perms in self.node_permissions.items():
            if stored_node_id == simple_node_id:
                return perms
            if stored_node_id.endswith(simple_node_id):
                return perms

        return None

    def _normalize_role(self, role: Any) -> str:
        """
        Normalize role to OpenPLC role string format.

        Handles UserRole enum, string, and other formats.
        Maps asyncua UserRole enum to OpenPLC roles:
        - UserRole.Admin -> "engineer"
        - UserRole.User -> "viewer"

        Args:
            role: The role value (enum, string, or other)

        Returns:
            OpenPLC role string: "viewer", "operator", or "engineer"
        """
        # Handle string roles directly
        if isinstance(role, str):
            role_lower = role.lower()
            # Check if it's a valid OpenPLC role
            if role_lower in ("viewer", "operator", "engineer"):
                return role_lower
            # Handle asyncua role string representations
            if "admin" in role_lower:
                return "engineer"
            if "user" in role_lower:
                return "viewer"
            return role_lower

        # Handle enum with name attribute (like asyncua UserRole)
        if hasattr(role, 'name'):
            role_name = role.name.lower()
            # Map asyncua UserRole to OpenPLC roles
            if role_name == "admin":
                return "engineer"
            if role_name == "user":
                return "viewer"
            # Check if it's already an OpenPLC role name
            if role_name in ("viewer", "operator", "engineer"):
                return role_name
            return role_name

        # Fallback: convert to string and try to extract role
        role_str = str(role).lower()
        if "admin" in role_str:
            return "engineer"
        if "user" in role_str:
            return "viewer"

        # Default to viewer for unknown roles (most restrictive)
        log_warn(f"Unknown role format '{role}', defaulting to 'viewer'")
        return "viewer"
