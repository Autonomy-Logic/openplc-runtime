import sys
import os
import json
import traceback
import re
import threading
import time
from typing import Optional, Literal, List, Dict, Any

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException, ConnectionException
from pymodbus.pdu import ExceptionResponse

def get_batch_read_requests_from_io_points(io_points: List[Any]) -> Dict[int, List[Any]]:
    """
    Groups I/O points by Modbus read function code (1,2,3,4) and creates
    batch read lists to optimize Modbus operations.
    Returns a dictionary mapping FC to lists of points.
    """
    read_requests: Dict[int, List[Any]] = {}
    for point in io_points:
        fc = point.fc
        if fc in [1, 2, 3, 4]:  # Read functions
            if fc not in read_requests:
                read_requests[fc] = []
            read_requests[fc].append(point)
    return read_requests

def get_batch_write_requests_from_io_points(io_points: List[Any]) -> Dict[int, List[Any]]:
    """
    Groups I/O points by Modbus write function code (5,6,15,16) and creates
    batch write lists to optimize Modbus operations.
    Returns a dictionary mapping FC to lists of points.
    """
    write_requests: Dict[int, List[Any]] = {}
    for point in io_points:
        fc = point.fc
        if fc in [5, 6, 15, 16]:  # Write functions
            if fc not in write_requests:
                write_requests[fc] = []
            write_requests[fc].append(point)
    return write_requests

# Add the parent directory to Python path to find shared module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import the correct type definitions
from shared.python_plugin_types import (
    PluginRuntimeArgs, 
    safe_extract_runtime_args_from_capsule,
    SafeBufferAccess,
    PluginStructureValidator
)

# Import the configuration model
from shared.plugin_config_decode.modbus_master_config_model import ModbusMasterConfig

# Global variables for plugin lifecycle and configuration
runtime_args = None
modbus_master_config: ModbusMasterConfig = None
safe_buffer_accessor: SafeBufferAccess = None
slave_threads: List[threading.Thread] = []

class ModbusSlaveDevice(threading.Thread):
    def __init__(self, device_config: Any, sba: SafeBufferAccess):
        super().__init__(daemon=True)
        self.device_config = device_config
        self.sba = sba
        self._stop_event = threading.Event()
        self.client: Optional[ModbusTcpClient] = None
        self.name = f"ModbusSlave-{device_config.name}-{device_config.host}:{device_config.port}"
        
        # Retry configuration - simple system
        self.retry_delay_base = 2.0      # initial delay between attempts (seconds)
        self.retry_delay_max = 30.0      # maximum delay between attempts (seconds)
        self.retry_delay_current = self.retry_delay_base
        self.is_connected = False

    def _get_sba_access_details(self, iec_addr, is_write_op: bool = False) -> Optional[Dict[str, Any]]:
        """
        Maps IECAddress to SafeBufferAccess method parameters.
        
        Args:
            iec_addr: IECAddress object
            is_write_op: True if this is for a write operation (affects input/output buffer selection)
        
        Returns:
            Dictionary with buffer access details or None if mapping fails
        """
        try:
            area = iec_addr.area
            size = iec_addr.size
            
            # Determine if this is a boolean operation
            is_boolean = (size == "X")
            
            # Calculate buffer_idx based on size and index_bytes
            if size == "X":  # Boolean - 1 bit
                buffer_idx = iec_addr.index_bytes
                bit_idx = iec_addr.bit
                element_size_bytes = 1  # Bit operations work on byte boundaries
            elif size == "B":  # Byte - 8 bits
                buffer_idx = iec_addr.index_bytes
                bit_idx = None
                element_size_bytes = 1
            elif size == "W":  # Word - 16 bits
                buffer_idx = iec_addr.index_bytes // 2
                bit_idx = None
                element_size_bytes = 2
            elif size == "D":  # Double word - 32 bits
                buffer_idx = iec_addr.index_bytes // 4
                bit_idx = None
                element_size_bytes = 4
            elif size == "L":  # Long word - 64 bits
                buffer_idx = iec_addr.index_bytes // 8
                bit_idx = None
                element_size_bytes = 8
            else:
                print(f"[{self.name}] ⚠ Unsupported IEC size: {size}")
                return None
            
            # Determine buffer type string based on area, size, and operation direction
            if is_boolean:  # Size == "X"
                if area == "I":
                    buffer_type_str = "bool_input"
                elif area == "Q":
                    buffer_type_str = "bool_output" if is_write_op else "bool_output"
                elif area == "M":
                    print(f"[{self.name}] ⚠ Memory area 'M' not supported for boolean operations")
                    return None
                else:
                    print(f"[{self.name}] ⚠ Unknown area for boolean: {area}")
                    return None
            else:  # Non-boolean (B, W, D, L)
                if area == "M":  # Memory area
                    if size == "B":
                        buffer_type_str = "byte_memory"  # Memory area uses memory buffer types
                    elif size == "W":
                        buffer_type_str = "int_memory"
                    elif size == "D":
                        buffer_type_str = "dint_memory"
                    elif size == "L":
                        buffer_type_str = "lint_memory"
                    else:
                        print(f"[{self.name}] ⚠ Unsupported memory size: {size}")
                        return None
                elif area == "I":  # Input area
                    if size == "B":
                        buffer_type_str = "byte_input"
                    elif size == "W":
                        buffer_type_str = "int_input"
                    elif size == "D":
                        buffer_type_str = "dint_input"
                    elif size == "L":
                        buffer_type_str = "lint_input"
                    else:
                        print(f"[{self.name}] ⚠ Unsupported input size: {size}")
                        return None
                elif area == "Q":  # Output area
                    if size == "B":
                        buffer_type_str = "byte_output"
                    elif size == "W":
                        buffer_type_str = "int_output"
                    elif size == "D":
                        buffer_type_str = "dint_output"
                    elif size == "L":
                        buffer_type_str = "lint_output"
                    else:
                        print(f"[{self.name}] ⚠ Unsupported output size: {size}")
                        return None
                else:
                    print(f"[{self.name}] ⚠ Unknown area: {area}")
                    return None
            
            return {
                "buffer_type_str": buffer_type_str,
                "buffer_idx": buffer_idx,
                "bit_idx": bit_idx,
                "element_size_bytes": element_size_bytes,
                "is_boolean": is_boolean
            }
            
        except Exception as e:
            print(f"[{self.name}] ✗ Error in _get_sba_access_details: {e}")
            return None

    def _connect_with_retry(self) -> bool:
        """
        Attempts to connect to Modbus device with infinite retry.
        
        Returns:
            True if connected successfully, False if thread was interrupted
        """
        host = self.device_config.host
        port = self.device_config.port
        timeout = self.device_config.timeout_ms / 1000.0
        
        retry_count = 0
        
        while not self._stop_event.is_set():
            try:
                # Create new client if necessary
                if self.client is None or not self.client.connected:
                    if self.client:
                        try:
                            self.client.close()
                        except:
                            pass
                    self.client = ModbusTcpClient(host=host, port=port, timeout=timeout)
                
                # Attempt to connect
                if self.client.connect():
                    print(f"[{self.name}] ✓ Connected to {host}:{port} (attempt {retry_count + 1})")
                    self.is_connected = True
                    self.retry_delay_current = self.retry_delay_base  # Reset delay
                    return True
                
            except Exception as e:
                print(f"[{self.name}] ✗ Connection attempt {retry_count + 1} failed: {e}")
            
            # Increment counter and calculate delay
            retry_count += 1
            
            # Attempt logging
            if retry_count == 1:
                print(f"[{self.name}] ⚠ Failed to connect to {host}:{port}, starting retry attempts...")
            elif retry_count % 10 == 0:  # Log every 10 attempts
                print(f"[{self.name}] ⚠ Connection attempt {retry_count} failed, continuing retries...")
            
            # Wait with increasing delay (limited exponential backoff)
            delay = min(self.retry_delay_current, self.retry_delay_max)
            
            # Sleep in small increments to allow quick stop
            sleep_increments = int(delay * 10)  # 0.1s increments
            for _ in range(sleep_increments):
                if self._stop_event.is_set():
                    return False
                time.sleep(0.1)
            
            # Increase delay for next attempt (maximum of retry_delay_max)
            self.retry_delay_current = min(self.retry_delay_current * 1.5, self.retry_delay_max)
        
        return False

    def _ensure_connection(self) -> bool:
        """
        Ensures there is a valid connection, reconnecting if necessary.
        
        Returns:
            True if connection is available, False if thread was interrupted
        """
        # Check if already connected
        if self.client and self.client.connected:
            return True
        
        # Mark as disconnected
        self.is_connected = False
        
        # Try to reconnect
        return self._connect_with_retry()

    def _update_iec_buffer_from_modbus_data(self, iec_addr, modbus_data: list, length: int):
        """
        Updates IEC buffers with data read from Modbus.
        Assumes mutex is already acquired.
        
        Args:
            iec_addr: IECAddress object
            modbus_data: List of values from Modbus (booleans for coils/inputs, integers for registers)
            length: Number of elements to write
        """
        try:
            details = self._get_sba_access_details(iec_addr, is_write_op=True)
            if not details:
                print(f"[{self.name}] ✗ Failed to get SBA access details for {iec_addr}")
                return
            
            buffer_type = details["buffer_type_str"]
            base_buffer_idx = details["buffer_idx"]
            base_bit_idx = details["bit_idx"]
            is_boolean = details["is_boolean"]
            
            # Write data elements to consecutive buffer locations
            for i in range(min(length, len(modbus_data))):
                current_data = modbus_data[i]
                
                if is_boolean:
                    # For boolean operations, handle bit indexing
                    if base_bit_idx is not None:
                        # Calculate the actual bit position for this element
                        current_bit_idx = base_bit_idx + i
                        current_buffer_idx = base_buffer_idx + (current_bit_idx // 8)
                        actual_bit_idx = current_bit_idx % 8
                    else:
                        current_buffer_idx = base_buffer_idx
                        actual_bit_idx = i
                    
                    # Write boolean value
                    if buffer_type == "bool_input":
                        success, msg = self.sba.write_bool_input(current_buffer_idx, actual_bit_idx, 
                                                                current_data, thread_safe=False)
                    elif buffer_type == "bool_output":
                        success, msg = self.sba.write_bool_output(current_buffer_idx, actual_bit_idx, 
                                                                current_data, thread_safe=False)
                    else:
                        print(f"[{self.name}] ⚠ Unexpected boolean buffer type: {buffer_type}")
                        continue
                    
                    if not success:
                        print(f"[{self.name}] ✗ Failed to write boolean at buffer {current_buffer_idx}, bit {actual_bit_idx}: {msg}")
                
                else:
                    # For non-boolean operations
                    current_buffer_idx = base_buffer_idx + i
                    
                    # Write the value using the appropriate method
                    if buffer_type == "byte_input":
                        success, msg = self.sba.write_byte_input(current_buffer_idx, current_data, thread_safe=False)
                    elif buffer_type == "byte_output":
                        success, msg = self.sba.write_byte_output(current_buffer_idx, current_data, thread_safe=False)
                    elif buffer_type == "int_input":
                        success, msg = self.sba.write_int_input(current_buffer_idx, current_data, thread_safe=False)
                    elif buffer_type == "int_output":
                        success, msg = self.sba.write_int_output(current_buffer_idx, current_data, thread_safe=False)
                    elif buffer_type == "int_memory":
                        success, msg = self.sba.write_int_memory(current_buffer_idx, current_data, thread_safe=False)
                    elif buffer_type == "dint_input":
                        success, msg = self.sba.write_dint_input(current_buffer_idx, current_data, thread_safe=False)
                    elif buffer_type == "dint_output":
                        success, msg = self.sba.write_dint_output(current_buffer_idx, current_data, thread_safe=False)
                    elif buffer_type == "dint_memory":
                        success, msg = self.sba.write_dint_memory(current_buffer_idx, current_data, thread_safe=False)
                    elif buffer_type == "lint_input":
                        success, msg = self.sba.write_lint_input(current_buffer_idx, current_data, thread_safe=False)
                    elif buffer_type == "lint_output":
                        success, msg = self.sba.write_lint_output(current_buffer_idx, current_data, thread_safe=False)
                    elif buffer_type == "lint_memory":
                        success, msg = self.sba.write_lint_memory(current_buffer_idx, current_data, thread_safe=False)
                    else:
                        print(f"[{self.name}] ⚠ Unknown buffer type: {buffer_type}")
                        continue
                    
                    if not success:
                        print(f"[{self.name}] ✗ Failed to write {buffer_type} at index {current_buffer_idx}: {msg}")
                        
        except Exception as e:
            print(f"[{self.name}] ✗ Error updating IEC buffer: {e}")

    def _read_data_for_modbus_write(self, iec_addr, length: int) -> Optional[list]:
        """
        Reads data from IEC buffers for Modbus write operations.
        Assumes mutex is already acquired.
        
        Args:
            iec_addr: IECAddress object
            length: Number of elements to read
        
        Returns:
            List of values ready for Modbus write or None if failed
        """
        try:
            details = self._get_sba_access_details(iec_addr, is_write_op=False)
            if not details:
                print(f"[{self.name}] ✗ Failed to get SBA access details for {iec_addr}")
                return None
            
            buffer_type = details["buffer_type_str"]
            base_buffer_idx = details["buffer_idx"]
            base_bit_idx = details["bit_idx"]
            is_boolean = details["is_boolean"]
            
            values = []
            
            # Read data elements from consecutive buffer locations
            for i in range(length):
                if is_boolean:
                    # For boolean operations, handle bit indexing
                    if base_bit_idx is not None:
                        current_bit_idx = base_bit_idx + i
                        current_buffer_idx = base_buffer_idx + (current_bit_idx // 8)
                        actual_bit_idx = current_bit_idx % 8
                    else:
                        current_buffer_idx = base_buffer_idx
                        actual_bit_idx = i
                    
                    # Read boolean value
                    if buffer_type == "bool_input":
                        value, msg = self.sba.read_bool_input(current_buffer_idx, actual_bit_idx, thread_safe=False)
                    elif buffer_type == "bool_output":
                        value, msg = self.sba.read_bool_output(current_buffer_idx, actual_bit_idx, thread_safe=False)
                    else:
                        print(f"[{self.name}] ⚠ Unexpected boolean buffer type: {buffer_type}")
                        return None
                    
                    if msg != "Success":
                        print(f"[{self.name}] ✗ Failed to read boolean at buffer {current_buffer_idx}, bit {actual_bit_idx}: {msg}")
                        return None
                    
                    values.append(value)
                
                else:
                    # For non-boolean operations
                    current_buffer_idx = base_buffer_idx + i
                    
                    # Read the value using the appropriate method
                    if buffer_type == "byte_input":
                        value, msg = self.sba.read_byte_input(current_buffer_idx, thread_safe=False)
                    elif buffer_type == "byte_output":
                        value, msg = self.sba.read_byte_output(current_buffer_idx, thread_safe=False)
                    elif buffer_type == "int_input":
                        value, msg = self.sba.read_int_input(current_buffer_idx, thread_safe=False)
                    elif buffer_type == "int_output":
                        value, msg = self.sba.read_int_output(current_buffer_idx, thread_safe=False)
                    elif buffer_type == "int_memory":
                        value, msg = self.sba.read_int_memory(current_buffer_idx, thread_safe=False)
                    elif buffer_type == "dint_input":
                        value, msg = self.sba.read_dint_input(current_buffer_idx, thread_safe=False)
                    elif buffer_type == "dint_output":
                        value, msg = self.sba.read_dint_output(current_buffer_idx, thread_safe=False)
                    elif buffer_type == "dint_memory":
                        value, msg = self.sba.read_dint_memory(current_buffer_idx, thread_safe=False)
                    elif buffer_type == "lint_input":
                        value, msg = self.sba.read_lint_input(current_buffer_idx, thread_safe=False)
                    elif buffer_type == "lint_output":
                        value, msg = self.sba.read_lint_output(current_buffer_idx, thread_safe=False)
                    elif buffer_type == "lint_memory":
                        value, msg = self.sba.read_lint_memory(current_buffer_idx, thread_safe=False)
                    else:
                        print(f"[{self.name}] ⚠ Unknown buffer type: {buffer_type}")
                        return None
                    
                    if msg != "Success":
                        print(f"[{self.name}] ✗ Failed to read {buffer_type} at index {current_buffer_idx}: {msg}")
                        return None
                    
                    values.append(value)
            
            return values
                        
        except Exception as e:
            print(f"[{self.name}] ✗ Error reading data for Modbus write: {e}")
            return None

    def run(self):
        print(f"[{self.name}] Thread started.")
        
        cycle_time = self.device_config.cycle_time_ms / 1000.0
        io_points = self.device_config.io_points

        if not io_points:
            print(f"[{self.name}] No I/O points defined. Stopping thread.")
            return

        # Connect with infinite retry
        if not self._connect_with_retry():
            print(f"[{self.name}] Thread stopped before connection could be established.")
            return

        try:
            while not self._stop_event.is_set():
                cycle_start_time = time.monotonic()
                
                # Ensure connection exists before cycle
                if not self._ensure_connection():
                    break  # Thread was interrupted

                # 1. READ OPERATIONS - Collect all read requests and store results
                read_requests = get_batch_read_requests_from_io_points(io_points)
                read_results_to_update = []  # Store tuples: (iec_addr, modbus_data, length)
                
                # Perform all Modbus read operations first
                for fc, points in read_requests.items():
                    if self._stop_event.is_set():
                        break

                    for point in points:
                        if self._stop_event.is_set():
                            break

                        try:
                            # Convert offset string to integer
                            if not isinstance(point.offset, str) or not point.offset.strip():
                                raise ValueError(f"Offset must be a non-empty string, got: {point.offset!r} (type: {type(point.offset)})")
                            
                            # Try to convert to integer, handling decimal and hexadecimal formats
                            offset_str = point.offset.strip()
                            try:
                                # Support both decimal (123) and hexadecimal (0x1234, 0X1234) formats
                                if offset_str.lower().startswith('0x'):
                                    address = int(offset_str, 16)  # Hexadecimal
                                else:
                                    address = int(offset_str, 10)  # Decimal
                            except ValueError as conv_err:
                                raise ValueError(f"Cannot convert offset '{offset_str}' to integer (supports decimal or 0x hex): {conv_err}")
                            
                            if address < 0:
                                raise ValueError(f"Offset must be non-negative, got: {address}")
                            
                            count = point.length
                            
                            # Perform Modbus read based on function code
                            if fc == 1:  # Read Coils
                                response = self.client.read_coils(address, count)
                            elif fc == 2:  # Read Discrete Inputs
                                response = self.client.read_discrete_inputs(address, count)
                            elif fc == 3:  # Read Holding Registers
                                response = self.client.read_holding_registers(address, count)
                            elif fc == 4:  # Read Input Registers
                                response = self.client.read_input_registers(address, count)
                            else:
                                print(f"[{self.name}] Unsupported read FC: {fc}")
                                continue
                            
                            # Check if response is valid
                            if isinstance(response, (ModbusIOException, ExceptionResponse)):
                                print(f"[{self.name}] ✗ Modbus read error (FC {fc}, addr {address}): {response}")
                                # Mark as disconnected to force reconnection on next cycle
                                self.is_connected = False
                                continue
                            elif response.isError():
                                print(f"[{self.name}] ✗ Modbus read failed (FC {fc}, addr {address}): {response}")
                                # Mark as disconnected to force reconnection on next cycle
                                self.is_connected = False
                                continue
                            
                            # Extract data from response
                            if fc in [1, 2]:  # Coils/Discrete Inputs (boolean data)
                                modbus_data = response.bits
                            else:  # Holding/Input Registers (integer data)
                                modbus_data = response.registers
                            
                            # Store for batch update
                            read_results_to_update.append((point.iec_location, modbus_data, point.length))
                            
                        except ValueError as ve:
                            print(f"[{self.name}] ✗ Invalid offset '{point.offset}' for FC {fc}: {ve}")
                        except ConnectionException as ce:
                            print(f"[{self.name}] ✗ Connection error reading FC {fc}, offset {point.offset}: {ce}")
                            # Mark as disconnected to force reconnection
                            self.is_connected = False
                        except Exception as e:
                            print(f"[{self.name}] ✗ Error reading FC {fc}, offset {point.offset}: {e}")
                            # For other errors also mark disconnected as precaution
                            self.is_connected = False

                # Batch update IEC buffers with single mutex acquisition
                if read_results_to_update:
                    lock_acquired, lock_msg = self.sba.acquire_mutex()
                    if lock_acquired:
                        try:
                            for iec_addr, modbus_data, length in read_results_to_update:
                                self._update_iec_buffer_from_modbus_data(iec_addr, modbus_data, length)
                        finally:
                            self.sba.release_mutex()
                    else:
                        print(f"[{self.name}] ✗ Failed to acquire mutex for read updates: {lock_msg}")

                # 2. WRITE OPERATIONS - Read from IEC buffers and perform Modbus writes
                write_requests = get_batch_write_requests_from_io_points(io_points)
                
                for fc, points in write_requests.items():
                    if self._stop_event.is_set():
                        break

                    for point in points:
                        if self._stop_event.is_set():
                            break

                        try:
                            # Convert offset string to integer
                            if not isinstance(point.offset, str) or not point.offset.strip():
                                raise ValueError(f"Offset must be a non-empty string, got: {point.offset!r} (type: {type(point.offset)})")
                            
                            # Try to convert to integer, handling decimal and hexadecimal formats
                            offset_str = point.offset.strip()
                            try:
                                # Support both decimal (123) and hexadecimal (0x1234, 0X1234) formats
                                if offset_str.lower().startswith('0x'):
                                    address = int(offset_str, 16)  # Hexadecimal
                                else:
                                    address = int(offset_str, 10)  # Decimal
                            except ValueError as conv_err:
                                raise ValueError(f"Cannot convert offset '{offset_str}' to integer (supports decimal or 0x hex): {conv_err}")
                            
                            if address < 0:
                                raise ValueError(f"Offset must be non-negative, got: {address}")
                            
                            # Read data from IEC buffers (with mutex)
                            lock_acquired, lock_msg = self.sba.acquire_mutex()
                            if not lock_acquired:
                                print(f"[{self.name}] ✗ Failed to acquire mutex for write prep (FC {fc}, offset {point.offset}): {lock_msg}")
                                continue
                                
                            try:
                                values_to_write = self._read_data_for_modbus_write(point.iec_location, point.length)
                            finally:
                                self.sba.release_mutex()
                            
                            if values_to_write is None:
                                print(f"[{self.name}] ✗ Failed to read data for Modbus write (FC {fc}, offset {point.offset})")
                                continue
                            
                            # Perform Modbus write operation
                            if fc == 5:  # Write Single Coil
                                if len(values_to_write) > 0:
                                    response = self.client.write_coil(address, values_to_write[0])
                                else:
                                    print(f"[{self.name}] ✗ No data to write for FC 5, offset {address}")
                                    continue
                            elif fc == 6:  # Write Single Register
                                if len(values_to_write) > 0:
                                    response = self.client.write_register(address, values_to_write[0])
                                else:
                                    print(f"[{self.name}] ✗ No data to write for FC 6, offset {address}")
                                    continue
                            elif fc == 15:  # Write Multiple Coils
                                response = self.client.write_coils(address, values_to_write)
                            elif fc == 16:  # Write Multiple Registers
                                response = self.client.write_registers(address, values_to_write)
                            else:
                                print(f"[{self.name}] Unsupported write FC: {fc}")
                                continue
                            
                            # Check write response
                            if isinstance(response, (ModbusIOException, ExceptionResponse)):
                                print(f"[{self.name}] ✗ Modbus write error (FC {fc}, addr {address}): {response}")
                                # Mark as disconnected to force reconnection on next cycle
                                self.is_connected = False
                            elif response.isError():
                                print(f"[{self.name}] ✗ Modbus write failed (FC {fc}, addr {address}): {response}")
                                # Mark as disconnected to force reconnection on next cycle
                                self.is_connected = False
                            
                        except ValueError as ve:
                            print(f"[{self.name}] ✗ Invalid offset '{point.offset}' for FC {fc}: {ve}")
                        except ConnectionException as ce:
                            print(f"[{self.name}] ✗ Connection error writing FC {fc}, offset {point.offset}: {ce}")
                            # Mark as disconnected to force reconnection
                            self.is_connected = False
                        except Exception as e:
                            print(f"[{self.name}] ✗ Error writing FC {fc}, offset {point.offset}: {e}")
                            # For other errors also mark disconnected as precaution
                            self.is_connected = False
                
                # 3. CYCLE TIMING
                cycle_elapsed = time.monotonic() - cycle_start_time
                sleep_duration = max(0, cycle_time - cycle_elapsed)
                if sleep_duration > 0:
                    # Sleep in small increments (100ms each) to allow for quick shutdown
                    sleep_increment = 0.1
                    remaining_sleep = sleep_duration
                    
                    while remaining_sleep > 0 and not self._stop_event.is_set():
                        actual_sleep = min(sleep_increment, remaining_sleep)
                        time.sleep(actual_sleep)
                        remaining_sleep -= actual_sleep

        except ConnectionException as ce:
            print(f"[{self.name}] ✗ Connection failed: {ce}")
            # Try to reconnect
            self.is_connected = False
        except Exception as e:
            print(f"[{self.name}] ✗ Unexpected error in thread: {e}")
            traceback.print_exc()
        finally:
            if self.client and self.client.connected:
                self.client.close()
            print(f"[{self.name}] Thread finished and connection closed.")

    def stop(self):
        print(f"[{self.name}] Stop signal received.")
        self._stop_event.set()

def init(args_capsule):
    """
    Initialize the Modbus Master plugin.
    This function is called once when the plugin is loaded.
    """
    global runtime_args, modbus_master_config, safe_buffer_accessor
    
    print("🔧 Modbus Master Plugin - Initializing...")
    
    try:
        # Extract runtime arguments from capsule
        runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
        if not runtime_args:
            print(f"✗ Failed to extract runtime args: {error_msg}")
            return False
        
        print("✓ Runtime arguments extracted successfully")
        
        # Create safe buffer accessor
        safe_buffer_accessor = SafeBufferAccess(runtime_args)
        if not safe_buffer_accessor.is_valid:
            print(f"✗ Failed to create SafeBufferAccess: {safe_buffer_accessor.error_msg}")
            return False
        
        print("✓ SafeBufferAccess created successfully")
        
        # Load configuration
        config_path, config_error = safe_buffer_accessor.get_config_path()
        if not config_path:
            print(f"✗ Failed to get config path: {config_error}")
            return False
        
        print(f"📄 Loading configuration from: {config_path}")
        
        modbus_master_config = ModbusMasterConfig()
        modbus_master_config.import_config_from_file(config_path)
        modbus_master_config.validate()
        
        print(f"✓ Configuration loaded successfully: {len(modbus_master_config.devices)} device(s)")
        
        return True
        
    except Exception as e:
        print(f"✗ Error during initialization: {e}")
        import traceback
        traceback.print_exc()
        return False

def start_loop():
    """
    Start the main loop for all configured Modbus devices.
    This function is called after successful initialization.
    """
    global slave_threads, modbus_master_config, safe_buffer_accessor
    
    print("🚀 Modbus Master Plugin - Starting main loop...")
    
    try:
        if not modbus_master_config or not safe_buffer_accessor:
            print("✗ Plugin not properly initialized")
            return False
        
        # Start a thread for each configured device
        for device_config in modbus_master_config.devices:
            try:
                device_thread = ModbusSlaveDevice(device_config, safe_buffer_accessor)
                device_thread.start()
                slave_threads.append(device_thread)
                print(f"✓ Started thread for device: {device_config.name} ({device_config.host}:{device_config.port})")
            except Exception as e:
                print(f"✗ Failed to start thread for device {device_config.name}: {e}")
        
        if slave_threads:
            print(f"✓ Successfully started {len(slave_threads)} device thread(s)")
            return True
        else:
            print("✗ No device threads started")
            return False
            
    except Exception as e:
        print(f"✗ Error starting main loop: {e}")
        import traceback
        traceback.print_exc()
        return False

def stop_loop():
    """
    Stop the main loop and all running device threads.
    This function is called when the plugin needs to be stopped.
    """
    global slave_threads
    
    print("🛑 Modbus Master Plugin - Stopping main loop...")
    
    try:
        if not slave_threads:
            print("ℹ No threads to stop")
            return True
        
        # Signal all threads to stop
        for thread in slave_threads:
            try:
                if hasattr(thread, 'stop'):
                    thread.stop()
                else:
                    print(f"⚠ Thread {thread.name} does not have a stop method")
            except Exception as e:
                print(f"✗ Error stopping thread {thread.name}: {e}")
        
        # Wait for all threads to finish (with timeout)
        timeout_per_thread = 5.0  # seconds
        for thread in slave_threads:
            try:
                thread.join(timeout=timeout_per_thread)
                if thread.is_alive():
                    print(f"⚠ Thread {thread.name} did not stop within timeout")
                else:
                    print(f"✓ Thread {thread.name} stopped successfully")
            except Exception as e:
                print(f"✗ Error joining thread {thread.name}: {e}")
        
        print("✓ Main loop stopped")
        return True
        
    except Exception as e:
        print(f"✗ Error stopping main loop: {e}")
        import traceback
        traceback.print_exc()
        return False

def cleanup():
    """
    Clean up resources before plugin unload.
    This function is called when the plugin is being unloaded.
    """
    global runtime_args, modbus_master_config, safe_buffer_accessor, slave_threads
    
    print("🧹 Modbus Master Plugin - Cleaning up...")
    
    try:
        # Stop all threads if not already stopped
        stop_loop()
        
        # Clear thread list
        slave_threads.clear()
        
        # Reset global variables
        runtime_args = None
        modbus_master_config = None
        safe_buffer_accessor = None
        
        print("✓ Cleanup completed successfully")
        return True
        
    except Exception as e:
        print(f"✗ Error during cleanup: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    """
    Test mode for development purposes.
    This allows running the plugin standalone for testing.
    """
    print("🧪 Modbus Master Plugin - Test Mode")
    print("This plugin is designed to be loaded by the OpenPLC runtime.")
    print("Standalone testing is not fully supported without runtime integration.")
    
    # You could add basic configuration validation here
    try:
        test_config = ModbusMasterConfig()
        print("✓ Configuration model can be instantiated")
    except Exception as e:
        print(f"✗ Error testing configuration model: {e}")
