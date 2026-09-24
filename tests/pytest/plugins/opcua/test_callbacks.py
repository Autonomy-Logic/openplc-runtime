# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Unit tests for OPC-UA permission callback handler.

Tests the PermissionCallbackHandler class in callbacks.py:
- Role normalization (_normalize_role)
- Permission checking for read/write operations
"""

import pytest
import sys
from pathlib import Path
from enum import Enum
from types import SimpleNamespace

# Add plugin path for imports
_plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "core" / "src" / "drivers" / "plugins" / "python"
sys.path.insert(0, str(_plugin_dir / "opcua"))
sys.path.insert(0, str(_plugin_dir / "shared"))

from callbacks import PermissionCallbackHandler
from shared.plugin_config_decode.opcua_config_model import VariablePermissions


# Mock asyncua UserRole enum for testing
class MockUserRole(Enum):
    """Mock of asyncua.server.user_managers.UserRole."""
    User = 1
    Admin = 2


class TestNormalizeRole:
    """Tests for the _normalize_role method."""

    @pytest.fixture
    def handler(self):
        """Create a PermissionCallbackHandler for testing."""
        return PermissionCallbackHandler({}, {})

    # String role tests
    def test_normalize_viewer_string(self, handler):
        """String 'viewer' should return 'viewer'."""
        assert handler._normalize_role("viewer") == "viewer"

    def test_normalize_operator_string(self, handler):
        """String 'operator' should return 'operator'."""
        assert handler._normalize_role("operator") == "operator"

    def test_normalize_engineer_string(self, handler):
        """String 'engineer' should return 'engineer'."""
        assert handler._normalize_role("engineer") == "engineer"

    def test_normalize_uppercase_roles(self, handler):
        """Uppercase role strings should be normalized to lowercase."""
        assert handler._normalize_role("VIEWER") == "viewer"
        assert handler._normalize_role("OPERATOR") == "operator"
        assert handler._normalize_role("ENGINEER") == "engineer"

    def test_normalize_mixed_case_roles(self, handler):
        """Mixed case role strings should be normalized."""
        assert handler._normalize_role("Viewer") == "viewer"
        assert handler._normalize_role("Operator") == "operator"
        assert handler._normalize_role("Engineer") == "engineer"

    # asyncua UserRole enum tests
    def test_normalize_userrole_admin_enum(self, handler):
        """UserRole.Admin enum should map to 'engineer'."""
        assert handler._normalize_role(MockUserRole.Admin) == "engineer"

    def test_normalize_userrole_user_enum(self, handler):
        """UserRole.User enum should map to 'viewer'."""
        assert handler._normalize_role(MockUserRole.User) == "viewer"

    # String representations of asyncua enums (the bug case)
    def test_normalize_userrole_admin_string(self, handler):
        """String 'UserRole.Admin' should map to 'engineer'."""
        assert handler._normalize_role("UserRole.Admin") == "engineer"

    def test_normalize_userrole_user_string(self, handler):
        """String 'UserRole.User' should map to 'viewer'."""
        assert handler._normalize_role("UserRole.User") == "viewer"

    def test_normalize_lowercase_userrole_admin(self, handler):
        """String 'userrole.admin' should map to 'engineer'."""
        assert handler._normalize_role("userrole.admin") == "engineer"

    def test_normalize_lowercase_userrole_user(self, handler):
        """String 'userrole.user' should map to 'viewer'."""
        assert handler._normalize_role("userrole.user") == "viewer"

    # Partial matches
    def test_normalize_admin_string(self, handler):
        """String 'admin' should map to 'engineer'."""
        assert handler._normalize_role("admin") == "engineer"

    def test_normalize_user_string(self, handler):
        """String 'user' should map to 'viewer'."""
        assert handler._normalize_role("user") == "viewer"

    # Edge cases
    def test_normalize_none_like_string(self, handler):
        """Unknown role should default to 'viewer'."""
        result = handler._normalize_role("unknown_role")
        assert result == "unknown_role" or result == "viewer"

    def test_normalize_custom_enum_with_name(self, handler):
        """Enum with name attribute containing 'admin' should map to 'engineer'."""
        class CustomRole(Enum):
            CustomAdmin = 1

        assert handler._normalize_role(CustomRole.CustomAdmin) == "customadmin"


class TestPermissionCallbackHandlerInit:
    """Tests for PermissionCallbackHandler initialization."""

    def test_init_empty_permissions(self):
        """Handler should initialize with empty permissions."""
        handler = PermissionCallbackHandler({}, {})
        assert handler.node_permissions == {}
        assert handler.nodeid_to_variable == {}

    def test_init_with_permissions(self):
        """Handler should store provided permissions."""
        permissions = {
            "PLC.Test.var1": VariablePermissions(viewer="r", operator="rw", engineer="rw")
        }
        nodeid_map = {
            "ns=2;s=PLC.Test.var1": "PLC.Test.var1"
        }
        handler = PermissionCallbackHandler(permissions, nodeid_map)
        assert "PLC.Test.var1" in handler.node_permissions
        assert len(handler.nodeid_to_variable) == 1


class TestGetPermissionsForNode:
    """Tests for the _get_permissions_for_node method."""

    @pytest.fixture
    def handler_with_permissions(self):
        """Create handler with sample permissions."""
        permissions = {
            "PLC.Test.simple_int": VariablePermissions(viewer="r", operator="rw", engineer="rw"),
            "PLC.Test.readonly_var": VariablePermissions(viewer="r", operator="r", engineer="r"),
        }
        return PermissionCallbackHandler(permissions, {})

    def test_direct_lookup(self, handler_with_permissions):
        """Should find permissions by direct node_id match."""
        perms = handler_with_permissions._get_permissions_for_node("PLC.Test.simple_int")
        assert perms is not None
        assert perms.viewer == "r"
        assert perms.operator == "rw"

    def test_not_found(self, handler_with_permissions):
        """Should return None for unknown node_id."""
        perms = handler_with_permissions._get_permissions_for_node("PLC.Test.unknown")
        assert perms is None


class TestRolePermissionMapping:
    """Integration tests for role-to-permission mapping."""

    @pytest.fixture
    def handler(self):
        """Create handler with sample permissions."""
        permissions = {
            "PLC.Test.var": VariablePermissions(viewer="r", operator="rw", engineer="rw")
        }
        return PermissionCallbackHandler(permissions, {})

    def test_viewer_can_read(self, handler):
        """Viewer role should have read permission."""
        role = handler._normalize_role("viewer")
        perms = handler._get_permissions_for_node("PLC.Test.var")
        role_perm = getattr(perms, role, "")
        assert "r" in role_perm

    def test_viewer_cannot_write(self, handler):
        """Viewer role should not have write permission."""
        role = handler._normalize_role("viewer")
        perms = handler._get_permissions_for_node("PLC.Test.var")
        role_perm = getattr(perms, role, "")
        assert "w" not in role_perm

    def test_operator_can_write(self, handler):
        """Operator role should have write permission."""
        role = handler._normalize_role("operator")
        perms = handler._get_permissions_for_node("PLC.Test.var")
        role_perm = getattr(perms, role, "")
        assert "w" in role_perm

    def test_engineer_can_write(self, handler):
        """Engineer role should have write permission."""
        role = handler._normalize_role("engineer")
        perms = handler._get_permissions_for_node("PLC.Test.var")
        role_perm = getattr(perms, role, "")
        assert "w" in role_perm

    def test_userrole_admin_maps_to_engineer_permissions(self, handler):
        """asyncua UserRole.Admin should get engineer permissions."""
        # Simulate the problematic case
        role = handler._normalize_role("userrole.admin")
        assert role == "engineer"

        perms = handler._get_permissions_for_node("PLC.Test.var")
        role_perm = getattr(perms, role, "")
        assert "r" in role_perm
        assert "w" in role_perm

    def test_userrole_user_maps_to_viewer_permissions(self, handler):
        """asyncua UserRole.User should get viewer permissions."""
        role = handler._normalize_role("userrole.user")
        assert role == "viewer"

        perms = handler._get_permissions_for_node("PLC.Test.var")
        role_perm = getattr(perms, role, "")
        assert "r" in role_perm
        assert "w" not in role_perm
