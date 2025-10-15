from typing import List, Dict, Any
import json

try:
    from .plugin_config_contact import PluginConfigContract
except ImportError:
    # Para execução direta
    from plugin_config_contact import PluginConfigContract

class ModbusMasterConfig(PluginConfigContract):
    """
    Modbus Master configuration model.
    """
    def __init__(self, config_path: str):
        super().__init__(config_path) # Call the base class constructor
        self.config = {} # attributes specific to ModbusMasterConfig can be added here
        self.io_points: List[ModbusIoPointConfig] = []  # List to hold Modbus I/O points

    def from_json_file(self, file_path: str):
        """Read config from a JSON file."""
        with open(file_path, 'r') as f:
            raw_config = json.load(f)
            # Here you would parse raw_config into the appropriate attributes

            self.name = raw_config.get("name", "UNDEFINED")
            self.protocol = raw_config.get("protocol", "UNDEFINED")

            self.config = raw_config.get("config", {})

            self.type = self.config.get("type", "UNDEFINED")
            self.host = self.config.get("host", "UNDEFINED")
            self.port = self.config.get("port", 0)
            self.cycle_time_ms = self.config.get("cycle_time_ms", 0)
            self.timeout_ms = self.config.get("timeout_ms", 0)

            # Parse I/O points
            io_points_data = self.config.get("io_points", [])
            self.io_points = []
            for point in io_points_data:
                # Parse each I/O point from dictionary
                modbus_point = ModbusIoPointConfig.from_dict(data=point)
                self.io_points.append(modbus_point)

    def validate(self) -> None:
        """Validates the configuration."""
        pass

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(PATH={self.config_path})"
    

class ModbusIoPointConfig:
    """
    Model for a single Modbus I/O point configuration.
    """
    def __init__(self, fc: int, offset: str, iec_location: str, length: int):
        self.fc = fc  # Function code
        self.offset = offset  # Modbus register offset
        self.iec_location = iec_location  # IEC location string
        self.length = length  # Length of the data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ModbusIoPointConfig':
        """
        Creates a ModbusIoPointConfig instance from a dictionary.
        """
        try:
            fc = data["fc"]
            offset = data["offset"]
            iec_location = data["iec_location"]
            length = data["len"]
        except KeyError as e:
            raise ValueError(f"Missing required field in Modbus I/O point config: {e}")

        return cls(fc=fc, offset=offset, iec_location=iec_location, length=length)

    def __repr__(self) -> str:
        return (f"ModbusIoPointConfig(fc={self.fc}, offset='{self.offset}', "
                f"iec_location='{self.iec_location}', length={self.length})")