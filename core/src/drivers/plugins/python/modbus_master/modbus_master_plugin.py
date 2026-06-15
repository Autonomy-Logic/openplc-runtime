import os
import sys
import threading
import time
import traceback
from typing import Any, List

from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.pdu import ExceptionResponse

# Add the parent directory to Python path to find shared module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import the correct type definitions
# pylint: disable=wrong-import-position
from shared import (
    PluginLogger,
    SafeBufferAccess,
    safe_extract_runtime_args_from_capsule,
)

# Import the configuration model
from shared.plugin_config_decode.modbus_master_config_model import (
    ModbusMasterConfig,
)

# pylint: enable=wrong-import-position

# Import local modules
try:
    # Try relative imports first (when used as package)
    from .modbus_master_connection import ModbusConnectionManager
    from .modbus_master_memory import (  # Optimized functions for minimal mutex hold time
        convert_modbus_data_to_iec_values,
        convert_raw_iec_to_modbus,
        read_raw_iec_values,
        write_preconverted_iec_values,
    )
    from .modbus_master_utils import (
        calculate_gcd_of_cycle_times,
        get_modbus_registers_count_for_iec_size,
        parse_modbus_offset,
    )
except ImportError:
    # Fallback to absolute imports (when run standalone)
    from modbus_master_connection import ModbusConnectionManager
    from modbus_master_memory import (  # Optimized functions for minimal mutex hold time
        convert_modbus_data_to_iec_values,
        convert_raw_iec_to_modbus,
        read_raw_iec_values,
        write_preconverted_iec_values,
    )
    from modbus_master_utils import (
        calculate_gcd_of_cycle_times,
        get_modbus_registers_count_for_iec_size,
        parse_modbus_offset,
    )

# Global variables for plugin lifecycle and configuration
# pylint: disable=invalid-name
runtime_args = None
modbus_master_config: ModbusMasterConfig = None
safe_buffer_accessor: SafeBufferAccess = None
logger: PluginLogger = None
slave_threads: List[threading.Thread] = []
# pylint: enable=invalid-name


class ModbusSlaveDevice(threading.Thread):
    """
    Handles a single Modbus TCP device with its own connection.
    For RTU devices, use ModbusRtuBusHandler instead.
    """
    def __init__(self, device_config: Any, sba: SafeBufferAccess, plugin_logger: PluginLogger):
        super().__init__(daemon=True)
        self.device_config = device_config
        self.sba = sba
        self.logger = plugin_logger
        self._stop_event = threading.Event()

        # Create connection manager for TCP device
        self.connection_manager = ModbusConnectionManager(
            transport="tcp",
            host=device_config.host,
            port=device_config.port,
            timeout_ms=device_config.timeout_ms,
            slave_id=device_config.slave_id
        )
        self.name = f"ModbusSlave-{device_config.name}-TCP-{device_config.host}:{device_config.port}"

        # Calculate GCD of all I/O point cycle times for this device
        self.gcd_cycle_time_ms = calculate_gcd_of_cycle_times(device_config.io_points)
        self.logger.info(f"[{self.name}] Calculated GCD cycle time: {self.gcd_cycle_time_ms}ms")

    def _ensure_connection(self) -> bool:
        """
        Ensures there is a valid connection, reconnecting if necessary.

        Returns:
            True if connection is available, False if thread was interrupted
        """
        return self.connection_manager.ensure_connection(self._stop_event)

    def run(self):  # pylint: disable=too-many-locals
        self.logger.info(f"[{self.name}] Thread started.")

        io_points = self.device_config.io_points
        gcd_cycle_time_seconds = self.gcd_cycle_time_ms / 1000.0

        if not io_points:
            self.logger.warn(f"[{self.name}] No I/O points defined. Stopping thread.")
            return

        # Connect with infinite retry
        if not self.connection_manager.connect_with_retry(self._stop_event):
            self.logger.info(f"[{self.name}] Thread stopped before connection could be established.")
            return

        # Initialize cycle counter
        cycle_counter = 0

        try:
            while not self._stop_event.is_set():
                cycle_start_time = time.monotonic()

                # Ensure connection exists before cycle
                if not self._ensure_connection():
                    break  # Thread was interrupted

                # 1. READ OPERATIONS - Process only I/O points that are due for polling this cycle
                read_results_to_update = []  # Store tuples: (iec_addr, modbus_data, length)

                # Check each read point individually
                for point in io_points:
                    if self._stop_event.is_set():
                        break

                    # Skip if this point doesn't need to be polled this cycle
                    if point.fc not in [1, 2, 3, 4]:  # Read functions
                        continue

                    # Check if point should be polled this cycle
                    point_cycle_multiple = point.cycle_time_ms // self.gcd_cycle_time_ms
                    if (cycle_counter % point_cycle_multiple) != 0:
                        continue

                    try:
                        # Parse Modbus offset
                        address = parse_modbus_offset(point.offset)

                        # Calculate the correct number of Modbus registers/coils needed
                        if point.fc in [3, 4]:  # Register-based operations (FC 3,4)
                            iec_size = point.iec_location.size
                            registers_per_iec_element = get_modbus_registers_count_for_iec_size(
                                iec_size
                            )
                            count = point.length * registers_per_iec_element
                        else:  # Coil/Discrete Input operations (FC 1,2)
                            count = point.length  # 1:1 mapping for boolean operations

                        # Perform Modbus read based on function code
                        # Note: pymodbus 3.10+ uses device_id parameter (formerly slave)
                        # device_id is used for Modbus TCP gateways that forward to RS485 devices
                        if point.fc == 1:  # Read Coils
                            response = self.connection_manager.client.read_coils(
                                address, count=count, device_id=self.connection_manager.slave_id
                            )
                        elif point.fc == 2:  # Read Discrete Inputs
                            response = self.connection_manager.client.read_discrete_inputs(
                                address, count=count, device_id=self.connection_manager.slave_id
                            )
                        elif point.fc == 3:  # Read Holding Registers
                            response = self.connection_manager.client.read_holding_registers(
                                address, count=count, device_id=self.connection_manager.slave_id
                            )
                        elif point.fc == 4:  # Read Input Registers
                            response = self.connection_manager.client.read_input_registers(
                                address, count=count, device_id=self.connection_manager.slave_id
                            )
                        else:
                            self.logger.warn(f"[{self.name}] Unsupported read FC: {point.fc}")
                            continue

                        # Check if response is valid
                        if isinstance(response, (ModbusIOException, ExceptionResponse)):
                            self.logger.error(
                                f"[{self.name}] Modbus read error "
                                f"(FC {point.fc}, addr {address}): {response}"
                            )
                            # Mark as disconnected to force reconnection on next cycle
                            self.connection_manager.mark_disconnected()
                            continue
                        if response.isError():
                            self.logger.error(
                                f"[{self.name}] Modbus read failed "
                                f"(FC {point.fc}, addr {address}): {response}"
                            )
                            # Mark as disconnected to force reconnection on next cycle
                            self.connection_manager.mark_disconnected()
                            continue

                        # Extract data from response
                        if point.fc in [1, 2]:  # Coils/Discrete Inputs (boolean data)
                            modbus_data = response.bits
                        else:  # Holding/Input Registers (integer data)
                            modbus_data = response.registers

                        # Store for batch update
                        read_results_to_update.append(
                            (point.iec_location, modbus_data, point.length)
                        )

                    except ValueError as ve:
                        self.logger.error(
                            f"[{self.name}] Invalid offset "
                            f"'{point.offset}' for FC {point.fc}: {ve}"
                        )
                    except ConnectionException as ce:
                        self.logger.error(
                            f"[{self.name}] Connection error reading "
                            f"FC {point.fc}, offset {point.offset}: {ce}"
                        )
                        # Mark as disconnected to force reconnection
                        self.connection_manager.mark_disconnected()
                    except Exception as e:
                        self.logger.error(
                            f"[{self.name}] Error reading "
                            f"FC {point.fc}, offset {point.offset}: {e}"
                        )
                        # For other errors also mark disconnected as precaution
                        self.connection_manager.mark_disconnected()

                # Batch update IEC buffers with single mutex acquisition
                # OPTIMIZATION: Pre-convert all Modbus data to IEC values BEFORE
                # acquiring the mutex to minimize mutex hold time
                if read_results_to_update:
                    # Phase 1: Pre-convert all data outside the mutex
                    preconverted_updates = []
                    for iec_addr, modbus_data, length in read_results_to_update:
                        converted_values, details = convert_modbus_data_to_iec_values(
                            iec_addr, modbus_data, length
                        )
                        if converted_values is not None and details is not None:
                            preconverted_updates.append((converted_values, details))
                        else:
                            self.logger.error(
                                f"[{self.name}] Data conversion failed "
                                f"for IEC address {iec_addr}, length={length}"
                            )

                    # Phase 2: Write pre-converted values under the mutex
                    if preconverted_updates:
                        lock_acquired, lock_msg = self.sba.acquire_mutex()
                        if lock_acquired:
                            try:
                                for converted_values, details in preconverted_updates:
                                    write_preconverted_iec_values(
                                        self.sba, converted_values, details
                                    )
                            finally:
                                self.sba.release_mutex()
                        else:
                            self.logger.error(
                                f"[{self.name}] Failed to acquire mutex "
                                f"for read updates: {lock_msg}"
                            )

                # 2. WRITE OPERATIONS - Process only I/O points that are due for polling this cycle
                # OPTIMIZATION: Batch all write preparations under a single mutex acquisition
                # to minimize mutex hold time. Read all raw IEC values at once, then convert
                # and write to Modbus outside the mutex.

                # Phase 1: Collect all write points that are due this cycle
                write_points_due = []
                for point in io_points:
                    if self._stop_event.is_set():
                        break

                    if point.fc not in [5, 6, 15, 16]:  # Write functions
                        continue

                    point_cycle_multiple = point.cycle_time_ms // self.gcd_cycle_time_ms
                    if (cycle_counter % point_cycle_multiple) != 0:
                        continue

                    try:
                        address = parse_modbus_offset(point.offset)
                        write_points_due.append((point, address))
                    except ValueError as ve:
                        self.logger.error(
                            f"[{self.name}] Invalid offset "
                            f"'{point.offset}' for FC {point.fc}: {ve}"
                        )

                # Phase 2: Read all raw IEC values under a single mutex acquisition
                raw_iec_data = []  # List of (point, address, raw_values, details, iec_size)
                if write_points_due:
                    lock_acquired, lock_msg = self.sba.acquire_mutex()
                    if lock_acquired:
                        try:
                            for point, address in write_points_due:
                                raw_values, details, iec_size = read_raw_iec_values(
                                    self.sba, point.iec_location, point.length
                                )
                                if raw_values is not None:
                                    raw_iec_data.append(
                                        (point, address, raw_values, details, iec_size)
                                    )
                                else:
                                    self.logger.error(
                                        f"[{self.name}] Failed to read raw IEC data "
                                        f"for write (FC {point.fc}, offset {point.offset})"
                                    )
                        finally:
                            self.sba.release_mutex()
                    else:
                        self.logger.error(
                            f"[{self.name}] Failed to acquire mutex "
                            f"for batch write prep: {lock_msg}"
                        )

                # Phase 3: Convert raw IEC values to Modbus format (outside mutex)
                # and perform Modbus writes
                for point, address, raw_values, details, iec_size in raw_iec_data:
                    if self._stop_event.is_set():
                        break

                    try:
                        # Convert raw IEC values to Modbus format (outside mutex)
                        values_to_write = convert_raw_iec_to_modbus(raw_values, details, iec_size)

                        if values_to_write is None:
                            self.logger.error(
                                f"[{self.name}] Failed to convert data "
                                f"for Modbus write (FC {point.fc}, "
                                f"offset {point.offset})"
                            )
                            continue

                        # Perform Modbus write operation
                        # Note: pymodbus 3.10+ uses device_id parameter (formerly slave)
                        # device_id is used for Modbus TCP gateways that forward to RS485 devices
                        if point.fc == 5:  # Write Single Coil
                            if len(values_to_write) > 0:
                                response = self.connection_manager.client.write_coil(
                                    address, values_to_write[0], device_id=self.connection_manager.slave_id
                                )
                            else:
                                self.logger.error(
                                    f"[{self.name}] No data to write "
                                    f"for FC 5, offset {address}"
                                )
                                continue
                        elif point.fc == 6:  # Write Single Register
                            if len(values_to_write) > 0:
                                response = self.connection_manager.client.write_register(
                                    address, values_to_write[0], device_id=self.connection_manager.slave_id
                                )
                            else:
                                self.logger.error(
                                    f"[{self.name}] No data to write "
                                    f"for FC 6, offset {address}"
                                )
                                continue
                        elif point.fc == 15:  # Write Multiple Coils
                            response = self.connection_manager.client.write_coils(
                                address, values_to_write, device_id=self.connection_manager.slave_id
                            )
                        elif point.fc == 16:  # Write Multiple Registers
                            response = self.connection_manager.client.write_registers(
                                address, values_to_write, device_id=self.connection_manager.slave_id
                            )
                        else:
                            self.logger.warn(f"[{self.name}] Unsupported write FC: {point.fc}")
                            continue

                        # Check write response
                        if isinstance(response, (ModbusIOException, ExceptionResponse)):
                            self.logger.error(
                                f"[{self.name}] Modbus write error "
                                f"(FC {point.fc}, addr {address}): {response}"
                            )
                            self.connection_manager.mark_disconnected()
                        elif response.isError():
                            self.logger.error(
                                f"[{self.name}] Modbus write failed "
                                f"(FC {point.fc}, addr {address}): {response}"
                            )
                            self.connection_manager.mark_disconnected()

                    except ConnectionException as ce:
                        self.logger.error(
                            f"[{self.name}] Connection error writing "
                            f"FC {point.fc}, offset {point.offset}: {ce}"
                        )
                        self.connection_manager.mark_disconnected()
                    except Exception as e:
                        self.logger.error(
                            f"[{self.name}] Error writing "
                            f"FC {point.fc}, offset {point.offset}: {e}"
                        )
                        self.connection_manager.mark_disconnected()

                # 3. CYCLE TIMING - Sleep for GCD cycle time
                cycle_elapsed = time.monotonic() - cycle_start_time
                sleep_duration = max(0, gcd_cycle_time_seconds - cycle_elapsed)
                if sleep_duration > 0:
                    # Sleep in small increments (100ms each) to allow for quick shutdown
                    sleep_increment = 0.1
                    remaining_sleep = sleep_duration

                    while remaining_sleep > 0 and not self._stop_event.is_set():
                        actual_sleep = min(sleep_increment, remaining_sleep)
                        time.sleep(actual_sleep)
                        remaining_sleep -= actual_sleep

                # Increment cycle counter
                cycle_counter += 1

        except ConnectionException as ce:
            self.logger.error(f"[{self.name}] Connection failed: {ce}")
            # Mark as disconnected to force reconnection
            self.connection_manager.mark_disconnected()
        except Exception as e:
            self.logger.error(f"[{self.name}] Unexpected error in thread: {e}")
            traceback.print_exc()
        finally:
            self.connection_manager.disconnect()
            self.logger.info(f"[{self.name}] Thread finished and connection closed.")

    def stop(self):
        self.logger.info(f"[{self.name}] Stop signal received.")
        self._stop_event.set()


class ModbusBusHandler(threading.Thread):
    """
    Handles multiple Modbus devices that share ONE connection, multiplexed by
    slave/unit ID. One thread per bus, managing multiple slave IDs over a single
    connection. Used for two cases:
      - RTU multi-drop: several devices on one serial port (different slave IDs).
      - TCP gateway (TCP-to-RTU converter): several serial slaves behind one
        IP:port, distinguished by their unit/slave ID.

    This differs from ModbusSlaveDevice (one connection per device) in that:
    - A single connection is shared by multiple devices.
    - Each device has a unique slave_id.
    - Each IO operation carries that slave_id (device_id / MBAP unit byte) so the
      gateway or bus routes it to the correct slave.

    The run() loop is transport-agnostic; only the connection setup differs.
    """

    def __init__(
        self,
        transport: str,            # "tcp" or "rtu"
        connection_config: dict,   # tcp: {host, port, timeout_ms};
                                   # rtu: {serial_port, baud_rate, parity, stop_bits, data_bits, timeout_ms}
        devices: List[Any],        # List of ModbusDeviceConfig sharing this connection
        sba: SafeBufferAccess,
        plugin_logger: PluginLogger,
    ):
        super().__init__(daemon=True)
        self.transport = transport
        self.connection_config = connection_config
        self.devices = devices
        self.sba = sba
        self.logger = plugin_logger
        self._stop_event = threading.Event()

        # Single shared connection for the whole bus/gateway. slave_id here is a
        # placeholder; every request uses its device-specific slave_id below.
        if transport == "tcp":
            self.connection_manager = ModbusConnectionManager(
                transport="tcp",
                host=connection_config["host"],
                port=connection_config["port"],
                timeout_ms=connection_config["timeout_ms"],
                slave_id=1,
            )
            self.name = f"ModbusTcpGateway-{connection_config['host']}:{connection_config['port']}"
        else:
            self.connection_manager = ModbusConnectionManager(
                transport="rtu",
                serial_port=connection_config["serial_port"],
                baud_rate=connection_config["baud_rate"],
                parity=connection_config["parity"],
                stop_bits=connection_config["stop_bits"],
                data_bits=connection_config["data_bits"],
                timeout_ms=connection_config["timeout_ms"],
                slave_id=1,  # Default, will use device-specific slave_id per operation
            )
            self.name = f"ModbusRtuBus-{connection_config['serial_port']}"

        # Build consolidated IO point list with slave_id
        # Each entry: {"point": io_point, "slave_id": device.slave_id, "device_name": device.name}
        self.all_io_points = []
        for device in devices:
            for point in device.io_points:
                self.all_io_points.append({
                    "point": point,
                    "slave_id": device.slave_id,
                    "device_name": device.name,
                })

        # Calculate GCD of all IO point cycle times across all devices on this bus
        all_cycle_times = [p.cycle_time_ms for d in devices for p in d.io_points]
        self.gcd_cycle_time_ms = calculate_gcd_of_cycle_times(
            [type('obj', (object,), {'cycle_time_ms': ct})() for ct in all_cycle_times]
        ) if all_cycle_times else 1000

        device_names = ", ".join([d.name for d in devices])
        self.logger.info(
            f"[{self.name}] bus handler created for devices: {device_names} "
            f"({len(self.all_io_points)} total IO points, GCD cycle: {self.gcd_cycle_time_ms}ms)"
        )

    def _ensure_connection(self) -> bool:
        """Ensures there is a valid connection, reconnecting if necessary."""
        return self.connection_manager.ensure_connection(self._stop_event)

    def run(self):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self.logger.info(f"[{self.name}] Thread started for {len(self.devices)} device(s).")

        gcd_cycle_time_seconds = self.gcd_cycle_time_ms / 1000.0

        if not self.all_io_points:
            self.logger.warn(f"[{self.name}] No I/O points defined. Stopping thread.")
            return

        # Connect with infinite retry
        if not self.connection_manager.connect_with_retry(self._stop_event):
            self.logger.info(f"[{self.name}] Thread stopped before connection could be established.")
            return

        # Initialize cycle counter
        cycle_counter = 0

        try:
            while not self._stop_event.is_set():
                cycle_start_time = time.monotonic()

                # Ensure connection exists before cycle
                if not self._ensure_connection():
                    break  # Thread was interrupted

                # 1. READ OPERATIONS - Process only I/O points that are due for polling this cycle
                read_results_to_update = []  # Store tuples: (iec_addr, modbus_data, length)

                for io_entry in self.all_io_points:
                    if self._stop_event.is_set():
                        break

                    point = io_entry["point"]
                    slave_id = io_entry["slave_id"]

                    # Skip if not a read operation
                    if point.fc not in [1, 2, 3, 4]:
                        continue

                    # Check if point should be polled this cycle
                    point_cycle_multiple = point.cycle_time_ms // self.gcd_cycle_time_ms
                    if (cycle_counter % point_cycle_multiple) != 0:
                        continue

                    try:
                        # Parse Modbus offset
                        address = parse_modbus_offset(point.offset)

                        # Calculate the correct number of Modbus registers/coils needed
                        if point.fc in [3, 4]:  # Register-based operations
                            iec_size = point.iec_location.size
                            registers_per_iec_element = get_modbus_registers_count_for_iec_size(
                                iec_size
                            )
                            count = point.length * registers_per_iec_element
                        else:  # Coil/Discrete Input operations
                            count = point.length

                        # Perform Modbus read with device-specific slave_id
                        if point.fc == 1:  # Read Coils
                            response = self.connection_manager.client.read_coils(
                                address, count=count, device_id=slave_id
                            )
                        elif point.fc == 2:  # Read Discrete Inputs
                            response = self.connection_manager.client.read_discrete_inputs(
                                address, count=count, device_id=slave_id
                            )
                        elif point.fc == 3:  # Read Holding Registers
                            response = self.connection_manager.client.read_holding_registers(
                                address, count=count, device_id=slave_id
                            )
                        elif point.fc == 4:  # Read Input Registers
                            response = self.connection_manager.client.read_input_registers(
                                address, count=count, device_id=slave_id
                            )
                        else:
                            self.logger.warn(f"[{self.name}] Unsupported read FC: {point.fc}")
                            continue

                        # Check if response is valid
                        if isinstance(response, (ModbusIOException, ExceptionResponse)):
                            self.logger.error(
                                f"[{self.name}] Modbus read error "
                                f"(slave {slave_id}, FC {point.fc}, addr {address}): {response}"
                            )
                            self.connection_manager.mark_disconnected()
                            continue
                        if response.isError():
                            self.logger.error(
                                f"[{self.name}] Modbus read failed "
                                f"(slave {slave_id}, FC {point.fc}, addr {address}): {response}"
                            )
                            self.connection_manager.mark_disconnected()
                            continue

                        # Extract data from response
                        if point.fc in [1, 2]:
                            modbus_data = response.bits
                        else:
                            modbus_data = response.registers

                        # Store for batch update
                        read_results_to_update.append(
                            (point.iec_location, modbus_data, point.length)
                        )

                    except ValueError as ve:
                        self.logger.error(
                            f"[{self.name}] Invalid offset "
                            f"'{point.offset}' for slave {slave_id}, FC {point.fc}: {ve}"
                        )
                    except ConnectionException as ce:
                        self.logger.error(
                            f"[{self.name}] Connection error reading "
                            f"slave {slave_id}, FC {point.fc}, offset {point.offset}: {ce}"
                        )
                        self.connection_manager.mark_disconnected()
                    except Exception as e:
                        self.logger.error(
                            f"[{self.name}] Error reading "
                            f"slave {slave_id}, FC {point.fc}, offset {point.offset}: {e}"
                        )
                        self.connection_manager.mark_disconnected()

                # Batch update IEC buffers with single mutex acquisition
                if read_results_to_update:
                    preconverted_updates = []
                    for iec_addr, modbus_data, length in read_results_to_update:
                        converted_values, details = convert_modbus_data_to_iec_values(
                            iec_addr, modbus_data, length
                        )
                        if converted_values is not None and details is not None:
                            preconverted_updates.append((converted_values, details))
                        else:
                            self.logger.error(
                                f"[{self.name}] Data conversion failed "
                                f"for IEC address {iec_addr}, length={length}"
                            )

                    if preconverted_updates:
                        lock_acquired, lock_msg = self.sba.acquire_mutex()
                        if lock_acquired:
                            try:
                                for converted_values, details in preconverted_updates:
                                    write_preconverted_iec_values(
                                        self.sba, converted_values, details
                                    )
                            finally:
                                self.sba.release_mutex()
                        else:
                            self.logger.error(
                                f"[{self.name}] Failed to acquire mutex "
                                f"for read updates: {lock_msg}"
                            )

                # 2. WRITE OPERATIONS
                write_points_due = []
                for io_entry in self.all_io_points:
                    if self._stop_event.is_set():
                        break

                    point = io_entry["point"]
                    slave_id = io_entry["slave_id"]

                    if point.fc not in [5, 6, 15, 16]:  # Write functions
                        continue

                    point_cycle_multiple = point.cycle_time_ms // self.gcd_cycle_time_ms
                    if (cycle_counter % point_cycle_multiple) != 0:
                        continue

                    try:
                        address = parse_modbus_offset(point.offset)
                        write_points_due.append((point, address, slave_id))
                    except ValueError as ve:
                        self.logger.error(
                            f"[{self.name}] Invalid offset "
                            f"'{point.offset}' for slave {slave_id}, FC {point.fc}: {ve}"
                        )

                # Phase 2: Read all raw IEC values under a single mutex acquisition
                raw_iec_data = []
                if write_points_due:
                    lock_acquired, lock_msg = self.sba.acquire_mutex()
                    if lock_acquired:
                        try:
                            for point, address, slave_id in write_points_due:
                                raw_values, details, iec_size = read_raw_iec_values(
                                    self.sba, point.iec_location, point.length
                                )
                                if raw_values is not None:
                                    raw_iec_data.append(
                                        (point, address, slave_id, raw_values, details, iec_size)
                                    )
                                else:
                                    self.logger.error(
                                        f"[{self.name}] Failed to read raw IEC data "
                                        f"for write (slave {slave_id}, FC {point.fc}, offset {point.offset})"
                                    )
                        finally:
                            self.sba.release_mutex()
                    else:
                        self.logger.error(
                            f"[{self.name}] Failed to acquire mutex "
                            f"for batch write prep: {lock_msg}"
                        )

                # Phase 3: Convert and perform Modbus writes
                for point, address, slave_id, raw_values, details, iec_size in raw_iec_data:
                    if self._stop_event.is_set():
                        break

                    try:
                        values_to_write = convert_raw_iec_to_modbus(raw_values, details, iec_size)

                        if values_to_write is None:
                            self.logger.error(
                                f"[{self.name}] Failed to convert data "
                                f"for Modbus write (slave {slave_id}, FC {point.fc}, "
                                f"offset {point.offset})"
                            )
                            continue

                        # Perform Modbus write with device-specific slave_id
                        if point.fc == 5:  # Write Single Coil
                            if len(values_to_write) > 0:
                                response = self.connection_manager.client.write_coil(
                                    address, values_to_write[0], device_id=slave_id
                                )
                            else:
                                self.logger.error(
                                    f"[{self.name}] No data to write "
                                    f"for slave {slave_id}, FC 5, offset {address}"
                                )
                                continue
                        elif point.fc == 6:  # Write Single Register
                            if len(values_to_write) > 0:
                                response = self.connection_manager.client.write_register(
                                    address, values_to_write[0], device_id=slave_id
                                )
                            else:
                                self.logger.error(
                                    f"[{self.name}] No data to write "
                                    f"for slave {slave_id}, FC 6, offset {address}"
                                )
                                continue
                        elif point.fc == 15:  # Write Multiple Coils
                            response = self.connection_manager.client.write_coils(
                                address, values_to_write, device_id=slave_id
                            )
                        elif point.fc == 16:  # Write Multiple Registers
                            response = self.connection_manager.client.write_registers(
                                address, values_to_write, device_id=slave_id
                            )
                        else:
                            self.logger.warn(f"[{self.name}] Unsupported write FC: {point.fc}")
                            continue

                        # Check write response
                        if isinstance(response, (ModbusIOException, ExceptionResponse)):
                            self.logger.error(
                                f"[{self.name}] Modbus write error "
                                f"(slave {slave_id}, FC {point.fc}, addr {address}): {response}"
                            )
                            self.connection_manager.mark_disconnected()
                        elif response.isError():
                            self.logger.error(
                                f"[{self.name}] Modbus write failed "
                                f"(slave {slave_id}, FC {point.fc}, addr {address}): {response}"
                            )
                            self.connection_manager.mark_disconnected()

                    except ConnectionException as ce:
                        self.logger.error(
                            f"[{self.name}] Connection error writing "
                            f"slave {slave_id}, FC {point.fc}, offset {point.offset}: {ce}"
                        )
                        self.connection_manager.mark_disconnected()
                    except Exception as e:
                        self.logger.error(
                            f"[{self.name}] Error writing "
                            f"slave {slave_id}, FC {point.fc}, offset {point.offset}: {e}"
                        )
                        self.connection_manager.mark_disconnected()

                # 3. CYCLE TIMING
                cycle_elapsed = time.monotonic() - cycle_start_time
                sleep_duration = max(0, gcd_cycle_time_seconds - cycle_elapsed)
                if sleep_duration > 0:
                    sleep_increment = 0.1
                    remaining_sleep = sleep_duration

                    while remaining_sleep > 0 and not self._stop_event.is_set():
                        actual_sleep = min(sleep_increment, remaining_sleep)
                        time.sleep(actual_sleep)
                        remaining_sleep -= actual_sleep

                cycle_counter += 1

        except ConnectionException as ce:
            self.logger.error(f"[{self.name}] Connection failed: {ce}")
            self.connection_manager.mark_disconnected()
        except Exception as e:
            self.logger.error(f"[{self.name}] Unexpected error in thread: {e}")
            traceback.print_exc()
        finally:
            self.connection_manager.disconnect()
            self.logger.info(f"[{self.name}] Thread finished and connection closed.")

    def stop(self):
        self.logger.info(f"[{self.name}] Stop signal received.")
        self._stop_event.set()


# Backward-compatible alias: the bus handler used to be RTU-only.
ModbusRtuBusHandler = ModbusBusHandler


def group_rtu_devices_by_bus(devices: List[Any]) -> dict:
    """
    Group RTU devices by serial port configuration (forming unique "buses").

    Devices on the same serial port with the same settings share a connection.
    Each bus has one thread handling all devices (different slave IDs).

    Args:
        devices: List of ModbusDeviceConfig with transport="rtu"

    Returns:
        Dictionary keyed by bus identifier:
        {
            "serial_port:baud_rate:parity:stop_bits:data_bits": {
                "config": {serial_port, baud_rate, parity, stop_bits, data_bits, timeout_ms},
                "devices": [list of devices on this bus]
            }
        }
    """
    buses = {}

    for device in devices:
        bus_key = (
            f"{device.serial_port}:{device.baud_rate}:"
            f"{device.parity}:{device.stop_bits}:{device.data_bits}"
        )

        if bus_key not in buses:
            buses[bus_key] = {
                "config": {
                    "serial_port": device.serial_port,
                    "baud_rate": device.baud_rate,
                    "parity": device.parity,
                    "stop_bits": device.stop_bits,
                    "data_bits": device.data_bits,
                    "timeout_ms": device.timeout_ms,
                },
                "devices": [],
            }

        buses[bus_key]["devices"].append(device)

        # Use minimum timeout across all devices on the bus
        if device.timeout_ms < buses[bus_key]["config"]["timeout_ms"]:
            buses[bus_key]["config"]["timeout_ms"] = device.timeout_ms

    return buses


def group_tcp_devices_by_endpoint(devices: List[Any]) -> dict:
    """
    Group TCP devices by (host, port) endpoint.

    Several devices may share one endpoint when it is a Modbus gateway
    (TCP-to-RTU converter): one IP:port fronts multiple serial slaves, each with
    a distinct slave/unit ID. Devices on the same endpoint share a single TCP
    connection (one ModbusBusHandler thread) and are routed by slave_id; a lone
    device on an endpoint keeps the simpler one-thread ModbusSlaveDevice path.

    Args:
        devices: List of ModbusDeviceConfig with transport="tcp"

    Returns:
        Dictionary keyed by "host:port":
        {
            "host:port": {
                "config": {host, port, timeout_ms},
                "devices": [list of devices on this endpoint],
            }
        }
    """
    endpoints = {}

    for device in devices:
        endpoint_key = f"{device.host}:{device.port}"

        if endpoint_key not in endpoints:
            endpoints[endpoint_key] = {
                "config": {
                    "host": device.host,
                    "port": device.port,
                    "timeout_ms": device.timeout_ms,
                },
                "devices": [],
            }

        endpoints[endpoint_key]["devices"].append(device)

        # Use minimum timeout across all devices sharing the endpoint
        if device.timeout_ms < endpoints[endpoint_key]["config"]["timeout_ms"]:
            endpoints[endpoint_key]["config"]["timeout_ms"] = device.timeout_ms

    return endpoints


def init(args_capsule):
    """
    Initialize the Modbus Master plugin.
    This function is called once when the plugin is loaded.
    """
    global runtime_args, logger

    # Initialize logger early (without runtime_args for now)
    logger = PluginLogger("MODBUS_MASTER", None)
    logger.info("Initializing...")

    try:
        # Extract runtime arguments from capsule
        runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
        if not runtime_args:
            logger.error(f"Failed to extract runtime args: {error_msg}")
            return False

        # Re-initialize logger with runtime_args for central logging
        logger = PluginLogger("MODBUS_MASTER", runtime_args)
        logger.info("Runtime arguments extracted successfully")

        return True

    except Exception as e:
        logger.error(f"Error during initialization: {e}")
        traceback.print_exc()
        return False


def start_loop():
    """
    Start the main loop for all configured Modbus devices.
    This function is called after successful initialization.

    TCP devices: grouped by (host, port). A lone device on an endpoint runs as
      ModbusSlaveDevice (one connection); multiple devices on one endpoint (a
      Modbus gateway / TCP-to-RTU converter) share one connection via
      ModbusBusHandler, multiplexed by slave/unit ID.
    RTU devices: One thread per serial bus (ModbusBusHandler), supporting multi-drop.
    """
    global slave_threads, modbus_master_config, safe_buffer_accessor, logger

    logger.info("Starting main loop...")

    try:
        if not runtime_args:
            logger.error("Plugin not properly initialized")
            return False

        # Create safe buffer accessor
        safe_buffer_accessor = SafeBufferAccess(runtime_args)
        if not safe_buffer_accessor.is_valid:
            logger.error(f"Failed to create SafeBufferAccess: {safe_buffer_accessor.error_msg}")
            return False

        logger.info("SafeBufferAccess created successfully")

        # Load configuration
        config_path, config_error = safe_buffer_accessor.get_config_path()
        if not config_path:
            logger.error(f"Failed to get config path: {config_error}")
            return False

        logger.debug(f"Loading configuration from: {config_path}")

        modbus_master_config = ModbusMasterConfig()
        modbus_master_config.import_config_from_file(config_path)
        modbus_master_config.validate()

        device_count = len(modbus_master_config.devices)
        logger.info(f"Configuration loaded successfully: {device_count} device(s)")

        # Separate TCP and RTU devices
        tcp_devices = [d for d in modbus_master_config.devices if d.transport == "tcp"]
        rtu_devices = [d for d in modbus_master_config.devices if d.transport == "rtu"]

        logger.info(f"Found {len(tcp_devices)} TCP device(s) and {len(rtu_devices)} RTU device(s)")

        # Group TCP devices by (host, port). A lone device on an endpoint keeps
        # the simple one-thread-per-device path; multiple devices on one endpoint
        # (a Modbus gateway / TCP-to-RTU converter) share a single TCP connection
        # and are multiplexed by slave/unit ID via ModbusBusHandler.
        if tcp_devices:
            tcp_endpoints = group_tcp_devices_by_endpoint(tcp_devices)
            logger.info(f"TCP devices grouped into {len(tcp_endpoints)} endpoint(s)")

            for endpoint_key, endpoint_info in tcp_endpoints.items():
                endpoint_devices = endpoint_info["devices"]
                try:
                    if len(endpoint_devices) == 1:
                        device_config = endpoint_devices[0]
                        device_thread = ModbusSlaveDevice(device_config, safe_buffer_accessor, logger)
                        device_thread.start()
                        slave_threads.append(device_thread)
                        logger.info(
                            f"Started TCP thread for device: {device_config.name} "
                            f"({device_config.host}:{device_config.port}, slave_id={device_config.slave_id})"
                        )
                    else:
                        gateway_thread = ModbusBusHandler(
                            transport="tcp",
                            connection_config=endpoint_info["config"],
                            devices=endpoint_devices,
                            sba=safe_buffer_accessor,
                            plugin_logger=logger,
                        )
                        gateway_thread.start()
                        slave_threads.append(gateway_thread)
                        device_names = ", ".join([d.name for d in endpoint_devices])
                        slave_ids = ", ".join([str(d.slave_id) for d in endpoint_devices])
                        logger.info(
                            f"Started TCP gateway thread: {endpoint_key} "
                            f"(devices: {device_names}, slave_ids: {slave_ids})"
                        )
                except Exception as e:
                    logger.error(f"Failed to start TCP thread(s) for endpoint {endpoint_key}: {e}")

        # Group RTU devices by serial port configuration
        if rtu_devices:
            rtu_buses = group_rtu_devices_by_bus(rtu_devices)
            logger.info(f"RTU devices grouped into {len(rtu_buses)} serial bus(es)")

            # Start RTU bus handlers (one thread per serial port)
            for bus_key, bus_info in rtu_buses.items():
                try:
                    bus_thread = ModbusBusHandler(
                        transport="rtu",
                        connection_config=bus_info["config"],
                        devices=bus_info["devices"],
                        sba=safe_buffer_accessor,
                        plugin_logger=logger,
                    )
                    bus_thread.start()
                    slave_threads.append(bus_thread)

                    device_names = ", ".join([d.name for d in bus_info["devices"]])
                    slave_ids = ", ".join([str(d.slave_id) for d in bus_info["devices"]])
                    logger.info(
                        f"Started RTU bus thread: {bus_info['config']['serial_port']} "
                        f"(devices: {device_names}, slave_ids: {slave_ids})"
                    )
                except Exception as e:
                    logger.error(f"Failed to start RTU bus thread for {bus_key}: {e}")

        if slave_threads:
            logger.info(f"Successfully started {len(slave_threads)} device/bus thread(s)")
            return True
        else:
            logger.error("No device threads started")
            return False

    except Exception as e:
        logger.error(f"Error starting main loop: {e}")
        traceback.print_exc()
        return False


def stop_loop():
    """
    Stop the main loop and all running device threads.
    This function is called when the plugin needs to be stopped.

    Returns:
        True if all threads stopped, False if any thread survived the timeout.
    """
    global slave_threads, logger  # pylint: disable=global-variable-not-assigned

    logger.info("Stopping main loop...")

    try:
        if not slave_threads:
            logger.info("No threads to stop")
            return True

        # Signal all threads to stop
        for thread in slave_threads:
            try:
                if hasattr(thread, "stop"):
                    thread.stop()
                else:
                    logger.warn(f"Thread {thread.name} does not have a stop method")
            except Exception as e:
                logger.error(f"Error stopping thread {thread.name}: {e}")

        # Wait for all threads to finish under a shared deadline so total
        # wall-clock time is bounded regardless of how many threads exist.
        deadline = time.time() + 10.0
        any_alive = False
        for thread in slave_threads:
            remaining = max(0, deadline - time.time())
            try:
                thread.join(timeout=remaining)
                if thread.is_alive():
                    logger.error(f"Thread {thread.name} did not stop within timeout")
                    any_alive = True
                else:
                    logger.info(f"Thread {thread.name} stopped successfully")
            except Exception as e:
                logger.error(f"Error joining thread {thread.name}: {e}")

        logger.info("Main loop stopped")
        return not any_alive

    except Exception as e:
        logger.error(f"Error stopping main loop: {e}")
        traceback.print_exc()
        return False


def cleanup():
    """
    Clean up resources before plugin unload.
    This function is called when the plugin is being unloaded.
    """
    # pylint: disable=global-variable-not-assigned
    global runtime_args, modbus_master_config, safe_buffer_accessor
    global slave_threads, logger
    # pylint: enable=global-variable-not-assigned

    logger.info("Cleaning up...")

    try:
        # Stop all threads if not already stopped
        stop_loop()

        # Clear thread list
        slave_threads.clear()

        # Reset global variables
        runtime_args = None
        modbus_master_config = None
        safe_buffer_accessor = None

        logger.info("Cleanup completed successfully")
        return True

    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    # Test mode for development purposes.
    # This allows running the plugin standalone for testing.
    print(" Modbus Master Plugin - Test Mode")
    print("This plugin is designed to be loaded by the OpenPLC runtime.")
    print("Standalone testing is not fully supported without runtime integration.")

    # You could add basic configuration validation here
    try:
        test_config = ModbusMasterConfig()
        print("(PASS) Configuration model can be instantiated")
    except Exception as e:
        print(f"(FAIL) Error testing configuration model: {e}")
