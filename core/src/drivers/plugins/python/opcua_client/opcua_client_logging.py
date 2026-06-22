"""
Logging for the OPC-UA Client plugin.

Re-exports the shared OPC-UA logger (shared.opcua_common.opcua_logging) so the
client integrates with the runtime logging system exactly like the server.
"""

import os
import sys

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
