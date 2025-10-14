"""
OpenPLC Python Plugin Configuration Package
"""

from .plugin_config_decode.plugin_config_contact import PluginConfigContract, PluginConfigError, ModbusIoPointConfig
from .plugin_config_decode.modbus_master_config_model import ModbusTcpConfig
from .plugin_config_decode.plugin_config_decoder import PluginInstanceConfig, PluginConfigDecoder

__all__ = [
    # abstract contract for each protocol config model
    'PluginConfigContract',
    # top level config instance
    'PluginConfigError', 
    'PluginConfigDecoder',
    'PluginInstanceConfig',
    # concrete protocol config models
    'ModbusIoPointConfig',
    'ModbusTcpConfig',
    # concrete protocol config models
    # 'EthercatConfig',
    # 'EthercatIoPointConfig',
]
