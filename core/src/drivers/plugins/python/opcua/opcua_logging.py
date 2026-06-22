"""
Compatibility shim — the real implementation now lives in
shared/opcua_common/opcua_logging.py so the OPC-UA Server and OPC-UA Client
plugins share a single logger. This module re-exports it so the Server's
existing `from .opcua_logging import ...` imports keep working unchanged.
"""

import os
import sys

# Make `shared` importable regardless of how this module is loaded.
_python_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _python_dir not in sys.path:
    sys.path.insert(0, _python_dir)

from shared.opcua_common.opcua_logging import (  # noqa: E402,F401
    OpcuaLogger,
    get_logger,
    log_debug,
    log_error,
    log_info,
    log_warn,
)
