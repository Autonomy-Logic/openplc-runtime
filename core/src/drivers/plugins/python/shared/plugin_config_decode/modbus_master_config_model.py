from abc import ABC, abstractmethod
from typing import List, Dict, Any

try:
    from .plugin_config_contact import PluginConfigContract, PluginConfigError
except ImportError:
    from plugin_config_contact import PluginConfigContract, PluginConfigError

class ModbusIoPointConfig:
    """
    Configuration model for a single I/O point.
    """
    def __init__(self, fc: int, offset: str, iec_location: str, length: int):
        self.fc = fc
        self.offset = self._parse_offset(offset)
        self.iec_location = iec_location
        self.length = length

        self._validate()

    def _parse_offset(self, offset_str: str) -> int:
        """Parses a hex string offset like '0x0000' to an integer."""
        if not isinstance(offset_str, str) or not offset_str.startswith('0x'):
            raise PluginConfigError(f"Invalid offset format: {offset_str}. Must be a hex string like '0x0000'.")
        try:
            return int(offset_str, 16)
        except ValueError:
            raise PluginConfigError(f"Invalid hex offset value: {offset_str}")

    def _validate(self):
        """Validates the I/O point configuration."""
        if not isinstance(self.fc, int) or self.fc <= 0:
            raise PluginConfigError(f"Invalid function code (fc): {self.fc}. Must be a positive integer.")
        if not isinstance(self.offset, int) or self.offset < 0:
            raise PluginConfigError(f"Invalid offset: {self.offset}. Must be a non-negative integer.")
        if not isinstance(self.iec_location, str) or not self.iec_location:
            raise PluginConfigError(f"Invalid IEC location: {self.iec_location}. Must be a non-empty string.")
        if not isinstance(self.length, int) or self.length <= 0:
            raise PluginConfigError(f"Invalid length: {self.length}. Must be a positive integer.")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ModbusIoPointConfig':
        """Creates an IoPointConfig instance from a dictionary."""
        try:
            return cls(
                fc=data["fc"],
                offset=data["offset"],
                iec_location=data["iec_location"],
                length=data["len"]
            )
        except KeyError as e:
            raise PluginConfigError(f"Missing required field in io_points config: {e}")
        except PluginConfigError as e:
            raise PluginConfigError(f"Invalid io_point config: {e}")

    def __repr__(self) -> str:
        return (f"IoPointConfig(fc={self.fc}, offset=0x{self.offset:04X}, "
                f"iec_location='{self.iec_location}', length={self.length})") 
    
class ModbusTcpConfig(PluginConfigContract):
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
