"""
Configuration loading for the OPC-UA Client plugin.

Loads and validates opcua_client.json via OpcuaClientMasterConfig and returns
the first plugin's typed OpcuaClientConfig. Returns None on any failure (the
format_version gate, invalid JSON, validation error) so start_loop can keep the
rest of the runtime up while the client stays down.
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

_current_dir = os.path.dirname(os.path.abspath(__file__))
_python_dir = os.path.dirname(_current_dir)
for _p in (_current_dir, _python_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared.plugin_config_decode.opcua_client_config_model import (  # noqa: E402
    OpcuaClientConfig,
    OpcuaClientMasterConfig,
)

try:
    from .opcua_client_logging import log_debug, log_error
except ImportError:
    from opcua_client_logging import log_debug, log_error


def load_client_config(config_path: str) -> Optional[OpcuaClientConfig]:
    """Load and validate the OPC-UA client configuration.

    Returns the first plugin's OpcuaClientConfig, or None if loading fails.
    """
    try:
        path = Path(config_path)
        if not path.exists():
            log_error(f"Configuration file not found: {config_path}")
            return None

        master = OpcuaClientMasterConfig()
        master.import_config_from_file(config_path)
        master.validate()

        if not master.plugins:
            log_error("No OPC-UA client plugins configured")
            return None

        config = master.plugins[0].config
        log_debug(
            f"OPC-UA client config loaded: {len(config.servers)} server(s), "
            f"format_version={config.format_version}"
        )
        for server in config.servers:
            log_debug(
                f"  server '{server.name}' -> {server.endpoint_url} "
                f"({len(server.mappings)} mapping(s))"
            )
        return config

    except json.JSONDecodeError as e:
        log_error(f"Invalid JSON in configuration file: {e}")
        return None
    except ValueError as e:
        log_error(f"Configuration validation error: {e}")
        return None
    except Exception as e:
        log_error(f"Failed to load configuration: {e}")
        return None
