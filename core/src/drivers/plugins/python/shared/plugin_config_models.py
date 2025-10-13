#!/usr/bin/env python3
"""
Configuration models for OpenPLC Python plugins.
This module provides classes to parse and validate plugin-specific JSON configurations.
"""

import json
from typing import List, Dict, Any, Optional
from base_protocol_config import BaseProtocolConfig, PluginConfigError, ModbusIoPointConfig

class ModbusTcpConfig(BaseProtocolConfig):
    """
    Configuration model for Modbus TCP protocol (acting as Master).
    """
    def __init__(self, config_data: Dict[str, Any]):
        self.type: str = ""
        self.host: str = ""
        self.port: int = 502  # Default Modbus port
        self.cycle_time_ms: int = 100
        self.timeout_ms: int = 1000
        self.io_points: List[ModbusIoPointConfig] = []
        super().__init__(config_data)

    def _parse_specific_config(self):
        """Parses Modbus TCP specific fields."""
        self.type = self.raw_config.get("type", "").upper()
        if self.type != "SLAVE":
            # In this context, "SLAVE" refers to the remote device type the master is talking to.
            raise PluginConfigError(f"Invalid type for Modbus master config: {self.type}. Expected 'SLAVE'.")

        host_val = self.raw_config.get("host")
        if not isinstance(host_val, str) or not host_val:
            raise PluginConfigError(f"Invalid or missing host: {host_val}. Must be a non-empty string.")
        self.host = host_val

        self.port = self.raw_config.get("port", 502)
        if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise PluginConfigError(f"Invalid port: {self.port}. Must be an integer between 1 and 65535.")

        self.cycle_time_ms = self.raw_config.get("cycle_time_ms", 100)
        if not isinstance(self.cycle_time_ms, int) or self.cycle_time_ms <= 0:
            raise PluginConfigError(f"Invalid cycle_time_ms: {self.cycle_time_ms}. Must be a positive integer.")

        self.timeout_ms = self.raw_config.get("timeout_ms", 1000)
        if not isinstance(self.timeout_ms, int) or self.timeout_ms <= 0:
            raise PluginConfigError(f"Invalid timeout_ms: {self.timeout_ms}. Must be a positive integer.")
        
        try:
            self.io_points = self.get_common_io_points()
        except PluginConfigError as e:
            raise PluginConfigError(f"Error parsing io_points for Modbus TCP: {e}")

    def get_protocol_name(self) -> str:
        return "MODBUS"

    def __repr__(self) -> str:
        return (f"ModbusTcpConfig(type='{self.type}', host='{self.host}', port={self.port}, "
                f"cycle_time_ms={self.cycle_time_ms}, timeout_ms={self.timeout_ms}, "
                f"io_points={self.io_points})")

class PluginInstanceConfig:
    """
    Top-level configuration model for a single plugin instance.
    Handles polymorphic parsing of the 'config' object based on the 'protocol' field.
    """
    def __init__(self, name: str, protocol: str, protocol_config: BaseProtocolConfig):
        self.name = name
        self.protocol = protocol
        self.protocol_config: BaseProtocolConfig = protocol_config

        self._validate()

    def _validate(self):
        """Validates the top-level plugin instance configuration."""
        if not isinstance(self.name, str) or not self.name:
            raise PluginConfigError(f"Invalid name: {self.name}. Must be a non-empty string.")
        if not isinstance(self.protocol, str) or not self.protocol:
            raise PluginConfigError(f"Invalid protocol: {self.protocol}. Must be a non-empty string.")
        if not isinstance(self.protocol_config, BaseProtocolConfig):
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

        protocol_config: Optional[BaseProtocolConfig] = None
        if protocol == "MODBUS":
            protocol_config = ModbusTcpConfig(config_data)
        else:
            raise PluginConfigError(f"Unsupported protocol: {protocol}. "
                                    f"Currently only 'MODBUS' is supported.")

        return cls(name=name, protocol=protocol, protocol_config=protocol_config)

    def __repr__(self) -> str:
        return f"PluginInstanceConfig(name='{self.name}', protocol='{self.protocol}', protocol_config={self.protocol_config})"

class PluginConfigParser:
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
            return PluginConfigParser.parse_from_json_string(json_string)
        except FileNotFoundError:
            raise PluginConfigError(f"Configuration file not found: {file_path}")
        except IOError as e:
            raise PluginConfigError(f"Error reading configuration file {file_path}: {e}")

if __name__ == "__main__":
    # This module is designed to be imported, not run directly.
    # For testing, run test_plugin_config_models.py instead.
    print("This is a library module. For testing, run 'python test_plugin_config_models.py'")
    exit(1)
