"""
OpenPLC Python Plugin Configuration Package
"""

from .plugin_config_decode.plugin_config_contact import PluginConfigContract, PluginConfigError
from .plugin_config_decode.modbus_master_config_model import ModbusIoPointConfig, ModbusMasterConfig

__all__ = [
    # abstract contract for each protocol config model
    'PluginConfigContract',
    # top level config instance
    'PluginConfigError', 
    # concrete protocol config models
    'ModbusIoPointConfig',
    'ModbusMasterConfig',
    # concrete protocol config models
    # 'EthercatConfig',
    # 'EthercatIoPointConfig',
]
