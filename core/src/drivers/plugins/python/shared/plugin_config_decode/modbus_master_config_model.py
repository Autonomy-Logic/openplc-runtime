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
        self.io_points: List['ModbusIoPointConfig'] = []  # List to hold Modbus I/O points

    def from_json_file(self, file_path: str):
        """Read config from a JSON file."""
        with open(file_path, 'r') as f:
            raw_config = json.load(f)
            print("Raw config loaded:", raw_config)
            # Here you would parse raw_config into the appropriate attributes
            count = 0
            for config in raw_config:
                print("Parsing config #", count)
                count += 1
                self.name = config.get("name", "UNDEFINED")
                self.protocol = config.get("protocol", "UNDEFINED")
                self.config = config.get("config", {})

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
        # Implement validation logic here
        if self.name == "UNDEFINED":
            raise ValueError("Plugin name is undefined.")
        if self.protocol != "MODBUS":
            raise ValueError(f"Invalid protocol: {self.protocol}. Expected 'MODBUS'.")
        if not isinstance(self.port, int) or self.port <= 0:
            raise ValueError(f"Invalid port: {self.port}. Must be a positive integer.")
        if not isinstance(self.cycle_time_ms, int) or self.cycle_time_ms <= 0:
            raise ValueError(f"Invalid cycle_time_ms: {self.cycle_time_ms}. Must be a positive integer.")
        if not isinstance(self.timeout_ms, int) or self.timeout_ms <= 0:
            raise ValueError(f"Invalid timeout_ms: {self.timeout_ms}. Must be a positive integer.")
        for point in self.io_points:

            if not isinstance(point, ModbusIoPointConfig):
                raise ValueError(f"Invalid I/O point: {point}. Must be an instance of ModbusIoPointConfig.")
            if not isinstance(point.fc, int) or point.fc <= 0:
                raise ValueError(f"Invalid function code (fc): {point.fc}. Must be a positive integer.")
            if not isinstance(point.offset, str) or not point.offset:
                raise ValueError(f"Invalid offset: {point.offset}. Must be a non-empty string.")
            if not isinstance(point.iec_location, str) or not point.iec_location:
                raise ValueError(f"Invalid IEC location: {point.iec_location}. Must be a non-empty string.")
            if not isinstance(point.length, int) or point.length <= 0:
                raise ValueError(f"Invalid length: {point.length}. Must be a positive integer.")

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

    def to_dict(self) -> Dict[str, Any]:
        """
        Converts the ModbusIoPointConfig instance to a dictionary.
        """
        return {
            "fc": self.fc,
            "offset": self.offset,
            "iec_location": self.iec_location,
            "len": self.length
        }

    def __repr__(self) -> str:
        return (f"ModbusIoPointConfig(fc={self.fc}, offset='{self.offset}', "
                f"iec_location='{self.iec_location}', length={self.length})")