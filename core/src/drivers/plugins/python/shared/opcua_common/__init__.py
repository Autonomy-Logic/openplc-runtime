"""
Shared OPC-UA helpers used by both the OPC-UA Server and OPC-UA Client plugins.

These modules are protocol-agnostic between the two plugin roles:
- opcua_logging: centralized logger (runtime integration + stdout fallback).
- types: VariableMetadata (plain dataclass, no asyncua dependency).
- opcua_memory: STruC++ debugger surface access (debug_read/write/set/size).
- opcua_utils: IEC <-> OPC-UA value/type conversion.
- opcua_security_common: security policy/mode maps + self-signed cert generation.

The Server keeps thin re-export shims at its old module paths
(opcua/opcua_logging.py, opcua/opcua_utils.py, opcua/opcua_memory.py) so its
existing imports keep resolving to the single source of truth here.
"""
