import re
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
TransportType = Literal["tcp", "rtu"]
ParityType = Literal["N", "E", "O"]

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
    Supports both TCP and RTU transport types.
    """
    def __init__(self):
        self.name: str = "UNDEFINED"
        self.protocol: str = "MODBUS"
        self.type: str = "SLAVE"

        # Transport type - "tcp" or "rtu" (defaults to "tcp" for backward compatibility)
        self.transport: TransportType = "tcp"

        # TCP-specific fields
        self.host: str = "127.0.0.1"
        self.port: int = 502

        # RTU-specific fields
        self.serial_port: str = ""
        self.baud_rate: int = 9600
        self.parity: ParityType = "N"
        self.stop_bits: int = 1
        self.data_bits: int = 8

        # Common fields
        self.timeout_ms: int = 1000
        self.slave_id: int = 1  # Unit/Slave ID (0-255 for TCP gateways, 1-247 for RTU)
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

        # Transport type - defaults to "tcp" for backward compatibility
        device.transport = config.get("transport", "tcp")

        # TCP fields
        device.host = config.get("host", "127.0.0.1")
        device.port = config.get("port", 502)

        # RTU fields
        device.serial_port = config.get("serial_port", "")
        device.baud_rate = config.get("baud_rate", 9600)
        device.parity = config.get("parity", "N")
        device.stop_bits = config.get("stop_bits", 1)
        device.data_bits = config.get("data_bits", 8)

        # Common fields
        device.timeout_ms = config.get("timeout_ms", 1000)
        device.slave_id = config.get("slave_id", 1)

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
            raise ValueError(f"Device name is undefined.")
        if self.protocol != "MODBUS":
            raise ValueError(f"Invalid protocol: {self.protocol}. Expected 'MODBUS' for device {self.name}.")
        if not isinstance(self.timeout_ms, int) or self.timeout_ms <= 0:
            raise ValueError(f"Invalid timeout_ms: {self.timeout_ms}. Must be a positive integer for device {self.name}.")

        # Transport-specific validation
        if self.transport == "rtu":
            if not self.serial_port:
                raise ValueError(f"Serial port is required for RTU device '{self.name}'")
            if not isinstance(self.slave_id, int) or not (1 <= self.slave_id <= 247):
                raise ValueError(f"Slave ID must be 1-247 for RTU device '{self.name}', got {self.slave_id}")
            if self.parity not in ("N", "E", "O"):
                raise ValueError(f"Invalid parity '{self.parity}' for RTU device '{self.name}'. Must be 'N', 'E', or 'O'.")
            if self.stop_bits not in (1, 2):
                raise ValueError(f"Stop bits must be 1 or 2 for RTU device '{self.name}', got {self.stop_bits}")
            if self.data_bits not in (7, 8):
                raise ValueError(f"Data bits must be 7 or 8 for RTU device '{self.name}', got {self.data_bits}")
        elif self.transport == "tcp":
            if not self.host:
                raise ValueError(f"Host is required for TCP device '{self.name}'")
            if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
                raise ValueError(f"Port must be 1-65535 for TCP device '{self.name}', got {self.port}")
            if not isinstance(self.slave_id, int) or not (0 <= self.slave_id <= 255):
                raise ValueError(f"Slave ID must be 0-255 for TCP device '{self.name}', got {self.slave_id}")
        else:
            raise ValueError(f"Invalid transport type '{self.transport}' for device '{self.name}'. Must be 'tcp' or 'rtu'.")

        # Validate IO points
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
            if not isinstance(point.cycle_time_ms, int) or point.cycle_time_ms <= 0:
                raise ValueError(f"Invalid cycle_time_ms: {point.cycle_time_ms}. Must be a positive integer for device {self.name}, point {i}.")

    def __repr__(self) -> str:
        if self.transport == "tcp":
            return f"ModbusDeviceConfig(name='{self.name}', transport='tcp', host='{self.host}', port={self.port}, slave_id={self.slave_id}, io_points={len(self.io_points)})"
        else:
            return f"ModbusDeviceConfig(name='{self.name}', transport='rtu', serial_port='{self.serial_port}', baud_rate={self.baud_rate}, slave_id={self.slave_id}, io_points={len(self.io_points)})"

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
                    print(f"(PASS) Device '{device.name}' loaded: {device.host}:{device.port}")
                except Exception as e:
                    print(f"(FAIL) Error parsing device config #{i+1}: {e}")
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

        # Separate TCP and RTU devices for different validation rules
        tcp_devices = [d for d in self.devices if d.transport == "tcp"]
        rtu_devices = [d for d in self.devices if d.transport == "rtu"]

        # Multiple TCP devices may share the same host:port — that is exactly how
        # an Ethernet-to-Modbus gateway (TCP-to-RTU converter) is addressed: one IP,
        # several serial slaves distinguished by their unit/slave ID. So we key TCP
        # devices by (host, port, slave_id) and reject only TRUE duplicates (same
        # endpoint AND slave ID), which would be ambiguous. The runtime routes each
        # device's slave_id into the MBAP unit-ID byte and shares one TCP connection
        # per host:port (see group_tcp_devices_by_endpoint / ModbusBusHandler).
        tcp_endpoints = [(device.host, device.port, device.slave_id) for device in tcp_devices]
        if len(tcp_endpoints) != len(set(tcp_endpoints)):
            raise ValueError(
                "Duplicate (host, port, slave_id) found for TCP devices. Multiple "
                "devices may share a host:port (a Modbus gateway) only if their "
                "slave IDs differ."
            )

        # Check for duplicate slave IDs on the same serial bus for RTU devices
        # Group RTU devices by serial bus (serial_port + baud_rate + parity + stop_bits + data_bits)
        rtu_buses: Dict[str, List[int]] = {}
        for device in rtu_devices:
            bus_key = f"{device.serial_port}:{device.baud_rate}:{device.parity}:{device.stop_bits}:{device.data_bits}"
            if bus_key not in rtu_buses:
                rtu_buses[bus_key] = []
            rtu_buses[bus_key].append(device.slave_id)

        # Check for duplicate slave IDs within each bus
        for bus_key, slave_ids in rtu_buses.items():
            if len(slave_ids) != len(set(slave_ids)):
                raise ValueError(f"Duplicate slave IDs found on RTU bus '{bus_key}'. Each device on the same serial bus must have a unique slave ID.")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(devices={len(self.devices)})"


class ModbusIoPointConfig:
    """
    Model for a single Modbus I/O point configuration.
    """
    def __init__(self, fc: int, offset: str, iec_location: str, length: int, cycle_time_ms: int = 1000):
        self.fc = fc  # Function code
        self.offset = offset  # Modbus register offset
        self.iec_location: IECAddress = parse_iec_address(iec_location)  # IEC location (as IECAddress)
        self.length = length  # Length of the data
        self.cycle_time_ms = cycle_time_ms  # Polling cycle time in milliseconds

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
            cycle_time_ms = data.get("cycle_time_ms", 1000)
        except KeyError as e:
            raise ValueError(f"Missing required field in Modbus I/O point config: {e}")

        return cls(fc=fc, offset=offset, iec_location=iec_location, length=length, cycle_time_ms=cycle_time_ms)

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
            "len": self.length,
            "cycle_time_ms": self.cycle_time_ms
        }

    def __repr__(self) -> str:
        return (f"ModbusIoPointConfig(fc={self.fc}, offset='{self.offset}', "
                f"iec_location='{self.iec_location}', length={self.length})")
