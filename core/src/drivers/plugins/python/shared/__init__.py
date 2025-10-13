"""
OpenPLC Python Plugin Configuration Package
"""

from .base_protocol_config import BaseProtocolConfig, PluginConfigError, ModbusIoPointConfig
from .plugin_config_models import PluginInstanceConfig, ModbusTcpConfig, PluginConfigParser

__all__ = [
    # abstract contract for each protocol config model
    'BaseProtocolConfig',
    # top level config instance
    'PluginConfigError', 
    'PluginConfigParser',
    'PluginInstanceConfig',
    # concrete protocol config models
    'ModbusIoPointConfig',
    'ModbusTcpConfig',
    # concrete protocol config models
    # 'EthercatConfig',
    # 'EthercatIoPointConfig',
]
