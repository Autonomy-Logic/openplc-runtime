"""
OPC UA Address Space Builder.

This module provides the AddressSpaceBuilder class that creates OPC-UA nodes
from configuration. It handles simple variables, structures, and arrays.
"""

import os
import sys
import traceback
from typing import Dict, Any, Optional, Tuple

from asyncua import Server, ua
from asyncua.common.node import Node

# Add directories to path for module access
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Import local modules (handle both package and direct loading)
try:
    from .opcua_logging import log_debug, log_error, log_info, log_warn
    from .opcua_types import VariableNode
    from .opcua_utils import map_plc_to_opcua_type, convert_value_for_opcua
except ImportError:
    from opcua_logging import log_debug, log_error, log_info, log_warn
    from opcua_types import VariableNode
    from opcua_utils import map_plc_to_opcua_type, convert_value_for_opcua

from shared.plugin_config_decode.opcua_config_model import (
    OpcuaConfig,
    SimpleVariable,
    StructVariable,
    VariableField,
    ArrayVariable,
    VariablePermissions,
)


def _type_default(datatype: str) -> Any:
    """Per-type seed value for newly-created OPC-UA nodes. Replaces
    the removed `initial_value` config field — the first sync cycle
    overwrites this with the program's actual current value via
    debug_read, so the seed only shows for one polling tick."""
    t = (datatype or "").upper()
    if t == "BOOL":
        return False
    if t in ("REAL", "LREAL"):
        return 0.0
    if t in ("STRING", "WSTRING"):
        return ""
    return 0


class AddressSpaceBuilder:
    """
    Builds OPC-UA address space from configuration.

    Creates:
    - Simple variable nodes
    - Struct objects with field variables
    - Array variable nodes

    After building, provides mappings for:
    - variable_nodes: Dict[(arr, elem), VariableNode] - address → node mapping
    - node_permissions: Dict[str, VariablePermissions] - node_id to permissions
    - nodeid_to_variable: Dict[Any, str] - NodeId to variable name mapping
    """

    def __init__(
        self,
        server: Server,
        namespace_idx: int,
        config: OpcuaConfig
    ):
        """
        Initialize the address space builder.

        Args:
            server: The asyncua Server instance
            namespace_idx: Namespace index for created nodes
            config: Typed OpcuaConfig instance
        """
        self.server = server
        self.namespace_idx = namespace_idx
        self.config = config

        # Output mappings (populated during build). Keyed by
        # (arr, elem) tuple — the same address space the runtime's
        # debug_read / debug_write / debug_set thunks consume.
        self.variable_nodes: Dict[Tuple[int, int], VariableNode] = {}
        self.node_permissions: Dict[str, VariablePermissions] = {}
        self.nodeid_to_variable: Dict[Any, str] = {}

        # Tracks every string identifier already assigned in this
        # namespace so we can detect config collisions and auto-rename.
        # OPC-UA requires NodeIds to be unique within a namespace.
        self._used_identifiers: set = set()

    def _resolve_identifier(self, desired: str, kind: str) -> Optional[str]:
        """
        Return a collision-free string identifier for the namespace.

        The identifier comes from the config's `node_id` (set by the
        Editor). The Editor already enforces uniqueness, but we re-check
        here as a belt-and-suspenders guard against malformed configs:
        on a collision we emit a warning and append a numeric suffix
        (_1, _2, ...) until the identifier is unique.

        Args:
            desired: The requested identifier (config node_id).
            kind: Human-readable node kind for log messages.

        Returns:
            A unique identifier string, or None when `desired` is empty
            (caller then falls back to a server-assigned numeric NodeId).
        """
        if desired is None or not str(desired).strip():
            log_warn(
                f"Empty node_id for {kind}; falling back to a "
                f"server-assigned numeric NodeId"
            )
            return None

        candidate = str(desired)
        if candidate in self._used_identifiers:
            original = candidate
            suffix = 1
            while f"{original}_{suffix}" in self._used_identifiers:
                suffix += 1
            candidate = f"{original}_{suffix}"
            log_warn(
                f"Duplicate OPC-UA node_id '{original}' in config for "
                f"{kind}; auto-renaming to '{candidate}' to avoid a "
                f"NodeId collision"
            )

        self._used_identifiers.add(candidate)
        return candidate

    def _node_args(self, desired: str, browse_name: str, kind: str):
        """
        Build the (nodeid, browsename) arguments for add_variable/add_object.

        When a config node_id is present, produces an explicit string
        NodeId (ns=<idx>;s=<node_id>) so clients see stable, meaningful
        identifiers. The browse name is passed as an explicit
        QualifiedName in the same namespace — required by asyncua, which
        would otherwise default a bare string browse name to namespace 0.

        When node_id is empty, returns the bare namespace index so
        asyncua assigns a numeric NodeId (legacy behavior).
        """
        identifier = self._resolve_identifier(desired, kind)
        if identifier is None:
            return self.namespace_idx, browse_name
        return (
            ua.NodeId(identifier, self.namespace_idx),
            ua.QualifiedName(browse_name, self.namespace_idx),
        )

    async def build(self) -> bool:
        """
        Create all nodes from configuration.

        Returns:
            True if all nodes created successfully, False on error
        """
        try:
            # Get the Objects folder as parent
            objects = self.server.get_objects_node()

            # Create simple variables
            for var in self.config.address_space.variables:
                try:
                    await self._create_simple_variable(objects, var)
                except Exception as e:
                    log_error(f"Error creating variable {var.node_id}: {e}")
                    traceback.print_exc()

            # Create structures
            for struct in self.config.address_space.structures:
                try:
                    await self._create_struct(objects, struct)
                except Exception as e:
                    log_error(f"Error creating struct {struct.node_id}: {e}")
                    traceback.print_exc()

            # Create arrays
            for arr in self.config.address_space.arrays:
                try:
                    await self._create_array(objects, arr)
                except Exception as e:
                    log_error(f"Error creating array {arr.node_id}: {e}")
                    traceback.print_exc()

            log_debug(f"Created {len(self.variable_nodes)} variable nodes")
            return True

        except Exception as e:
            log_error(f"Failed to create address space: {e}")
            traceback.print_exc()
            return False

    async def _create_simple_variable(
        self,
        parent_node: Node,
        var: SimpleVariable
    ) -> None:
        """
        Create a simple OPC-UA variable node.

        Args:
            parent_node: Parent node (typically Objects folder)
            var: SimpleVariable configuration
        """
        opcua_type = map_plc_to_opcua_type(var.datatype)
        # Type-defaulted seed value. The first sync cycle reads the
        # program's actual current value via debug_read and updates
        # the node — no `initial_value` config field anymore.
        initial_value = convert_value_for_opcua(var.datatype, _type_default(var.datatype))

        # Create the variable node with a string NodeId derived from
        # the config node_id (falls back to auto-numeric if empty).
        nodeid_arg, browse_arg = self._node_args(
            var.node_id, var.browse_name, "variable"
        )
        node = await parent_node.add_variable(
            nodeid_arg,
            browse_arg,
            ua.Variant(initial_value, opcua_type),
            datatype=opcua_type
        )

        # Set display name and description
        await node.write_attribute(
            ua.AttributeIds.DisplayName,
            ua.DataValue(ua.Variant(
                ua.LocalizedText(var.display_name),
                ua.VariantType.LocalizedText
            ))
        )
        await node.write_attribute(
            ua.AttributeIds.Description,
            ua.DataValue(ua.Variant(
                ua.LocalizedText(var.description),
                ua.VariantType.LocalizedText
            ))
        )

        # Set writable if any role has write permission
        has_write_permission = self._check_write_permission(var.permissions)
        if has_write_permission:
            await node.set_writable()
            log_debug(f"Node {var.node_id} set as writable")
        else:
            log_debug(f"Node {var.node_id} set as read-only")

        # Store node mapping
        access_mode = "readwrite" if has_write_permission else "readonly"
        var_node = VariableNode(
            node=node,
            arr=var.arr,
            elem=var.elem,
            datatype=var.datatype,
            access_mode=access_mode,
            is_array_element=False
        )

        self.variable_nodes[(var.arr, var.elem)] = var_node
        self.node_permissions[var.node_id] = var.permissions
        self.nodeid_to_variable[node.nodeid] = var.node_id

        log_debug(f"Created variable {var.node_id} (arr={var.arr}, elem={var.elem})")

    async def _create_struct(
        self,
        parent_node: Node,
        struct: StructVariable
    ) -> None:
        """
        Create an OPC-UA struct (object with fields).

        Args:
            parent_node: Parent node (typically Objects folder)
            struct: StructVariable configuration
        """
        # Create parent object for the struct
        nodeid_arg, browse_arg = self._node_args(
            struct.node_id, struct.browse_name, "struct"
        )
        struct_obj = await parent_node.add_object(
            nodeid_arg,
            browse_arg
        )

        # Set display name and description
        await struct_obj.write_attribute(
            ua.AttributeIds.DisplayName,
            ua.DataValue(ua.Variant(
                ua.LocalizedText(struct.display_name),
                ua.VariantType.LocalizedText
            ))
        )
        await struct_obj.write_attribute(
            ua.AttributeIds.Description,
            ua.DataValue(ua.Variant(
                ua.LocalizedText(struct.description),
                ua.VariantType.LocalizedText
            ))
        )

        # Create fields
        for field in struct.fields:
            await self._create_struct_field(struct_obj, struct.node_id, field)

        log_debug(f"Created struct {struct.node_id} with {len(struct.fields)} fields")

    async def _create_struct_field(
        self,
        parent_node: Node,
        struct_node_id: str,
        field: VariableField
    ) -> None:
        """
        Create a field within a struct.

        Supports nested fields for complex types (FB instances, nested structs).
        When a field has nested fields, creates an Object node and recursively
        creates child fields. Leaf fields (no nested fields) create Variable nodes.

        Args:
            parent_node: Parent struct object node
            struct_node_id: Parent struct's node_id for building field path
            field: VariableField configuration
        """
        field_node_id = f"{struct_node_id}.{field.name}"

        # Check if this is a complex type with nested fields
        if field.fields and len(field.fields) > 0:
            # Create an Object node for complex types (FB instances, nested structs)
            nodeid_arg, browse_arg = self._node_args(
                field_node_id, field.name, "struct field"
            )
            field_obj = await parent_node.add_object(
                nodeid_arg,
                browse_arg
            )

            # Set display name
            await field_obj.write_attribute(
                ua.AttributeIds.DisplayName,
                ua.DataValue(ua.Variant(
                    ua.LocalizedText(field.name),
                    ua.VariantType.LocalizedText
                ))
            )

            # Recursively create nested fields
            for nested_field in field.fields:
                await self._create_struct_field(field_obj, field_node_id, nested_field)

            log_debug(f"Created nested object {field_node_id} with {len(field.fields)} fields")
            return

        # This is a leaf field - create a Variable node
        opcua_type = map_plc_to_opcua_type(field.datatype)
        initial_value = convert_value_for_opcua(field.datatype, _type_default(field.datatype))

        # Create the variable node with a string NodeId built from the
        # struct path (parent node_id + field name).
        nodeid_arg, browse_arg = self._node_args(
            field_node_id, field.name, "struct field"
        )
        node = await parent_node.add_variable(
            nodeid_arg,
            browse_arg,
            ua.Variant(initial_value, opcua_type),
            datatype=opcua_type
        )

        # Set display name
        await node.write_attribute(
            ua.AttributeIds.DisplayName,
            ua.DataValue(ua.Variant(
                ua.LocalizedText(field.name),
                ua.VariantType.LocalizedText
            ))
        )

        # Set writable if any role has write permission
        has_write_permission = self._check_write_permission(field.permissions)
        if has_write_permission:
            await node.set_writable()

        # Store node mapping (only for leaf fields with valid addresses).
        if field.arr is not None and field.elem is not None:
            access_mode = "readwrite" if has_write_permission else "readonly"
            var_node = VariableNode(
                node=node,
                arr=field.arr,
                elem=field.elem,
                datatype=field.datatype,
                access_mode=access_mode,
                is_array_element=False
            )

            self.variable_nodes[(field.arr, field.elem)] = var_node
            self.node_permissions[field_node_id] = field.permissions
            self.nodeid_to_variable[node.nodeid] = field_node_id

            log_debug(f"Created field {field_node_id} (arr={field.arr}, elem={field.elem})")
        else:
            # Complex types (FBs, nested structs) have null addresses;
            # only their leaf children carry (arr, elem).
            log_debug(f"Field {field_node_id} is a complex type (no address) - skipping node mapping")

    async def _create_array(
        self,
        parent_node: Node,
        arr: ArrayVariable
    ) -> None:
        """
        Create an OPC-UA array variable.

        Args:
            parent_node: Parent node (typically Objects folder)
            arr: ArrayVariable configuration
        """
        opcua_type = map_plc_to_opcua_type(arr.datatype)
        initial_value = convert_value_for_opcua(arr.datatype, _type_default(arr.datatype))

        # Create array with initial values
        array_values = [initial_value] * arr.length
        array_variant = ua.Variant(array_values, opcua_type)

        # Create the variable node with a string NodeId derived from
        # the config node_id (falls back to auto-numeric if empty).
        nodeid_arg, browse_arg = self._node_args(
            arr.node_id, arr.browse_name, "array"
        )
        node = await parent_node.add_variable(
            nodeid_arg,
            browse_arg,
            array_variant,
            datatype=opcua_type
        )

        # Set array dimensions - critical for OPC-UA clients to recognize this as an array
        # ValueRank = 1 means OneDimension (one-dimensional array)
        # ArrayDimensions specifies the size of each dimension
        await node.write_attribute(
            ua.AttributeIds.ValueRank,
            ua.DataValue(ua.Variant(1, ua.VariantType.Int32))
        )
        await node.write_attribute(
            ua.AttributeIds.ArrayDimensions,
            ua.DataValue(ua.Variant([arr.length], ua.VariantType.UInt32))
        )

        # Set display name
        await node.write_attribute(
            ua.AttributeIds.DisplayName,
            ua.DataValue(ua.Variant(
                ua.LocalizedText(arr.display_name),
                ua.VariantType.LocalizedText
            ))
        )

        # Set writable if any role has write permission
        has_write_permission = self._check_write_permission(arr.permissions)
        if has_write_permission:
            await node.set_writable()

        # Store node mapping
        access_mode = "readwrite" if has_write_permission else "readonly"
        var_node = VariableNode(
            node=node,
            arr=arr.arr,
            elem=arr.elem,
            datatype=arr.datatype,
            access_mode=access_mode,
            is_array_element=False,
            array_length=arr.length
        )

        # Key the array by its base (arr, elem) — sync loops iterate
        # length consecutive (arr, elem+i) addresses.
        self.variable_nodes[(arr.arr, arr.elem)] = var_node
        self.node_permissions[arr.node_id] = arr.permissions
        self.nodeid_to_variable[node.nodeid] = arr.node_id

        log_debug(
            f"Created array {arr.node_id}[{arr.length}] "
            f"(arr={arr.arr}, elem={arr.elem})"
        )

    def _check_write_permission(self, permissions: VariablePermissions) -> bool:
        """
        Check if any role has write permission.

        Args:
            permissions: VariablePermissions object

        Returns:
            True if any role has write permission
        """
        try:
            if not permissions:
                log_warn("No permissions object provided, defaulting to read-only")
                return False

            # Check each role for write permission
            viewer_perm = getattr(permissions, 'viewer', '')
            operator_perm = getattr(permissions, 'operator', '')
            engineer_perm = getattr(permissions, 'engineer', '')

            has_write = (
                (viewer_perm and 'w' in str(viewer_perm)) or
                (operator_perm and 'w' in str(operator_perm)) or
                (engineer_perm and 'w' in str(engineer_perm))
            )

            return bool(has_write)

        except (AttributeError, TypeError) as e:
            log_warn(f"Invalid permissions object: {e}, defaulting to read-only")
            return False

    def get_variable_indices(self) -> list:
        """Get list of all variable indices for memory cache initialization."""
        return list(self.variable_nodes.keys())
