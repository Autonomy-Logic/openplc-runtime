#!/usr/bin/env python3
"""
Base protocol configuration abstract class for OpenPLC Python plugins.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .modbus_master_config_model import ModbusIoPointConfig

class PluginConfigError(Exception):
    """Custom exception for plugin configuration errors."""
    pass


class PluginConfigContract(ABC):
    """
    Abstract base class for protocol-specific configurations.
    """
    def __init__(self, config_data: Dict[str, Any]):
        self.raw_config = config_data
        self._parse_specific_config()

    @abstractmethod
    def _parse_specific_config(self):
        """
        Parse and validate protocol-specific fields from the 'config' object.
        This method should populate instance attributes with parsed and validated data.
        Raises PluginConfigError on validation failure.
        """
        pass

    @abstractmethod
    def get_protocol_name(self) -> str:
        """Returns the protocol name this config is for."""
        pass

    def get_common_io_points(self) -> List[Any]:
        """
        Parses and returns the list of I/O points, which is common across protocols.
        """
        # Import dynamically to avoid circular dependencies
        try:
            from .modbus_master_config_model import ModbusIoPointConfig
        except ImportError:
            from modbus_master_config_model import ModbusIoPointConfig
            
        io_points_data = self.raw_config.get("io_points", [])
        if not isinstance(io_points_data, list):
            raise PluginConfigError("'io_points' must be a list.")
        
        parsed_io_points = []
        for point_data in io_points_data:
            try:
                parsed_io_points.append(ModbusIoPointConfig.from_dict(point_data))
            except PluginConfigError as e:
                raise PluginConfigError(f"Error parsing io_point: {e}")
        return parsed_io_points

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(raw_config={self.raw_config})"