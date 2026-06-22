"""
Compatibility shim — the real implementation now lives in
shared/opcua_common/opcua_memory.py (shared by the OPC-UA Server and Client
plugins). Re-exported here so the Server's existing
`from .opcua_memory import ...` imports keep working unchanged.
"""

import os
import sys

_python_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _python_dir not in sys.path:
    sys.path.insert(0, _python_dir)

from shared.opcua_common.opcua_memory import (  # noqa: E402,F401
    STATUS_OK,
    TIME_DATATYPES,
    debug_force_value,
    debug_read_value,
    debug_unforce,
    debug_write_value,
    initialize_variable_cache,
    time_to_timespec,
    timespec_to_time,
)
