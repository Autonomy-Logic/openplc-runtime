# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Modbus Master plugin type definitions."""

from dataclasses import dataclass
from typing import Optional, Dict, Any, List


@dataclass
class ModbusConnectionConfig:
    """Configuration for Modbus TCP connection."""
    host: str
    port: int
    timeout_ms: int


@dataclass
class ModbusIOPoint:
    """Represents a Modbus I/O point configuration."""
    name: str
    fc: int  # Function code
    offset: str  # Register/coil offset
    length: int  # Number of elements
    iec_location: Any  # IECAddress object
    cycle_time_ms: int


@dataclass
class ModbusDeviceConfig:
    """Configuration for a Modbus slave device."""
    name: str
    host: str
    port: int
    timeout_ms: int
    io_points: List[ModbusIOPoint]


@dataclass
class BufferAccessDetails:
    """Details for SafeBufferAccess operations."""
    buffer_type_str: str
    buffer_idx: int
    bit_idx: Optional[int]
    element_size_bytes: int
    is_boolean: bool
