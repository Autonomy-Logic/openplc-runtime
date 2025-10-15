"""
Plugin configuration decoding package
"""

from .plugin_config_contact import PluginConfigContract, PluginConfigError
from .modbus_master_config_model import ModbusIoPointConfig, ModbusMasterConfig

__all__ = [
    'PluginConfigContract',
    'PluginConfigError',
    'ModbusIoPointConfig',
    'ModbusMasterConfig'
]