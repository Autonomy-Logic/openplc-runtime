# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
OpenPLC Python Plugin Shared Components Package

This package provides shared components for OpenPLC Python plugins, including
buffer access utilities, configuration handling, and type definitions.
"""

# Core buffer access functionality (refactored modular architecture)
from .safe_buffer_access_refactored import SafeBufferAccess

# Safe logging access functionality
from .safe_logging_access import SafeLoggingAccess

# Plugin logger (high-level logging interface for plugins)
from .plugin_logger import PluginLogger

# Core type definitions
from .iec_types import IEC_BOOL, IEC_BYTE, IEC_UINT, IEC_UDINT, IEC_ULINT
from .plugin_runtime_args import PluginRuntimeArgs
from .plugin_structure_validator import PluginStructureValidator
from .capsule_extraction import safe_extract_runtime_args_from_capsule

# Configuration models
from .plugin_config_decode.plugin_config_contact import PluginConfigContract, PluginConfigError
from .plugin_config_decode.modbus_master_config_model import ModbusIoPointConfig, ModbusMasterConfig

# Component interfaces (for advanced users who want to extend the system)
from .component_interfaces import (
    IBufferType, IMutexManager, IBufferValidator, IBufferAccessor,
    IBatchProcessor, IConfigHandler, ISafeBufferAccess
)

__all__ = [
    # Core buffer access (refactored)
    'SafeBufferAccess',

    # Safe logging access functionality
    'SafeLoggingAccess',

    # Plugin logger (high-level interface)
    'PluginLogger',

    # IEC type definitions
    'IEC_BOOL', 'IEC_BYTE', 'IEC_UINT', 'IEC_UDINT', 'IEC_ULINT',

    # Core type definitions
    'PluginRuntimeArgs',
    'PluginStructureValidator',
    'safe_extract_runtime_args_from_capsule',

    # Configuration models
    'PluginConfigContract',
    'PluginConfigError',
    'ModbusIoPointConfig',
    'ModbusMasterConfig',

    # Component interfaces (for extension)
    'IBufferType', 'IMutexManager', 'IBufferValidator', 'IBufferAccessor',
    'IBatchProcessor', 'IConfigHandler', 'ISafeBufferAccess',

    # Future extensions
    # 'EthercatConfig',
    # 'EthercatIoPointConfig',
]
