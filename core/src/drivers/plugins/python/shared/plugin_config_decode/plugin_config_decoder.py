#!/usr/bin/env python3
"""
Configuration models for OpenPLC Python plugins.
This module provides classes to parse and validate plugin-specific JSON configurations.
"""

import json
from typing import List, Dict, Any, Optional

try:
    from .plugin_config_contact import PluginConfigContract, PluginConfigError
    from .modbus_master_config_model import ModbusIoPointConfig, ModbusTcpConfig
except ImportError:
    # For direct execution
    from plugin_config_contact import PluginConfigContract, PluginConfigError
    from modbus_master_config_model import ModbusIoPointConfig, ModbusTcpConfig

class PluginInstanceConfig:
    """
    Top-level configuration model for a single plugin instance.
    Handles polymorphic parsing of the 'config' object based on the 'protocol' field.
    """
    def __init__(self, name: str, protocol: str, protocol_config: PluginConfigContract):
        self.name = name
        self.protocol = protocol
        self.protocol_config: PluginConfigContract = protocol_config

        self._validate()

    def _validate(self):
        """Validates the top-level plugin instance configuration."""
        if not isinstance(self.name, str) or not self.name:
            raise PluginConfigError(f"Invalid name: {self.name}. Must be a non-empty string.")
        if not isinstance(self.protocol, str) or not self.protocol:
            raise PluginConfigError(f"Invalid protocol: {self.protocol}. Must be a non-empty string.")
        if not isinstance(self.protocol_config, PluginConfigContract):
            raise PluginConfigError(f"Invalid protocol_config type: {type(self.protocol_config)}.")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PluginInstanceConfig':
        """
        Creates a PluginInstanceConfig instance from a dictionary.
        This method handles the polymorphic creation of the protocol-specific config.
        """
        try:
            name = data["name"]
            protocol = data["protocol"].upper()  # Normalize protocol name
            config_data = data["config"]
        except KeyError as e:
            raise PluginConfigError(f"Missing required field in plugin instance config: {e}")

        if not isinstance(config_data, dict):
            raise PluginConfigError("'config' field must be a dictionary.")

        protocol_config: Optional[PluginConfigContract] = None
        if protocol == "MODBUS":
            protocol_config = ModbusTcpConfig(config_data)
        elif protocol == "ETHERCAT":
            # Placeholder for future EtherCAT support
            # protocol_config = EthercatConfig(config_data)
            raise PluginConfigError("EtherCAT protocol support is not yet implemented.")    
        else:
            raise PluginConfigError(f"Unsupported protocol: {protocol}. "
                                    f"Currently only 'MODBUS' is supported.")

        return cls(name=name, protocol=protocol, protocol_config=protocol_config)

    def __repr__(self) -> str:
        return f"PluginInstanceConfig(name='{self.name}', protocol='{self.protocol}', protocol_config={self.protocol_config})"

class PluginConfigDecoder:
    """
    Utility class to parse a list of plugin instance configurations from a JSON file or string.
    """
    @staticmethod
    def parse_from_json_string(json_string: str) -> List[PluginInstanceConfig]:
        """
        Parses a JSON string into a list of PluginInstanceConfig objects.
        """
        try:
            data = json.loads(json_string)
        except json.JSONDecodeError as e:
            raise PluginConfigError(f"Invalid JSON format: {e}")

        if not isinstance(data, list):
            raise PluginConfigError("Top-level JSON structure must be a list of plugin configurations.")

        parsed_configs = []
        for instance_data in data:
            try:
                parsed_configs.append(PluginInstanceConfig.from_dict(instance_data))
            except PluginConfigError as e:
                # Optionally, log and continue, or re-raise to fail fast
                # For now, let's re-raise to make it explicit that a config is bad.
                raise PluginConfigError(f"Error parsing plugin instance '{instance_data.get('name', 'UNKNOWN')}': {e}")
        return parsed_configs

    @staticmethod
    def parse_from_json_file(file_path: str) -> List[PluginInstanceConfig]:
        """
        Parses a JSON file into a list of PluginInstanceConfig objects.
        """
        try:
            with open(file_path, 'r') as f:
                json_string = f.read()
            return PluginConfigDecoder.parse_from_json_string(json_string)
        except FileNotFoundError:
            raise PluginConfigError(f"Configuration file not found: {file_path}")
        except IOError as e:
            raise PluginConfigError(f"Error reading configuration file {file_path}: {e}")

if __name__ == "__main__":
    # This module is designed to be imported, not run directly.
    # For testing, run test_plugin_config_models.py instead.
    print("This is a library module. For testing, run 'python test_plugin_config_models.py'")
    exit(1)
