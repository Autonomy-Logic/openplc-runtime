"""
Compatibility shim — the real implementation now lives in
shared/opcua_common/opcua_utils.py (shared by the OPC-UA Server and Client
plugins). Re-exported here so the Server's existing
`from .opcua_utils import ...` imports keep working unchanged.
"""

import os
import sys

_python_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _python_dir not in sys.path:
    sys.path.insert(0, _python_dir)

from shared.opcua_common.opcua_utils import (  # noqa: E402,F401
    TIME_DATATYPES,
    convert_value_for_opcua,
    convert_value_for_plc,
    infer_var_type,
    map_plc_to_opcua_type,
    milliseconds_to_timespec,
    timespec_to_milliseconds,
)
