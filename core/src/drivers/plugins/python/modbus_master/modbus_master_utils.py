"""Modbus Master plugin utility functions."""

import math
from typing import List, Dict, Any


def gcd(a: int, b: int) -> int:
    """
    Calculate the Greatest Common Divisor of two numbers using Euclidean algorithm.
    """
    while b != 0:
        a, b = b, a % b
    return a


def calculate_gcd_of_cycle_times(io_points: List[Any]) -> int:
    """
    Calculate the GCD of all cycle_time_ms values from I/O points.
    If no points have cycle_time_ms, return 1000 (1 second default).
    """
    cycle_times = []
    for point in io_points:
        if hasattr(point, 'cycle_time_ms') and point.cycle_time_ms > 0:
            cycle_times.append(point.cycle_time_ms)

    if not cycle_times:
        return 1000  # Default 1 second

    # Calculate GCD of all cycle times
    result = cycle_times[0]
    for time_ms in cycle_times[1:]:
        result = gcd(result, time_ms)

    return result


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


def get_modbus_registers_count_for_iec_size(iec_size: str) -> int:
    """
    Returns how many 16-bit Modbus registers are needed for an IEC data type.

    Args:
        iec_size: IEC data size ('X', 'B', 'W', 'D', 'L')

    Returns:
        Number of 16-bit registers needed
    """
    if iec_size == "X":  # 1 bit - handled separately (coils/discrete inputs)
        return 0  # Not applicable for registers
    elif iec_size == "B":  # 8 bits - fits in 1 register (with some unused bits)
        return 1
    elif iec_size == "W":  # 16 bits - exactly 1 register
        return 1
    elif iec_size == "D":  # 32 bits - needs 2 registers
        return 2
    elif iec_size == "L":  # 64 bits - needs 4 registers
        return 4
    else:
        return 1  # Default fallback


def convert_modbus_registers_to_iec_value(registers: List[int], iec_size: str, use_big_endian: bool = False):
    """
    Converts Modbus register values to IEC data type value.

    Args:
        registers: List of 16-bit register values from Modbus
        iec_size: IEC data size ('B', 'W', 'D', 'L')
        use_big_endian: If True, use big-endian byte order, else little-endian

    Returns:
        Converted value ready for IEC buffer
    """
    if iec_size == "B":  # 8 bits
        # Take lower 8 bits of first register
        return registers[0] & 0xFF
    elif iec_size == "W":  # 16 bits
        # Single register, no conversion needed
        return registers[0] & 0xFFFF
    elif iec_size == "D":  # 32 bits
        # Combine 2 registers into 32-bit value
        if len(registers) < 2:
            raise ValueError("Need at least 2 registers for D (32-bit) type")
        if use_big_endian:
            return (registers[0] << 16) | registers[1]
        else:  # little-endian
            return (registers[1] << 16) | registers[0]
    elif iec_size == "L":  # 64 bits
        # Combine 4 registers into 64-bit value
        if len(registers) < 4:
            raise ValueError("Need at least 4 registers for L (64-bit) type")
        if use_big_endian:
            return (registers[0] << 48) | (registers[1] << 32) | (registers[2] << 16) | registers[3]
        else:  # little-endian
            return (registers[3] << 48) | (registers[2] << 32) | (registers[1] << 16) | registers[0]
    else:
        raise ValueError(f"Unsupported IEC size for register conversion: {iec_size}")


def convert_iec_value_to_modbus_registers(value: int, iec_size: str, use_big_endian: bool = False) -> List[int]:
    """
    Converts IEC data type value to Modbus register values.

    Args:
        value: IEC value to convert
        iec_size: IEC data size ('B', 'W', 'D', 'L')
        use_big_endian: If True, use big-endian byte order, else little-endian

    Returns:
        List of 16-bit register values for Modbus
    """
    if iec_size == "B":  # 8 bits
        # Put 8-bit value in lower part of register, upper part is 0
        return [value & 0xFF]
    elif iec_size == "W":  # 16 bits
        # Single register
        return [value & 0xFFFF]
    elif iec_size == "D":  # 32 bits
        # Split into 2 registers
        if use_big_endian:
            return [(value >> 16) & 0xFFFF, value & 0xFFFF]
        else:  # little-endian
            return [value & 0xFFFF, (value >> 16) & 0xFFFF]
    elif iec_size == "L":  # 64 bits
        # Split into 4 registers
        if use_big_endian:
            return [
                (value >> 48) & 0xFFFF,
                (value >> 32) & 0xFFFF,
                (value >> 16) & 0xFFFF,
                value & 0xFFFF
            ]
        else:  # little-endian
            return [
                value & 0xFFFF,
                (value >> 16) & 0xFFFF,
                (value >> 32) & 0xFFFF,
                (value >> 48) & 0xFFFF
            ]
    else:
        raise ValueError(f"Unsupported IEC size for register conversion: {iec_size}")


def parse_modbus_offset(offset_str: str) -> int:
    """
    Parse Modbus offset string supporting decimal and hexadecimal formats.

    Args:
        offset_str: Offset string (e.g., "123", "0x1234", "0X1234")

    Returns:
        Parsed integer offset

    Raises:
        ValueError: If offset cannot be parsed or is negative
    """
    if not isinstance(offset_str, str) or not offset_str.strip():
        raise ValueError(f"Offset must be a non-empty string, got: {offset_str!r}")

    offset_str = offset_str.strip()
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

    return address


def get_read_count_for_io_point(point: Any) -> int:
    """
    Returns how many Modbus registers/coils a read of `point` asks for.

    Registers (FC 3/4) scale with the IEC element width — a %MD point needs
    two registers per element — while coils and discrete inputs (FC 1/2) map
    1:1. Callers use this both to size the Modbus request and to size the
    zero-fill that replaces a failed read (see `get_zero_payload_for_io_point`),
    which is why it derives everything from the point itself.
    """
    if point.fc in (3, 4):
        registers_per_element = get_modbus_registers_count_for_iec_size(point.iec_location.size)
        return point.length * registers_per_element
    return point.length


def get_zero_payload_for_io_point(point: Any) -> List[Any]:
    """
    Returns a zeroed payload shaped like the response a successful read of
    `point` would have produced.

    Used to honour the I/O group's "set to zero" error handling: instead of a
    separate buffer-writing path, the failed read feeds this through the same
    conversion + batched mutex write the happy path uses, so a zeroed value
    lands exactly where a real one would.

    Booleans come back as `False` (FC 1/2 responses carry bits), registers as
    integer zeros.
    """
    count = get_read_count_for_io_point(point)
    if point.fc in (1, 2):
        return [False] * count
    return [0] * count
