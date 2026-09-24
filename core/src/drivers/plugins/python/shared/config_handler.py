# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Configuration Handler for OpenPLC Python Plugin System

This module handles plugin-specific configuration file operations.
It provides utilities for reading and parsing configuration files.
"""

import json
import os
from typing import Dict, Tuple
try:
    # Try relative imports first (when used as package)
    from .component_interfaces import IConfigHandler
except ImportError:
    # Fall back to absolute imports (when testing standalone)
    from component_interfaces import IConfigHandler


class ConfigHandler(IConfigHandler):
    """
    Handles plugin-specific configuration file operations.

    This class provides utilities for reading, parsing, and managing
    plugin configuration files in JSON format.
    """

    def __init__(self, runtime_args):
        """
        Initialize the configuration handler.

        Args:
            runtime_args: PluginRuntimeArgs instance containing config path
        """
        self.args = runtime_args

    def get_config_path(self) -> Tuple[str, str]:
        """
        Retrieve the plugin-specific configuration file path.

        Returns:
            Tuple[str, str]: (config_path, error_message)
        """
        try:
            config_path_bytes = self.args.plugin_specific_config_file_path

            # Handle different types of C char arrays
            if isinstance(config_path_bytes, (bytes, bytearray)):
                config_path = config_path_bytes.decode('utf-8').rstrip('\x00')
            elif hasattr(config_path_bytes, 'value'):
                config_path = config_path_bytes.value.decode('utf-8').rstrip('\x00')
            elif hasattr(config_path_bytes, 'raw'):
                config_path = config_path_bytes.raw.decode('utf-8').rstrip('\x00')
            else:
                # Try to convert to bytes first
                config_path = bytes(config_path_bytes).decode('utf-8').rstrip('\x00')

            # Clean up the path - remove all whitespace and control characters
            config_path = config_path.strip()

            return config_path, "Success"

        except (AttributeError, TypeError, ValueError, OSError, MemoryError) as e:
            return "", f"Exception retrieving config path: {e}"

    def get_config_as_map(self) -> Tuple[Dict, str]:
        """
        Parse the plugin-specific configuration file as a key-value map.

        Supports JSON format for flexibility. Returns an empty dict if
        the config file doesn't exist or can't be parsed.

        Returns:
            Tuple[Dict, str]: (config_map, error_message)
        """
        config_path, err_msg = self.get_config_path()
        if not config_path:
            return {}, f"Failed to get config path: {err_msg}"

        # Debug information (could be logged if needed)
        debug_info = f"Config path: '{config_path}', CWD: '{os.getcwd()}'"

        try:
            with open(config_path, 'r', encoding='utf-8') as config_file:
                config_data = json.load(config_file)
                if not isinstance(config_data, dict):
                    return {}, "Configuration file must contain a JSON object at the top level"
                return config_data, "Success"

        except FileNotFoundError:
            return {}, f"Configuration file not found: {config_path}"

        except json.JSONDecodeError as e:
            return {}, f"JSON parsing error in config file {config_path}: {e}"

        except (OSError, MemoryError) as e:
            return {}, f"Exception reading config file {config_path}: {e}"

        except UnicodeDecodeError as e:
            return {}, f"Encoding error reading config file {config_path}: {e}"

    def validate_config_file(self) -> Tuple[bool, str]:
        """
        Validate that the configuration file exists and is readable.

        Returns:
            Tuple[bool, str]: (is_valid, error_message)
        """
        config_path, err_msg = self.get_config_path()
        if not config_path:
            return False, f"Failed to get config path: {err_msg}"

        if not os.path.exists(config_path):
            return False, f"Configuration file does not exist: {config_path}"

        if not os.path.isfile(config_path):
            return False, f"Configuration path is not a file: {config_path}"

        try:
            # Try to open and read the file
            with open(config_path, 'r', encoding='utf-8') as f:
                f.read(1)  # Just read one character to test readability
            return True, "Configuration file is valid and readable"

        except (OSError, UnicodeDecodeError) as e:
            return False, f"Configuration file is not readable: {e}"

    def get_config_value(self, key: str, default=None):
        """
        Get a specific configuration value by key.

        Args:
            key: Configuration key to retrieve
            default: Default value if key is not found

        Returns:
            Any: Configuration value or default
        """
        config_map, err_msg = self.get_config_as_map()
        if not config_map:
            return default

        return config_map.get(key, default)

    def has_config_key(self, key: str) -> bool:
        """
        Check if a configuration key exists.

        Args:
            key: Configuration key to check

        Returns:
            bool: True if key exists, False otherwise
        """
        config_map, _ = self.get_config_as_map()
        return key in config_map

    def get_config_summary(self) -> Dict:
        """
        Get a summary of configuration status.

        Returns:
            Dict: Configuration summary with status and metadata
        """
        config_path, path_err = self.get_config_path()
        is_valid, valid_err = self.validate_config_file()
        config_map, map_err = self.get_config_as_map()

        summary = {
            'config_path': config_path,
            'path_error': path_err if path_err != "Success" else None,
            'is_valid': is_valid,
            'validation_error': valid_err if not is_valid else None,
            'has_config': bool(config_map),
            'config_keys': list(config_map.keys()) if config_map else [],
            'config_error': map_err if map_err != "Success" else None
        }

        return summary
