# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

import os
import sys
import threading
import time
import traceback
from typing import Any, List, Tuple, Union

from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.pdu import ExceptionResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# pylint: disable=wrong-import-position
from shared import (
    PluginLogger,
    SafeBufferAccess,
    safe_extract_runtime_args_from_capsule,
)

from shared.image_sizes import set_image_sizes  # noqa: F401
from shared.plugin_config_decode.modbus_master_config_model import (
    ERROR_HANDLING_SET_TO_ZERO,
    IECAddress,
    ModbusDeviceConfig,
    ModbusIoPointConfig,
    ModbusMasterConfig,
)

# pylint: enable=wrong-import-position

try:
    from .modbus_master_connection import ModbusConnectionManager
    from .modbus_master_memory import (
        convert_modbus_data_to_iec_values,
        convert_raw_iec_to_modbus,
        read_raw_iec_values,
        write_preconverted_iec_values,
    )
    from .modbus_master_utils import (
        calculate_gcd_of_cycle_times,
        get_read_count_for_io_point,
        get_zero_payload_for_io_point,
        parse_modbus_offset,
    )
except ImportError:
    from modbus_master_connection import ModbusConnectionManager
    from modbus_master_memory import (
        convert_modbus_data_to_iec_values,
        convert_raw_iec_to_modbus,
        read_raw_iec_values,
        write_preconverted_iec_values,
    )
    from modbus_master_utils import (
        calculate_gcd_of_cycle_times,
        get_read_count_for_io_point,
        get_zero_payload_for_io_point,
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

ReadResult = Tuple[IECAddress, List[Union[bool, int]], int]
READ_FUNCTION_CODES = (1, 2, 3, 4)

def queue_zero_fill_on_failure(point: Any, read_results_to_update: List[Any]) -> bool:
    """
    Queue a zeroed payload for a read point whose group asks for it.

    Points configured to keep the last value are left untouched.

    Returns True when a zero-fill was queued, so callers can log it.
    """
    if getattr(point, "error_handling", None) != ERROR_HANDLING_SET_TO_ZERO:
        return False
    read_results_to_update.append(
        (point.iec_location, get_zero_payload_for_io_point(point), point.length)
    )
    return True

def zero_fills_for_set_to_zero_read_points(points: List[ModbusIoPointConfig]) -> List[ReadResult]:
    """Return a zeroed read result for every read point whose error handling is set-to-zero."""
    zero_fills: List[ReadResult] = []
    for point in points:
        if point.fc in READ_FUNCTION_CODES:
            queue_zero_fill_on_failure(point, zero_fills)
    return zero_fills

def write_read_results(
    sba: SafeBufferAccess,
    plugin_logger: PluginLogger,
    thread_name: str,
    read_results: List[ReadResult],
) -> None:
    """
    Write read results to the IEC buffers.

    The Modbus data is converted to IEC values outside the mutex, then every value is
    written under a single mutex acquisition to keep the hold time minimal.
    """
    if not read_results:
        return
    preconverted_updates = []
    for iec_addr, modbus_data, length in read_results:
        converted_values, details = convert_modbus_data_to_iec_values(iec_addr, modbus_data, length)
        if converted_values is not None and details is not None:
            preconverted_updates.append((converted_values, details))
        else:
            plugin_logger.error(
                f"[{thread_name}] Data conversion failed "
                f"for IEC address {iec_addr}, length={length}"
            )

    if not preconverted_updates:
        return
    lock_acquired, lock_msg = sba.acquire_mutex()
    if lock_acquired:
        try:
            for converted_values, details in preconverted_updates:
                write_preconverted_iec_values(sba, converted_values, details)
        finally:
            sba.release_mutex()
    else:
        plugin_logger.error(f"[{thread_name}] Failed to acquire mutex for read updates: {lock_msg}")

def give_up_on_endpoint(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    sba: SafeBufferAccess,
    plugin_logger: PluginLogger,
    thread_name: str,
    connection_manager: ModbusConnectionManager,
    devices: List[ModbusDeviceConfig],
    pending_reads: List[ReadResult],
) -> None:
    """
    Finish serving a TCP endpoint whose connection was given up.

    Writes the pending read results followed by a zero-fill of every set-to-zero read
    point of the endpoint's devices, then logs the give-up once as a warning.

    Args:
        sba: Buffer access used for the final IEC writes.
        plugin_logger: Logger that receives the give-up warning.
        thread_name: Name of the thread serving the endpoint, for error messages.
        connection_manager: Connection manager holding the endpoint and the give-up reason.
        devices: ModbusDeviceConfig objects served through the endpoint.
        pending_reads: Read results of the current cycle not yet written.
    """
    points = [point for device in devices for point in device.io_points]
    write_read_results(
        sba,
        plugin_logger,
        thread_name,
        pending_reads + zero_fills_for_set_to_zero_read_points(points),
    )
    device_names = ", ".join(device.name for device in devices)
    plugin_logger.warn(
        f"Gave up on {connection_manager.host}:{connection_manager.port} "
        f"(devices: {device_names}): {connection_manager.give_up_reason}; "
        "Reconnect is off. Stop then Start the PLC to retry."
    )

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

        self.connection_manager = ModbusConnectionManager(
            transport="tcp",
            host=device_config.host,
            port=device_config.port,
            timeout_ms=device_config.timeout_ms,
            slave_id=device_config.slave_id,
            reconnect=device_config.reconnect,
        )
        self.name = (
            f"ModbusSlave-{device_config.name}-TCP-{device_config.host}:{device_config.port}"
        )

        self.gcd_cycle_time_ms = calculate_gcd_of_cycle_times(device_config.io_points)
        self.logger.info(f"[{self.name}] Calculated GCD cycle time: {self.gcd_cycle_time_ms}ms")

    def _ensure_connection(self) -> bool:
        """
        Ensures there is a valid connection, reconnecting if necessary.

        Returns:
            True if connection is available, False if thread was interrupted
        """
        return self.connection_manager.ensure_connection(self._stop_event)

    def _give_up(self, pending_reads: List[ReadResult]) -> None:
        """Zero-fill the set-to-zero read points and log that the endpoint was given up."""
        give_up_on_endpoint(
            self.sba,
            self.logger,
            self.name,
            self.connection_manager,
            [self.device_config],
            pending_reads,
        )

    def _on_read_failure(
        self, point: ModbusIoPointConfig, failure: object, read_results: List[ReadResult]
    ) -> None:
        """Apply the reconnect policy to a failed read and queue its zero-fill unless given up."""
        self.connection_manager.handle_request_failure(failure)
        if not self.connection_manager.given_up:
            queue_zero_fill_on_failure(point, read_results)

    def run(self):  # pylint: disable=too-many-locals
        self.logger.info(f"[{self.name}] Thread started.")

        io_points = self.device_config.io_points
        gcd_cycle_time_seconds = self.gcd_cycle_time_ms / 1000.0

        if not io_points:
            self.logger.warn(f"[{self.name}] No I/O points defined. Stopping thread.")
            return

        if not self.connection_manager.connect(self._stop_event):
            if self.connection_manager.given_up:
                self._give_up([])
            else:
                self.logger.info(
                    f"[{self.name}] Thread stopped before connection could be established."
                )
            return

        cycle_counter = 0

        try:
            while not self._stop_event.is_set():
                cycle_start_time = time.monotonic()

                if not self._ensure_connection():
                    if self.connection_manager.given_up:
                        self._give_up([])
                    break

                read_results_to_update = []

                for point in io_points:
                    if self._stop_event.is_set() or self.connection_manager.given_up:
                        break

                    if point.fc not in [1, 2, 3, 4]:
                        continue

                    point_cycle_multiple = point.cycle_time_ms // self.gcd_cycle_time_ms
                    if (cycle_counter % point_cycle_multiple) != 0:
                        continue

                    try:
                        address = parse_modbus_offset(point.offset)

                        count = get_read_count_for_io_point(point)

                        if point.fc == 1:
                            response = self.connection_manager.client.read_coils(
                                address, count=count, device_id=self.connection_manager.slave_id
                            )
                        elif point.fc == 2:
                            response = self.connection_manager.client.read_discrete_inputs(
                                address, count=count, device_id=self.connection_manager.slave_id
                            )
                        elif point.fc == 3:
                            response = self.connection_manager.client.read_holding_registers(
                                address, count=count, device_id=self.connection_manager.slave_id
                            )
                        elif point.fc == 4:
                            response = self.connection_manager.client.read_input_registers(
                                address, count=count, device_id=self.connection_manager.slave_id
                            )
                        else:
                            self.logger.warn(f"[{self.name}] Unsupported read FC: {point.fc}")
                            continue

                        if isinstance(response, (ModbusIOException, ExceptionResponse)):
                            self.logger.error(
                                f"[{self.name}] Modbus read error "
                                f"(FC {point.fc}, addr {address}): {response}"
                            )
                            self._on_read_failure(point, response, read_results_to_update)
                            continue
                        if response.isError():
                            self.logger.error(
                                f"[{self.name}] Modbus read failed "
                                f"(FC {point.fc}, addr {address}): {response}"
                            )
                            self._on_read_failure(point, response, read_results_to_update)
                            continue

                        if point.fc in [1, 2]:
                            modbus_data = response.bits
                        else:
                            modbus_data = response.registers

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
                        self._on_read_failure(point, ce, read_results_to_update)
                    except Exception as e:
                        self.logger.error(
                            f"[{self.name}] Error reading "
                            f"FC {point.fc}, offset {point.offset}: {e}"
                        )
                        self._on_read_failure(point, e, read_results_to_update)

                if self.connection_manager.given_up:
                    self._give_up(read_results_to_update)
                    break

                write_read_results(self.sba, self.logger, self.name, read_results_to_update)


                write_points_due = []
                for point in io_points:
                    if self._stop_event.is_set():
                        break

                    if point.fc not in [5, 6, 15, 16]:
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

                raw_iec_data = []
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

                for point, address, raw_values, details, iec_size in raw_iec_data:
                    if self._stop_event.is_set() or self.connection_manager.given_up:
                        break

                    try:
                        values_to_write = convert_raw_iec_to_modbus(raw_values, details, iec_size)

                        if values_to_write is None:
                            self.logger.error(
                                f"[{self.name}] Failed to convert data "
                                f"for Modbus write (FC {point.fc}, "
                                f"offset {point.offset})"
                            )
                            continue

                        if point.fc == 5:
                            if len(values_to_write) > 0:
                                response = self.connection_manager.client.write_coil(
                                    address,
                                    values_to_write[0],
                                    device_id=self.connection_manager.slave_id,
                                )
                            else:
                                self.logger.error(
                                    f"[{self.name}] No data to write " f"for FC 5, offset {address}"
                                )
                                continue
                        elif point.fc == 6:
                            if len(values_to_write) > 0:
                                response = self.connection_manager.client.write_register(
                                    address,
                                    values_to_write[0],
                                    device_id=self.connection_manager.slave_id,
                                )
                            else:
                                self.logger.error(
                                    f"[{self.name}] No data to write " f"for FC 6, offset {address}"
                                )
                                continue
                        elif point.fc == 15:
                            response = self.connection_manager.client.write_coils(
                                address, values_to_write, device_id=self.connection_manager.slave_id
                            )
                        elif point.fc == 16:
                            response = self.connection_manager.client.write_registers(
                                address, values_to_write, device_id=self.connection_manager.slave_id
                            )
                        else:
                            self.logger.warn(f"[{self.name}] Unsupported write FC: {point.fc}")
                            continue

                        if isinstance(response, (ModbusIOException, ExceptionResponse)):
                            self.logger.error(
                                f"[{self.name}] Modbus write error "
                                f"(FC {point.fc}, addr {address}): {response}"
                            )
                            self.connection_manager.handle_request_failure(response)
                        elif response.isError():
                            self.logger.error(
                                f"[{self.name}] Modbus write failed "
                                f"(FC {point.fc}, addr {address}): {response}"
                            )
                            self.connection_manager.handle_request_failure(response)

                    except ConnectionException as ce:
                        self.logger.error(
                            f"[{self.name}] Connection error writing "
                            f"FC {point.fc}, offset {point.offset}: {ce}"
                        )
                        self.connection_manager.handle_request_failure(ce)
                    except Exception as e:
                        self.logger.error(
                            f"[{self.name}] Error writing "
                            f"FC {point.fc}, offset {point.offset}: {e}"
                        )
                        self.connection_manager.handle_request_failure(e)

                if self.connection_manager.given_up:
                    self._give_up([])
                    break

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
        transport: str,
        connection_config: dict,
        devices: List[Any],
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

        if transport == "tcp":
            self.connection_manager = ModbusConnectionManager(
                transport="tcp",
                host=connection_config["host"],
                port=connection_config["port"],
                timeout_ms=connection_config["timeout_ms"],
                slave_id=1,
                reconnect=connection_config.get("reconnect", True),
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
                slave_id=1,
            )
            self.name = f"ModbusRtuBus-{connection_config['serial_port']}"

        self.all_io_points = []
        for device in devices:
            for point in device.io_points:
                self.all_io_points.append(
                    {
                        "point": point,
                        "slave_id": device.slave_id,
                        "device_name": device.name,
                    }
                )

        all_cycle_times = [p.cycle_time_ms for d in devices for p in d.io_points]
        self.gcd_cycle_time_ms = (
            calculate_gcd_of_cycle_times(
                [type("obj", (object,), {"cycle_time_ms": ct})() for ct in all_cycle_times]
            )
            if all_cycle_times
            else 1000
        )

        device_names = ", ".join([d.name for d in devices])
        self.logger.info(
            f"[{self.name}] bus handler created for devices: {device_names} "
            f"({len(self.all_io_points)} total IO points, GCD cycle: {self.gcd_cycle_time_ms}ms)"
        )

    def _ensure_connection(self) -> bool:
        """Ensures there is a valid connection, reconnecting if necessary."""
        return self.connection_manager.ensure_connection(self._stop_event)

    def _give_up(self, pending_reads: List[ReadResult]) -> None:
        """Zero-fill the set-to-zero read points and log that the endpoint was given up."""
        give_up_on_endpoint(
            self.sba,
            self.logger,
            self.name,
            self.connection_manager,
            self.devices,
            pending_reads,
        )

    def _on_read_failure(
        self, point: ModbusIoPointConfig, failure: object, read_results: List[ReadResult]
    ) -> None:
        """Apply the reconnect policy to a failed read and queue its zero-fill unless given up."""
        self.connection_manager.handle_request_failure(failure)
        if not self.connection_manager.given_up:
            queue_zero_fill_on_failure(point, read_results)

    def run(self):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self.logger.info(f"[{self.name}] Thread started for {len(self.devices)} device(s).")

        gcd_cycle_time_seconds = self.gcd_cycle_time_ms / 1000.0

        if not self.all_io_points:
            self.logger.warn(f"[{self.name}] No I/O points defined. Stopping thread.")
            return

        if not self.connection_manager.connect(self._stop_event):
            if self.connection_manager.given_up:
                self._give_up([])
            else:
                self.logger.info(
                    f"[{self.name}] Thread stopped before connection could be established."
                )
            return

        cycle_counter = 0

        try:
            while not self._stop_event.is_set():
                cycle_start_time = time.monotonic()

                if not self._ensure_connection():
                    if self.connection_manager.given_up:
                        self._give_up([])
                    break

                read_results_to_update = []

                for io_entry in self.all_io_points:
                    if self._stop_event.is_set() or self.connection_manager.given_up:
                        break

                    point = io_entry["point"]
                    slave_id = io_entry["slave_id"]

                    if point.fc not in [1, 2, 3, 4]:
                        continue

                    point_cycle_multiple = point.cycle_time_ms // self.gcd_cycle_time_ms
                    if (cycle_counter % point_cycle_multiple) != 0:
                        continue

                    try:
                        address = parse_modbus_offset(point.offset)

                        count = get_read_count_for_io_point(point)

                        if point.fc == 1:
                            response = self.connection_manager.client.read_coils(
                                address, count=count, device_id=slave_id
                            )
                        elif point.fc == 2:
                            response = self.connection_manager.client.read_discrete_inputs(
                                address, count=count, device_id=slave_id
                            )
                        elif point.fc == 3:
                            response = self.connection_manager.client.read_holding_registers(
                                address, count=count, device_id=slave_id
                            )
                        elif point.fc == 4:
                            response = self.connection_manager.client.read_input_registers(
                                address, count=count, device_id=slave_id
                            )
                        else:
                            self.logger.warn(f"[{self.name}] Unsupported read FC: {point.fc}")
                            continue

                        if isinstance(response, (ModbusIOException, ExceptionResponse)):
                            self.logger.error(
                                f"[{self.name}] Modbus read error "
                                f"(slave {slave_id}, FC {point.fc}, addr {address}): {response}"
                            )
                            self._on_read_failure(point, response, read_results_to_update)
                            continue
                        if response.isError():
                            self.logger.error(
                                f"[{self.name}] Modbus read failed "
                                f"(slave {slave_id}, FC {point.fc}, addr {address}): {response}"
                            )
                            self._on_read_failure(point, response, read_results_to_update)
                            continue

                        if point.fc in [1, 2]:
                            modbus_data = response.bits
                        else:
                            modbus_data = response.registers

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
                        self._on_read_failure(point, ce, read_results_to_update)
                    except Exception as e:
                        self.logger.error(
                            f"[{self.name}] Error reading "
                            f"slave {slave_id}, FC {point.fc}, offset {point.offset}: {e}"
                        )
                        self._on_read_failure(point, e, read_results_to_update)

                if self.connection_manager.given_up:
                    self._give_up(read_results_to_update)
                    break

                write_read_results(self.sba, self.logger, self.name, read_results_to_update)

                write_points_due = []
                for io_entry in self.all_io_points:
                    if self._stop_event.is_set():
                        break

                    point = io_entry["point"]
                    slave_id = io_entry["slave_id"]

                    if point.fc not in [5, 6, 15, 16]:
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

                for point, address, slave_id, raw_values, details, iec_size in raw_iec_data:
                    if self._stop_event.is_set() or self.connection_manager.given_up:
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

                        if point.fc == 5:
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
                        elif point.fc == 6:
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
                        elif point.fc == 15:
                            response = self.connection_manager.client.write_coils(
                                address, values_to_write, device_id=slave_id
                            )
                        elif point.fc == 16:
                            response = self.connection_manager.client.write_registers(
                                address, values_to_write, device_id=slave_id
                            )
                        else:
                            self.logger.warn(f"[{self.name}] Unsupported write FC: {point.fc}")
                            continue

                        if isinstance(response, (ModbusIOException, ExceptionResponse)):
                            self.logger.error(
                                f"[{self.name}] Modbus write error "
                                f"(slave {slave_id}, FC {point.fc}, addr {address}): {response}"
                            )
                            self.connection_manager.handle_request_failure(response)
                        elif response.isError():
                            self.logger.error(
                                f"[{self.name}] Modbus write failed "
                                f"(slave {slave_id}, FC {point.fc}, addr {address}): {response}"
                            )
                            self.connection_manager.handle_request_failure(response)

                    except ConnectionException as ce:
                        self.logger.error(
                            f"[{self.name}] Connection error writing "
                            f"slave {slave_id}, FC {point.fc}, offset {point.offset}: {ce}"
                        )
                        self.connection_manager.handle_request_failure(ce)
                    except Exception as e:
                        self.logger.error(
                            f"[{self.name}] Error writing "
                            f"slave {slave_id}, FC {point.fc}, offset {point.offset}: {e}"
                        )
                        self.connection_manager.handle_request_failure(e)

                if self.connection_manager.given_up:
                    self._give_up([])
                    break

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

# Alias of ModbusBusHandler for RTU buses.
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
    The devices of one endpoint share one reconnect setting, which
    ModbusMasterConfig.validate() enforces.

    Args:
        devices: List of ModbusDeviceConfig with transport="tcp"

    Returns:
        Dictionary keyed by "host:port":
        {
            "host:port": {
                "config": {host, port, timeout_ms, reconnect},
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
                    "reconnect": device.reconnect,
                },
                "devices": [],
            }

        endpoints[endpoint_key]["devices"].append(device)

        if device.timeout_ms < endpoints[endpoint_key]["config"]["timeout_ms"]:
            endpoints[endpoint_key]["config"]["timeout_ms"] = device.timeout_ms

    return endpoints

def init(args_capsule):
    """
    Initialize the Modbus Master plugin.
    This function is called once when the plugin is loaded.
    """
    global runtime_args, logger

    logger = PluginLogger("MODBUS_MASTER", None)
    logger.info("Initializing...")

    try:
        runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
        if not runtime_args:
            logger.error(f"Failed to extract runtime args: {error_msg}")
            return False

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

        safe_buffer_accessor = SafeBufferAccess(runtime_args)
        if not safe_buffer_accessor.is_valid:
            logger.error(f"Failed to create SafeBufferAccess: {safe_buffer_accessor.error_msg}")
            return False

        logger.info("SafeBufferAccess created successfully")

        config_path, config_error = safe_buffer_accessor.get_config_path()
        if not config_path:
            logger.error(f"Failed to get config path: {config_error}")
            return False

        logger.debug(f"Loading configuration from: {config_path}")

        modbus_master_config = ModbusMasterConfig()
        try:
            modbus_master_config.import_config_from_file(config_path)
            modbus_master_config.validate()
        except ValueError as e:
            logger.error(f"Configuration refused, no device thread started: {e}")
            return False

        device_count = len(modbus_master_config.devices)
        logger.info(f"Configuration loaded successfully: {device_count} device(s)")

        tcp_devices = [d for d in modbus_master_config.devices if d.transport == "tcp"]
        rtu_devices = [d for d in modbus_master_config.devices if d.transport == "rtu"]

        logger.info(f"Found {len(tcp_devices)} TCP device(s) and {len(rtu_devices)} RTU device(s)")

        started_threads = 0

        if tcp_devices:
            tcp_endpoints = group_tcp_devices_by_endpoint(tcp_devices)
            logger.info(f"TCP devices grouped into {len(tcp_endpoints)} endpoint(s)")

            for endpoint_key, endpoint_info in tcp_endpoints.items():
                endpoint_devices = endpoint_info["devices"]
                try:
                    if len(endpoint_devices) == 1:
                        device_config = endpoint_devices[0]
                        device_thread = ModbusSlaveDevice(
                            device_config, safe_buffer_accessor, logger
                        )
                        device_thread.start()
                        slave_threads.append(device_thread)
                        started_threads += 1
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
                        started_threads += 1
                        device_names = ", ".join([d.name for d in endpoint_devices])
                        slave_ids = ", ".join([str(d.slave_id) for d in endpoint_devices])
                        logger.info(
                            f"Started TCP gateway thread: {endpoint_key} "
                            f"(devices: {device_names}, slave_ids: {slave_ids})"
                        )
                except Exception as e:
                    logger.error(f"Failed to start TCP thread(s) for endpoint {endpoint_key}: {e}")

        if rtu_devices:
            rtu_buses = group_rtu_devices_by_bus(rtu_devices)
            logger.info(f"RTU devices grouped into {len(rtu_buses)} serial bus(es)")

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
                    started_threads += 1

                    device_names = ", ".join([d.name for d in bus_info["devices"]])
                    slave_ids = ", ".join([str(d.slave_id) for d in bus_info["devices"]])
                    logger.info(
                        f"Started RTU bus thread: {bus_info['config']['serial_port']} "
                        f"(devices: {device_names}, slave_ids: {slave_ids})"
                    )
                except Exception as e:
                    logger.error(f"Failed to start RTU bus thread for {bus_key}: {e}")

        if started_threads > 0:
            logger.info(f"Successfully started {started_threads} device/bus thread(s)")
            return True
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
    Threads that stopped leave `slave_threads`; a thread still alive after the timeout
    stays, and the next `stop_loop()` joins it again.

    Returns:
        True if all threads stopped, False if any thread survived the timeout.
    """
    global slave_threads, logger  # pylint: disable=global-variable-not-assigned

    logger.info("Stopping main loop...")

    try:
        if not slave_threads:
            logger.info("No threads to stop")
            return True

        for thread in slave_threads:
            try:
                if hasattr(thread, "stop"):
                    thread.stop()
                else:
                    logger.warn(f"Thread {thread.name} does not have a stop method")
            except Exception as e:
                logger.error(f"Error stopping thread {thread.name}: {e}")

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

        slave_threads[:] = [thread for thread in slave_threads if thread.is_alive()]
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
        stop_loop()

        slave_threads.clear()

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
    print(" Modbus Master Plugin - Test Mode")
    print("This plugin is designed to be loaded by the OpenPLC runtime.")
    print("Standalone testing is not fully supported without runtime integration.")

    try:
        test_config = ModbusMasterConfig()
        print("(PASS) Configuration model can be instantiated")
    except Exception as e:
        print(f"(FAIL) Error testing configuration model: {e}")
