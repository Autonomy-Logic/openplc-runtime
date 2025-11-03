import re
from typing import List, Dict, Any
import json
from dataclasses import dataclass
from typing import Optional, Literal, List, Dict, Any

try:
    from .plugin_config_contact import PluginConfigContract
except ImportError:
    # For direct execution
    from plugin_config_contact import PluginConfigContract

Area = Literal["I", "Q", "M"]
Size = Literal["X", "B", "W", "D", "L"]

ADDR_RE = re.compile(r"^%([IQM])([XBWDL])(\d+)(?:\.(\d+))?$", re.IGNORECASE)

@dataclass
class IECAddress:
    area: Area              # 'I' | 'Q' | 'M'
    size: Size              # 'X' | 'B' | 'W' | 'D' | 'L'
    byte: int               # base byte (for X it's the bit's byte; for B/W/D/L it's the start)
    bit: Optional[int]      # only for X
    index_bits: Optional[int]   # linear index in bits (only for X)
    index_bytes: int            # linear index in bytes (buffer offset)
    width_bits: int             # 1, 8, 16, 32, 64

def parse_iec_address(s: str) -> IECAddress:
    m = ADDR_RE.match(s.strip())
    if not m:
        raise ValueError(f"Invalid IEC address: {s!r}")
    _area, _size, n1, n2 = m.groups()
    area: Area = _area.upper()  # type: ignore 
    size: Size = _size.upper() # type: ignore
    byte = int(n1)
    bit = int(n2) if n2 is not None else None

    if size == "X":
        if bit is None or not (0 <= bit <= 7):
            raise ValueError("Missing bit or out of 0..7 range for X-type (bit) address.")
        index_bits = byte * 8 + bit
        index_bytes = byte
        width_bits = 1
    elif size == "B":
        index_bits = None
        index_bytes = byte
        width_bits = 8
    elif size == "W":
        index_bits = None
        index_bytes = byte * 2
        width_bits = 16
    elif size == "D":
        index_bits = None
        index_bytes = byte * 4
        width_bits = 32
    elif size == "L":
        index_bits = None
        index_bytes = byte * 8
        width_bits = 64
    else:
        raise ValueError(f"Unsupported size: {size}")

    return IECAddress(area, size, byte, bit, index_bits, index_bytes, width_bits)

class ModbusDeviceConfig:
    """
    Model for a single Modbus device configuration.
    """
    def __init__(self):
        self.name: str = "UNDEFINED"
        self.protocol: str = "MODBUS"
        self.type: str = "SLAVE"
        self.host: str = "127.0.0.1"
        self.port: int = 502
        self.cycle_time_ms: int = 1000
        self.timeout_ms: int = 1000
        self.io_points: List['ModbusIoPointConfig'] = []

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ModbusDeviceConfig':
        """
        Creates a ModbusDeviceConfig instance from a dictionary.
        """
        device = cls()
        device.name = data.get("name", "UNDEFINED")
        device.protocol = data.get("protocol", "MODBUS")
        
        config = data.get("config", {})
        device.type = config.get("type", "SLAVE")
        device.host = config.get("host", "127.0.0.1")
        device.port = config.get("port", 502)
        device.cycle_time_ms = config.get("cycle_time_ms", 1000)
        device.timeout_ms = config.get("timeout_ms", 1000)
        
        # Parse I/O points
        io_points_data = config.get("io_points", [])
        device.io_points = []
        
        for point in io_points_data:
            modbus_point = ModbusIoPointConfig.from_dict(data=point)
            device.io_points.append(modbus_point)
            
        return device

    def validate(self) -> None:
        """Validates the device configuration."""
        if self.name == "UNDEFINED":
            raise ValueError(f"Device name is undefined for device {self.host}:{self.port}.")
        if self.protocol != "MODBUS":
            raise ValueError(f"Invalid protocol: {self.protocol}. Expected 'MODBUS' for device {self.name}.")
        if not isinstance(self.port, int) or self.port <= 0:
            raise ValueError(f"Invalid port: {self.port}. Must be a positive integer for device {self.name}.")
        if not isinstance(self.cycle_time_ms, int) or self.cycle_time_ms <= 0:
            raise ValueError(f"Invalid cycle_time_ms: {self.cycle_time_ms}. Must be a positive integer for device {self.name}.")
        if not isinstance(self.timeout_ms, int) or self.timeout_ms <= 0:
            raise ValueError(f"Invalid timeout_ms: {self.timeout_ms}. Must be a positive integer for device {self.name}.")
        
        for i, point in enumerate(self.io_points):
            if not isinstance(point, ModbusIoPointConfig):
                raise ValueError(f"Invalid I/O point {i}: {point}. Must be an instance of ModbusIoPointConfig for device {self.name}.")
            if not isinstance(point.fc, int) or point.fc <= 0:
                raise ValueError(f"Invalid function code (fc): {point.fc}. Must be a positive integer for device {self.name}, point {i}.")
            if not isinstance(point.offset, str) or not point.offset:
                raise ValueError(f"Invalid offset: {point.offset}. Must be a non-empty string for device {self.name}, point {i}.")
            if not isinstance(point.iec_location, IECAddress):
                raise ValueError(f"Invalid IEC location: {point.iec_location}. Must be an IECAddress object for device {self.name}, point {i}.")
            if not isinstance(point.length, int) or point.length <= 0:
                raise ValueError(f"Invalid length: {point.length}. Must be a positive integer for device {self.name}, point {i}.")

    def __repr__(self) -> str:
        return f"ModbusDeviceConfig(name='{self.name}', host='{self.host}', port={self.port}, io_points={len(self.io_points)})"

class ModbusMasterConfig(PluginConfigContract):
    """
    Modbus Master configuration model.
    """
    def __init__(self):
        super().__init__() # Call the base class constructor
        # self.config = {} # attributes specific to ModbusMasterConfig can be added here
        self.devices: List[ModbusDeviceConfig] = []  # List to hold multiple Modbus devices

    def import_config_from_file(self, file_path: str):
        """Read config from a JSON file."""
        with open(file_path, 'r') as f:
            raw_config = json.load(f)
            print("Raw config loaded:", raw_config)
            
            # Clear any existing devices
            self.devices = []
            
            # Parse each device configuration
            for i, device_config in enumerate(raw_config):
                print(f"Parsing device config #{i+1}")
                try:
                    device = ModbusDeviceConfig.from_dict(device_config)
                    self.devices.append(device)
                    print(f"✓ Device '{device.name}' loaded: {device.host}:{device.port}")
                except Exception as e:
                    print(f"✗ Error parsing device config #{i+1}: {e}")
                    raise ValueError(f"Failed to parse device configuration #{i+1}: {e}")
            
            print(f"Total devices loaded: {len(self.devices)}")

    def validate(self) -> None:
        """Validates the configuration."""
        if not self.devices:
            raise ValueError("No devices configured. At least one Modbus device must be defined.")
        
        # Validate each device
        for i, device in enumerate(self.devices):
            try:
                device.validate()
            except Exception as e:
                raise ValueError(f"Device #{i+1} validation failed: {e}")
        
        # Check for duplicate device names
        device_names = [device.name for device in self.devices]
        if len(device_names) != len(set(device_names)):
            raise ValueError("Duplicate device names found. Each device must have a unique name.")
        
        # Check for duplicate host:port combinations
        host_port_combinations = [(device.host, device.port) for device in self.devices]
        if len(host_port_combinations) != len(set(host_port_combinations)):
            raise ValueError("Duplicate host:port combinations found. Each device must have a unique host:port combination.")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(devices={len(self.devices)})"


class ModbusIoPointConfig:
    """
    Model for a single Modbus I/O point configuration.
    """
    def __init__(self, fc: int, offset: str, iec_location: str, length: int):
        self.fc = fc  # Function code
        self.offset = offset  # Modbus register offset
        self.iec_location: IECAddress = parse_iec_address(iec_location)  # IEC location (as IECAddress)
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
        # Convert IECAddress back to string format for serialization
        iec_str = f"%{self.iec_location.area}{self.iec_location.size}{self.iec_location.byte}"
        if self.iec_location.bit is not None:
            iec_str += f".{self.iec_location.bit}"
            
        return {
            "fc": self.fc,
            "offset": self.offset,
            "iec_location": iec_str,
            "len": self.length
        }

    def __repr__(self) -> str:
        return (f"ModbusIoPointConfig(fc={self.fc}, offset='{self.offset}', "
                f"iec_location='{self.iec_location}', length={self.length})")