"""
OPC UA plugin configuration loader.

This module provides configuration loading for the OPC UA plugin,
returning typed OpcuaConfig dataclass for type safety and IDE support.
"""

import json
import sys
import os
from pathlib import Path
from typing import Any, Optional

# Add directories to path for module access
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Import logging (handle both package and direct loading)
try:
    from .opcua_logging import log_debug, log_error, log_info, log_warn
except ImportError:
    from opcua_logging import log_debug, log_error, log_info, log_warn

from shared.plugin_config_decode.opcua_config_model import (
    OpcuaConfig,
    OpcuaMasterConfig,
    ServerConfig,
    SecurityConfig,
    AddressSpace,
    SecurityProfile,
    User,
    OPCUA_CONFIG_MIN_FORMAT_VERSION,
)


def load_config(config_path: str) -> Optional[OpcuaConfig]:
    """
    Load OPC UA configuration from JSON file.

    Args:
        config_path: Path to configuration file

    Returns:
        OpcuaConfig instance or None if loading fails
    """
    try:
        path = Path(config_path)
        if not path.exists():
            log_error(f"Configuration file not found: {config_path}")
            return None

        # Use OpcuaMasterConfig to load and parse configuration
        master_config = OpcuaMasterConfig()
        master_config.import_config_from_file(config_path)
        master_config.validate()

        if not master_config.plugins:
            log_error("No OPC-UA plugins configured")
            return None

        # Return first plugin's config (single-server approach)
        config = master_config.plugins[0].config

        log_debug(f"Configuration loaded from {config_path}")
        log_debug(f"Server: {config.server.name}")
        log_debug(f"Endpoint: {config.server.endpoint_url}")
        log_debug(f"Variables: {len(config.address_space.variables)}")
        log_debug(f"Structures: {len(config.address_space.structures)}")
        log_debug(f"Arrays: {len(config.address_space.arrays)}")

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


def load_config_from_dict(raw_config: dict) -> Optional[OpcuaConfig]:
    """
    Load OPC UA configuration from a dictionary.

    Useful for testing or when config is provided programmatically.

    Args:
        raw_config: Configuration dictionary

    Returns:
        OpcuaConfig instance or None if parsing fails
    """
    try:
        # Normalize to single-server format
        config_dict = _normalize_config(raw_config)

        # Parse to typed dataclass
        config = OpcuaConfig.from_dict(config_dict)

        return config

    except ValueError as e:
        log_error(f"Configuration parsing error: {e}")
        return None
    except Exception as e:
        log_error(f"Failed to parse configuration: {e}")
        return None


def _normalize_config(raw_config: Any) -> dict:
    """
    Normalize configuration to single-server format.

    Handles both:
    - Old format: List of plugin configurations
    - New format: Single server configuration dictionary
    """
    # If it's a list (old format), extract first plugin's config
    if isinstance(raw_config, list):
        if not raw_config:
            return {}

        first_plugin = raw_config[0]
        if "config" in first_plugin:
            return first_plugin["config"]
        return first_plugin

    # If it's already a dict with "config" key (wrapper format)
    if isinstance(raw_config, dict) and "config" in raw_config:
        return raw_config["config"]

    # Already in new format
    return raw_config


def get_default_config() -> OpcuaConfig:
    """
    Get default configuration for development/testing.

    Returns:
        Default OpcuaConfig instance
    """
    default_dict = {
        "format_version": OPCUA_CONFIG_MIN_FORMAT_VERSION,
        "server": {
            "name": "OpenPLC OPC-UA Server",
            "application_uri": "urn:autonomy-logic:openplc:opcua:server",
            "product_uri": "urn:autonomy-logic:openplc",
            "endpoint_url": "opc.tcp://0.0.0.0:4840",
            "security_profiles": [
                {
                    "name": "insecure",
                    "enabled": True,
                    "security_policy": "None",
                    "security_mode": "None",
                    "auth_methods": ["Anonymous"]
                }
            ]
        },
        "security": {
            "server_certificate_strategy": "auto_self_signed",
            "trusted_client_certificates": []
        },
        "users": [],
        "address_space": {
            "namespace_uri": "urn:openplc:opcua",
            "variables": [],
            "structures": [],
            "arrays": []
        },
        "cycle_time_ms": 100
    }

    return OpcuaConfig.from_dict(default_dict)
