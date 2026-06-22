"""
OPC-UA Client configuration model.

Mirrors the style of opcua_config_model.py (dataclasses with from_dict,
PluginConfigContract, a format_version gate), but describes the inverse role:
the runtime acts as an OPC-UA *client* connecting OUT to one or more remote
OPC-UA servers. Each NodeMapping binds a remote NodeId to a local PLC debug
leaf (arr, elem) with a direction:

  - "remote_to_plc": subscribe the remote node, write its value into the PLC
    leaf via the debug surface (soft write).
  - "plc_to_remote": read the PLC leaf and write it to the remote node when it
    changes.

This model is pure (no asyncua dependency) so it can be parsed by the webserver
too; the remote NodeId is kept as a string and parsed at runtime.
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    from .opcua_config_model import VALID_DATATYPES
    from .plugin_config_contact import PluginConfigContract
except ImportError:
    # For direct execution
    from opcua_config_model import VALID_DATATYPES
    from plugin_config_contact import PluginConfigContract


# opcua_client.json contract version this runtime understands. v1 already
# carries the compiler-canonical per-leaf `size` (sourced from the same
# STruC++ compile that builds the .so) so the runtime encodes the exact byte
# width instead of re-deriving it from a drift-prone stored datatype. A config
# without `format_version` (or below this) is an older/foreign editor's output:
# we refuse it gracefully (the client stays down; the rest of the PLC runs)
# rather than risk writing the wrong number of bytes to a variable.
OPCUA_CLIENT_CONFIG_MIN_FORMAT_VERSION = 1

# Direction of a node mapping (relative to the PLC).
VALID_DIRECTIONS = frozenset(["remote_to_plc", "plc_to_remote"])

# Client-side authentication modes against the remote server.
VALID_AUTH_MODES = frozenset(["anonymous", "username", "certificate"])

# Security policies / message security modes accepted from config. Mirror of
# the maps in shared.opcua_common.opcua_security_common (kept as plain strings
# here so this model stays asyncua-free).
VALID_SECURITY_POLICIES = frozenset(
    ["None", "Basic256Sha256", "Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss"]
)
VALID_SECURITY_MODES = frozenset(["None", "Sign", "SignAndEncrypt"])


@dataclass
class RemoteEndpointSecurity:
    """How the client secures and authenticates the connection to a remote
    server."""

    security_policy: str = "None"
    security_mode: str = "None"
    auth_mode: str = "anonymous"
    username: Optional[str] = None
    password: Optional[str] = None
    # Client application certificate (required for SignAndEncrypt and for
    # certificate auth). PEM strings; the runtime materializes them to files
    # asyncua can load, or mints a self-signed one when absent.
    client_cert_pem: Optional[str] = None
    client_key_pem: Optional[str] = None
    # Expected server certificate (PEM) for Sign/SignAndEncrypt pinning.
    server_cert_pem: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RemoteEndpointSecurity":
        data = data or {}
        return cls(
            security_policy=data.get("security_policy", "None"),
            security_mode=data.get("security_mode", "None"),
            auth_mode=data.get("auth_mode", "anonymous"),
            username=data.get("username"),
            password=data.get("password"),
            client_cert_pem=data.get("client_cert_pem"),
            client_key_pem=data.get("client_key_pem"),
            server_cert_pem=data.get("server_cert_pem"),
        )


@dataclass
class NodeMapping:
    """Binds a remote OPC-UA NodeId to a local PLC debug leaf (arr, elem)."""

    remote_node_id: str
    arr: int
    elem: int
    datatype: str
    # Canonical leaf byte width from the compiler (debug-map.json) — the exact
    # number of bytes the encode/decode path moves for this leaf.
    size: int
    direction: str
    cycle_time_ms: int = 100

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NodeMapping":
        try:
            remote_node_id = data["remote_node_id"]
            arr = data["arr"]
            elem = data["elem"]
            datatype = data["datatype"]
            size = data["size"]
            direction = data["direction"]
        except KeyError as e:
            raise ValueError(f"Missing required field in node mapping: {e}")

        return cls(
            remote_node_id=remote_node_id,
            arr=arr,
            elem=elem,
            datatype=datatype,
            size=size,
            direction=direction,
            cycle_time_ms=data.get("cycle_time_ms", 100),
        )


@dataclass
class RemoteServerConfig:
    """A single remote OPC-UA server the client connects to."""

    name: str
    endpoint_url: str
    security: RemoteEndpointSecurity
    mappings: List[NodeMapping]
    session_timeout_ms: int = 60000
    reconnect: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RemoteServerConfig":
        try:
            name = data["name"]
            endpoint_url = data["endpoint_url"]
            mappings_data = data["mappings"]
        except KeyError as e:
            raise ValueError(f"Missing required field in remote server config: {e}")

        return cls(
            name=name,
            endpoint_url=endpoint_url,
            security=RemoteEndpointSecurity.from_dict(data.get("security")),
            mappings=[NodeMapping.from_dict(m) for m in mappings_data],
            session_timeout_ms=data.get("session_timeout_ms", 60000),
            reconnect=data.get("reconnect", True),
        )


@dataclass
class OpcuaClientConfig:
    """Complete OPC-UA client configuration (one or more remote servers)."""

    servers: List[RemoteServerConfig]
    format_version: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OpcuaClientConfig":
        # Contract gate FIRST, before parsing mappings: an older/foreign config
        # omits per-leaf `size`, so reject it with a clear message instead of a
        # confusing "missing size" KeyError. Raising here makes load_client_config
        # return None -> the OPC-UA client simply doesn't start while the rest of
        # the runtime keeps running.
        format_version = data.get("format_version", 0)
        if (
            not isinstance(format_version, int)
            or format_version < OPCUA_CLIENT_CONFIG_MIN_FORMAT_VERSION
        ):
            raise ValueError(
                f"Unsupported opcua_client.json format_version {format_version!r} "
                f"(this runtime requires >= {OPCUA_CLIENT_CONFIG_MIN_FORMAT_VERSION}). The config "
                f"was generated by an older OpenPLC Editor that omits per-variable byte sizes; "
                f"re-upload the project from a current editor to regenerate conf/opcua_client.json."
            )

        try:
            servers_data = data["servers"]
        except KeyError as e:
            raise ValueError(f"Missing required section in OPC-UA client config: {e}")

        return cls(
            servers=[RemoteServerConfig.from_dict(s) for s in servers_data],
            format_version=format_version,
        )


@dataclass
class OpcuaClientPluginConfig:
    """Represents a single OPC-UA client plugin configuration."""

    name: str
    protocol: str
    config: OpcuaClientConfig

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OpcuaClientPluginConfig":
        try:
            name = data["name"]
            protocol = data["protocol"]
            config_data = data["config"]
        except KeyError as e:
            raise ValueError(f"Missing required field in OPC-UA client plugin config: {e}")

        return cls(name=name, protocol=protocol, config=OpcuaClientConfig.from_dict(config_data))


class OpcuaClientMasterConfig(PluginConfigContract):
    """OPC-UA Client configuration model (list of client plugins)."""

    def __init__(self):
        super().__init__()
        self.plugins: List[OpcuaClientPluginConfig] = []

    def import_config_from_file(self, file_path: str):
        """Read config from a JSON file (a list of plugin objects)."""
        with open(file_path, "r") as f:
            raw_config = json.load(f)

        self.plugins = []
        for i, plugin_config in enumerate(raw_config):
            try:
                self.plugins.append(OpcuaClientPluginConfig.from_dict(plugin_config))
            except Exception as e:
                raise ValueError(f"Failed to parse plugin configuration #{i + 1}: {e}")

    def validate(self) -> None:
        """Validate the configuration."""
        if not self.plugins:
            raise ValueError(
                "No plugins configured. At least one OPC-UA client plugin must be defined."
            )

        for i, plugin in enumerate(self.plugins):
            if plugin.protocol != "OPC-UA-Client":
                raise ValueError(
                    f"Invalid protocol for plugin #{i + 1}: {plugin.protocol}. "
                    f"Expected 'OPC-UA-Client'"
                )
            if not plugin.name:
                raise ValueError(f"Plugin #{i + 1} has empty name")

            servers = plugin.config.servers
            if not servers:
                raise ValueError(f"Plugin '{plugin.name}' has no remote servers configured")

            server_names = [s.name for s in servers]
            if len(server_names) != len(set(server_names)):
                raise ValueError(f"Duplicate remote server names in plugin '{plugin.name}'")

            for server in servers:
                self._validate_server(plugin.name, server)

        plugin_names = [p.name for p in self.plugins]
        if len(plugin_names) != len(set(plugin_names)):
            raise ValueError("Duplicate plugin names found. Each plugin must have a unique name.")

    @staticmethod
    def _validate_server(plugin_name: str, server: RemoteServerConfig) -> None:
        where = f"server '{server.name}' in plugin '{plugin_name}'"

        if not server.endpoint_url:
            raise ValueError(f"Empty endpoint_url for {where}")

        sec = server.security
        if sec.security_policy not in VALID_SECURITY_POLICIES:
            raise ValueError(
                f"Invalid security_policy '{sec.security_policy}' for {where}. "
                f"Valid: {sorted(VALID_SECURITY_POLICIES)}"
            )
        if sec.security_mode not in VALID_SECURITY_MODES:
            raise ValueError(
                f"Invalid security_mode '{sec.security_mode}' for {where}. "
                f"Valid: {sorted(VALID_SECURITY_MODES)}"
            )
        if sec.auth_mode not in VALID_AUTH_MODES:
            raise ValueError(
                f"Invalid auth_mode '{sec.auth_mode}' for {where}. "
                f"Valid: {sorted(VALID_AUTH_MODES)}"
            )
        if sec.auth_mode == "username" and not sec.username:
            raise ValueError(f"auth_mode 'username' requires a username for {where}")
        if sec.auth_mode == "certificate" and not (sec.client_cert_pem and sec.client_key_pem):
            raise ValueError(
                f"auth_mode 'certificate' requires client_cert_pem and client_key_pem for {where}"
            )

        if not server.mappings:
            raise ValueError(f"No node mappings configured for {where}")

        # A local leaf may be fed by at most one remote source (two remote
        # nodes writing the same PLC leaf would fight each other). plc_to_remote
        # leaves may repeat (one local value can be pushed to many remote nodes).
        remote_to_plc_addrs: List[tuple] = []
        for m in server.mappings:
            if m.datatype.upper() not in VALID_DATATYPES:
                raise ValueError(
                    f"Invalid datatype '{m.datatype}' for node '{m.remote_node_id}' in {where}. "
                    f"Valid: {sorted(VALID_DATATYPES)}"
                )
            if m.direction not in VALID_DIRECTIONS:
                raise ValueError(
                    f"Invalid direction '{m.direction}' for node '{m.remote_node_id}' in {where}. "
                    f"Valid: {sorted(VALID_DIRECTIONS)}"
                )
            if not isinstance(m.size, int) or m.size <= 0:
                raise ValueError(
                    f"Invalid size {m.size!r} for node '{m.remote_node_id}' in {where} "
                    f"(must be a positive integer byte width)"
                )
            if not m.remote_node_id:
                raise ValueError(f"Empty remote_node_id in {where}")
            if m.direction == "remote_to_plc":
                remote_to_plc_addrs.append((m.arr, m.elem))

        if len(remote_to_plc_addrs) != len(set(remote_to_plc_addrs)):
            raise ValueError(
                f"Duplicate (arr, elem) among remote_to_plc mappings in {where} "
                f"(a PLC leaf can have only one remote source)"
            )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(plugins={len(self.plugins)})"
