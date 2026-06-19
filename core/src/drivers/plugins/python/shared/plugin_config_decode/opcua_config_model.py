from typing import List, Dict, Any, Optional, Literal
from dataclasses import dataclass
import json
import os

try:
    from .plugin_config_contact import PluginConfigContract
except ImportError:
    # For direct execution
    from plugin_config_contact import PluginConfigContract

# Permission types for variables
PermissionType = Literal["r", "w", "rw"]

# opcua.json contract version this runtime understands. v2 introduced
# compiler-canonical per-leaf `datatype` + `size` (sourced from the same
# STruC++ compile that builds the .so) so the runtime encodes the exact
# byte width instead of re-deriving it from a drift-prone stored datatype.
# A config without `format_version` (or below this) is an older editor's
# output: we refuse it gracefully (OPC-UA stays down; the rest of the PLC
# runs) rather than risk writing the wrong number of bytes to a variable.
# Mirror of OPCUA_CONFIG_FORMAT_VERSION in openplc-editor's
# generate-opcua-config.ts.
OPCUA_CONFIG_MIN_FORMAT_VERSION = 2

# Valid datatypes for OPC-UA variables (IEC 61131-3 base types)
# This list must match the base types supported by openplc-editor
VALID_DATATYPES = frozenset([
    # Boolean
    "BOOL",
    # Signed integers
    "SINT", "INT", "DINT", "LINT",
    # Unsigned integers
    "USINT", "UINT", "UDINT", "ULINT",
    # Floating point
    "REAL", "LREAL",
    # Bit strings
    "BYTE", "WORD", "DWORD", "LWORD",
    # String
    "STRING", "WSTRING",
    # Time-related types
    "TIME", "DATE", "TOD", "DT",
    # Legacy/alternative names (for backward compatibility)
    "INT32", "FLOAT",
])


@dataclass
class SecurityProfile:
    """Configuration for a security profile/endpoint."""
    name: str
    enabled: bool
    security_policy: str
    security_mode: str
    auth_methods: List[str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SecurityProfile':
        """Creates a SecurityProfile instance from a dictionary."""
        try:
            name = data["name"]
            enabled = data["enabled"]
            security_policy = data["security_policy"]
            security_mode = data["security_mode"]
            auth_methods = data["auth_methods"]
        except KeyError as e:
            raise ValueError(f"Missing required field in security profile: {e}")

        return cls(
            name=name,
            enabled=enabled,
            security_policy=security_policy,
            security_mode=security_mode,
            auth_methods=auth_methods
        )

@dataclass
class ServerConfig:
    """OPC-UA server basic configuration."""
    name: str
    application_uri: str
    product_uri: str
    endpoint_url: str
    security_profiles: List[SecurityProfile]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ServerConfig':
        """Creates a ServerConfig instance from a dictionary."""
        try:
            name = data["name"]
            application_uri = data["application_uri"]
            product_uri = data["product_uri"]
            endpoint_url = data["endpoint_url"]
            security_profiles_data = data["security_profiles"]
        except KeyError as e:
            raise ValueError(f"Missing required field in server config: {e}")

        security_profiles = [SecurityProfile.from_dict(sp) for sp in security_profiles_data]

        return cls(
            name=name,
            application_uri=application_uri,
            product_uri=product_uri,
            endpoint_url=endpoint_url,
            security_profiles=security_profiles
        )

@dataclass
class SecurityConfig:
    """Security configuration for certificates and trust."""
    server_certificate_strategy: str
    server_certificate_custom: Optional[str]
    server_private_key_custom: Optional[str]
    trusted_client_certificates: List[Dict[str, str]]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SecurityConfig':
        """Creates a SecurityConfig instance from a dictionary."""
        server_certificate_strategy = data.get("server_certificate_strategy", "auto_self_signed")
        server_certificate_custom = data.get("server_certificate_custom")
        server_private_key_custom = data.get("server_private_key_custom")
        trusted_client_certificates = data.get("trusted_client_certificates", [])

        return cls(
            server_certificate_strategy=server_certificate_strategy,
            server_certificate_custom=server_certificate_custom,
            server_private_key_custom=server_private_key_custom,
            trusted_client_certificates=trusted_client_certificates
        )

@dataclass
class User:
    """User configuration for authentication."""
    type: str
    username: Optional[str]
    password_hash: Optional[str]
    certificate_id: Optional[str]
    role: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'User':
        """Creates a User instance from a dictionary."""
        try:
            user_type = data["type"]
            role = data["role"]
        except KeyError as e:
            raise ValueError(f"Missing required field in user config: {e}")

        username = data.get("username")
        password_hash = data.get("password_hash")
        certificate_id = data.get("certificate_id")

        return cls(
            type=user_type,
            username=username,
            password_hash=password_hash,
            certificate_id=certificate_id,
            role=role
        )

@dataclass
class VariablePermissions:
    """Permissions for a variable per role."""
    viewer: PermissionType
    operator: PermissionType
    engineer: PermissionType

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'VariablePermissions':
        """Creates a VariablePermissions instance from a dictionary."""
        viewer = data.get("viewer", "r")
        operator = data.get("operator", "r")
        engineer = data.get("engineer", "rw")

        return cls(
            viewer=viewer,
            operator=operator,
            engineer=engineer
        )

@dataclass
class VariableField:
    """
    Field within a struct variable.

    Supports nested fields for complex types (FB instances, nested structs).
    When a field has nested fields, its (arr, elem) address will be None
    since only leaf fields have addresses in the debugger Entry tables.
    """
    name: str
    datatype: str
    # None for complex types that have nested fields. Leaf fields carry
    # both — an (arr, elem) tuple addressing the leaf in the runtime's
    # debug Entry tables (debug_dispatch.hpp).
    arr: Optional[int]
    elem: Optional[int]
    permissions: VariablePermissions
    # Canonical leaf byte width from the compiler (debug-map.json). None
    # for complex parents (containers with nested fields), present for
    # leaves. The encode/decode path uses this exact width.
    size: Optional[int] = None
    fields: Optional[List['VariableField']] = None  # Nested fields for complex types

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'VariableField':
        """Creates a VariableField instance from a dictionary."""
        try:
            name = data["name"]
            datatype = data["datatype"]
            arr = data["arr"]
            elem = data["elem"]
            permissions_data = data["permissions"]
        except KeyError as e:
            raise ValueError(f"Missing required field in variable field: {e}")

        permissions = VariablePermissions.from_dict(permissions_data)

        # Parse nested fields if present (recursive)
        nested_fields = None
        if "fields" in data and data["fields"]:
            nested_fields = [VariableField.from_dict(f) for f in data["fields"]]

        # Leaf fields carry a canonical size; complex parents (with nested
        # fields) carry null. A leaf missing `size` is a malformed v2 config.
        size = data.get("size")
        if nested_fields is None and size is None:
            raise ValueError(f"Missing required 'size' for leaf field '{name}'")

        return cls(
            name=name,
            datatype=datatype,
            arr=arr,
            elem=elem,
            permissions=permissions,
            size=size,
            fields=nested_fields
        )

@dataclass
class StructVariable:
    """Struct variable configuration."""
    node_id: str
    browse_name: str
    display_name: str
    description: str
    fields: List[VariableField]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StructVariable':
        """Creates a StructVariable instance from a dictionary."""
        try:
            node_id = data["node_id"]
            browse_name = data["browse_name"]
            display_name = data["display_name"]
            description = data["description"]
            fields_data = data["fields"]
        except KeyError as e:
            raise ValueError(f"Missing required field in struct variable: {e}")

        fields = [VariableField.from_dict(field) for field in fields_data]

        return cls(
            node_id=node_id,
            browse_name=browse_name,
            display_name=display_name,
            description=description,
            fields=fields
        )

@dataclass
class ArrayVariable:
    """Array variable configuration. Elements live at (arr, elem + i)
    for i in 0..length-1; STruC++ guarantees per-array contiguity."""
    node_id: str
    browse_name: str
    display_name: str
    datatype: str
    length: int
    arr: int
    elem: int
    permissions: VariablePermissions
    # Canonical byte width of ONE element from the compiler (debug-map.json).
    size: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ArrayVariable':
        """Creates an ArrayVariable instance from a dictionary."""
        try:
            node_id = data["node_id"]
            browse_name = data["browse_name"]
            display_name = data["display_name"]
            datatype = data["datatype"]
            length = data["length"]
            arr = data["arr"]
            elem = data["elem"]
            permissions_data = data["permissions"]
            size = data["size"]
        except KeyError as e:
            raise ValueError(f"Missing required field in array variable: {e}")

        permissions = VariablePermissions.from_dict(permissions_data)

        return cls(
            node_id=node_id,
            browse_name=browse_name,
            display_name=display_name,
            datatype=datatype,
            length=length,
            arr=arr,
            elem=elem,
            permissions=permissions,
            size=size
        )

@dataclass
class SimpleVariable:
    """Simple variable configuration. Address is (arr, elem) into the
    runtime's debug Entry tables (debug_dispatch.hpp)."""
    node_id: str
    browse_name: str
    display_name: str
    datatype: str
    description: str
    arr: int
    elem: int
    permissions: VariablePermissions
    # Canonical byte width from the compiler (debug-map.json) — the exact
    # number of bytes the encode/decode path moves for this leaf.
    size: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SimpleVariable':
        """Creates a SimpleVariable instance from a dictionary."""
        try:
            node_id = data["node_id"]
            browse_name = data["browse_name"]
            display_name = data["display_name"]
            datatype = data["datatype"]
            description = data["description"]
            arr = data["arr"]
            elem = data["elem"]
            permissions_data = data["permissions"]
            size = data["size"]
        except KeyError as e:
            raise ValueError(f"Missing required field in simple variable: {e}")

        permissions = VariablePermissions.from_dict(permissions_data)

        return cls(
            node_id=node_id,
            browse_name=browse_name,
            display_name=display_name,
            datatype=datatype,
            description=description,
            arr=arr,
            elem=elem,
            permissions=permissions,
            size=size
        )

@dataclass
class AddressSpace:
    """Address space configuration."""
    namespace_uri: str
    variables: List[SimpleVariable]
    structures: List[StructVariable]
    arrays: List[ArrayVariable]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AddressSpace':
        """Creates an AddressSpace instance from a dictionary."""
        try:
            namespace_uri = data["namespace_uri"]
            variables_data = data.get("variables", [])
            structures_data = data.get("structures", [])
            arrays_data = data.get("arrays", [])
        except KeyError as e:
            raise ValueError(f"Missing required field in address space: {e}")

        variables = [SimpleVariable.from_dict(var) for var in variables_data]
        structures = [StructVariable.from_dict(struct) for struct in structures_data]
        arrays = [ArrayVariable.from_dict(arr) for arr in arrays_data]

        return cls(
            namespace_uri=namespace_uri,
            variables=variables,
            structures=structures,
            arrays=arrays
        )

@dataclass
class OpcuaConfig:
    """Complete OPC-UA configuration."""
    server: ServerConfig
    security: SecurityConfig
    users: List[User]
    address_space: AddressSpace
    cycle_time_ms: int = 100  # Default cycle time in milliseconds
    format_version: int = 0  # opcua.json contract version (see gate in from_dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'OpcuaConfig':
        """Creates an OpcuaConfig instance from a dictionary."""
        # Contract gate FIRST, before parsing variables: an older editor's
        # config omits per-leaf `size`, so reject it with a clear message
        # instead of a confusing "missing size" KeyError. Raising here makes
        # load_config() return None -> the OPC-UA server simply doesn't start
        # while the rest of the runtime keeps running.
        format_version = data.get("format_version", 0)
        if not isinstance(format_version, int) or format_version < OPCUA_CONFIG_MIN_FORMAT_VERSION:
            raise ValueError(
                f"Unsupported opcua.json format_version {format_version!r} "
                f"(this runtime requires >= {OPCUA_CONFIG_MIN_FORMAT_VERSION}). The config was "
                f"generated by an older OpenPLC Editor that omits per-variable byte sizes; "
                f"re-upload the project from a current editor to regenerate conf/opcua.json."
            )

        try:
            server_data = data["server"]
            security_data = data["security"]
            users_data = data["users"]
            address_space_data = data["address_space"]
        except KeyError as e:
            raise ValueError(f"Missing required section in OPC-UA config: {e}")

        server = ServerConfig.from_dict(server_data)
        security = SecurityConfig.from_dict(security_data)
        users = [User.from_dict(user) for user in users_data]
        address_space = AddressSpace.from_dict(address_space_data)
        cycle_time_ms = data.get("cycle_time_ms", 100)  # Default 100ms if not specified

        return cls(
            server=server,
            security=security,
            users=users,
            address_space=address_space,
            cycle_time_ms=cycle_time_ms,
            format_version=format_version
        )

@dataclass
class OpcuaPluginConfig:
    """Represents a single OPC-UA plugin configuration."""
    name: str
    protocol: str
    config: OpcuaConfig

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'OpcuaPluginConfig':
        """Creates an OpcuaPluginConfig instance from a dictionary."""
        try:
            name = data["name"]
            protocol = data["protocol"]
            config_data = data["config"]
        except KeyError as e:
            raise ValueError(f"Missing required field in OPC-UA plugin config: {e}")

        config = OpcuaConfig.from_dict(config_data)

        return cls(name=name, protocol=protocol, config=config)

class OpcuaMasterConfig(PluginConfigContract):
    """
    OPC-UA Master configuration model.
    """
    def __init__(self):
        super().__init__()
        self.plugins: List[OpcuaPluginConfig] = []

    def import_config_from_file(self, file_path: str):
        """Read config from a JSON file."""
        with open(file_path, 'r') as f:
            raw_config = json.load(f)

            # Clear any existing plugins
            self.plugins = []

            # Parse each plugin configuration
            for i, plugin_config in enumerate(raw_config):
                try:
                    plugin = OpcuaPluginConfig.from_dict(plugin_config)
                    self.plugins.append(plugin)
                except Exception as e:
                    raise ValueError(f"Failed to parse plugin configuration #{i+1}: {e}")

    def validate(self) -> None:
        """Validates the configuration."""
        if not self.plugins:
            raise ValueError("No plugins configured. At least one OPC-UA plugin must be defined.")

        # Validate each plugin
        for i, plugin in enumerate(self.plugins):
            if plugin.protocol != "OPC-UA":
                raise ValueError(f"Invalid protocol for plugin #{i+1}: {plugin.protocol}. Expected 'OPC-UA'")

            if not plugin.name:
                raise ValueError(f"Plugin #{i+1} has empty name")

            # Validate address space
            address_space = plugin.config.address_space

            # Check for duplicate node_ids
            all_node_ids = []
            all_node_ids.extend([var.node_id for var in address_space.variables])
            all_node_ids.extend([struct.node_id for struct in address_space.structures])
            all_node_ids.extend([arr.node_id for arr in address_space.arrays])

            if len(all_node_ids) != len(set(all_node_ids)):
                raise ValueError(f"Duplicate node_ids found in plugin '{plugin.name}'")

            # Check for duplicate addresses. Variables are addressed by
            # (arr, elem) tuples into the runtime's debug Entry tables.
            def collect_field_addrs(fields: List[VariableField]) -> List[tuple]:
                addrs: List[tuple] = []
                for field in fields:
                    if field.arr is not None and field.elem is not None:
                        addrs.append((field.arr, field.elem))
                    if field.fields:  # Recurse into nested fields
                        addrs.extend(collect_field_addrs(field.fields))
                return addrs

            all_addrs: List[tuple] = []
            all_addrs.extend([(var.arr, var.elem) for var in address_space.variables])
            for struct in address_space.structures:
                all_addrs.extend(collect_field_addrs(struct.fields))
            # Each array claims length consecutive addresses starting
            # at (arr, elem). Expand for the duplicate check so an
            # array overlapping a scalar is caught.
            for ary in address_space.arrays:
                for offset in range(ary.length):
                    all_addrs.append((ary.arr, ary.elem + offset))

            if len(all_addrs) != len(set(all_addrs)):
                raise ValueError(f"Duplicate variable addresses found in plugin '{plugin.name}'")

            # Validate datatypes
            # Helper to validate datatypes recursively for nested fields
            # Only leaf fields (those without nested children) are validated
            def validate_field_datatypes(
                fields: List[VariableField],
                struct_node_id: str,
                plugin_name: str,
                path: str = ""
            ) -> None:
                for field in fields:
                    # Build full path for better error messages
                    current_path = f"{path}.{field.name}" if path else field.name
                    if field.fields:
                        # Complex type with nested fields - recurse into children
                        # Don't validate the parent's datatype (e.g., TON, TOF, custom FB)
                        validate_field_datatypes(
                            field.fields, struct_node_id, plugin_name, current_path
                        )
                    else:
                        # Leaf field - validate its datatype
                        if not field.datatype or field.datatype.upper() not in VALID_DATATYPES:
                            raise ValueError(
                                f"Invalid datatype '{field.datatype}' for field '{current_path}' "
                                f"in struct '{struct_node_id}' in plugin '{plugin_name}'. "
                                f"Valid types: {sorted(VALID_DATATYPES)}"
                            )

            for var in address_space.variables:
                if var.datatype.upper() not in VALID_DATATYPES:
                    raise ValueError(
                        f"Invalid datatype '{var.datatype}' for variable '{var.node_id}' "
                        f"in plugin '{plugin.name}'. Valid types: {sorted(VALID_DATATYPES)}"
                    )
            for struct in address_space.structures:
                validate_field_datatypes(struct.fields, struct.node_id, plugin.name)
            for arr in address_space.arrays:
                if arr.datatype.upper() not in VALID_DATATYPES:
                    raise ValueError(
                        f"Invalid datatype '{arr.datatype}' for array '{arr.node_id}' "
                        f"in plugin '{plugin.name}'. Valid types: {sorted(VALID_DATATYPES)}"
                    )

        # Check for duplicate plugin names
        plugin_names = [plugin.name for plugin in self.plugins]
        if len(plugin_names) != len(set(plugin_names)):
            raise ValueError("Duplicate plugin names found. Each plugin must have a unique name.")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(plugins={len(self.plugins)})"
