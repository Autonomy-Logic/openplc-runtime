# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""
Pytest fixtures for OPC-UA plugin tests.

Provides:
- Mock SafeBufferAccess for simulating PLC memory
- Configuration loading fixtures
- OPC-UA server/client fixtures for integration tests
"""

import pytest
import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Dict, Any, List
from unittest.mock import MagicMock, patch

# Add plugin paths for imports
_test_dir = Path(__file__).parent
_plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "core" / "src" / "drivers" / "plugins" / "python"
_opcua_dir = _plugin_dir / "opcua"
_shared_dir = _plugin_dir / "shared"

sys.path.insert(0, str(_plugin_dir))
sys.path.insert(0, str(_opcua_dir))
sys.path.insert(0, str(_shared_dir))


# Test configuration path
TEST_CONFIG_PATH = _test_dir / "test_project" / "opcua_datatype_test.json"


class MockDebugVariable:
    """Represents a single debug variable in mock PLC memory."""

    def __init__(self, index: int, datatype: str, initial_value: Any = None):
        self.index = index
        self.datatype = datatype.upper()
        self._value = initial_value if initial_value is not None else self._default_value()

    def _default_value(self) -> Any:
        """Get default value based on datatype."""
        defaults = {
            "BOOL": False,
            "BYTE": 0,
            "INT": 0,
            "DINT": 0,
            "INT32": 0,
            "LINT": 0,
            "REAL": 0.0,
            "FLOAT": 0.0,
            "STRING": "",
        }
        return defaults.get(self.datatype, 0)

    @property
    def value(self) -> Any:
        return self._value

    @value.setter
    def value(self, val: Any):
        self._value = val


class MockSafeBufferAccess:
    """
    Mock SafeBufferAccess that simulates PLC debug variable memory.

    Provides the same interface as the real SafeBufferAccess for testing
    OPC-UA synchronization without a running PLC.
    """

    def __init__(self, config_path: str = None, runtime_args: object = None):
        self.is_valid = True
        self.error_msg = ""
        self.runtime_args = runtime_args
        self._lock = threading.RLock()
        self._variables: Dict[int, MockDebugVariable] = {}
        self._config_path = config_path or str(TEST_CONFIG_PATH)

        # Initialize variables from config if available
        if config_path and os.path.exists(config_path):
            self._load_variables_from_config(config_path)

    def _load_variables_from_config(self, config_path: str):
        """Load variable definitions from opcua.json config."""
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)

            if isinstance(config, list) and len(config) > 0:
                address_space = config[0].get("config", {}).get("address_space", {})

                # Load simple variables
                for var in address_space.get("variables", []):
                    idx = var.get("index", -1)
                    if idx >= 0:
                        self._variables[idx] = MockDebugVariable(
                            index=idx,
                            datatype=var.get("datatype", "INT"),
                            initial_value=var.get("initial_value")
                        )

                # Load structure fields
                for struct in address_space.get("structures", []):
                    for field in struct.get("fields", []):
                        idx = field.get("index", -1)
                        if idx >= 0:
                            self._variables[idx] = MockDebugVariable(
                                index=idx,
                                datatype=field.get("datatype", "INT"),
                                initial_value=field.get("initial_value")
                            )

                # Load array variables (single index per array in this model)
                for arr in address_space.get("arrays", []):
                    idx = arr.get("index", -1)
                    if idx >= 0:
                        self._variables[idx] = MockDebugVariable(
                            index=idx,
                            datatype=arr.get("datatype", "INT"),
                            initial_value=arr.get("initial_value")
                        )
        except Exception as e:
            print(f"Warning: Failed to load config: {e}")

    def get_config_path(self) -> str:
        """Return path to opcua.json configuration."""
        return self._config_path

    def invalidate(self) -> None:
        """Mark the accessor unusable, as SafeBufferAccess.invalidate() does."""
        self.is_valid = False
        self.error_msg = "Buffer accessor invalidated"

    def acquire_mutex(self):
        """Acquire lock for thread-safe access."""
        self._lock.acquire()

    def release_mutex(self):
        """Release lock."""
        try:
            self._lock.release()
        except RuntimeError:
            pass

    def lock(self):
        """Alias for acquire_mutex."""
        self.acquire_mutex()

    def unlock(self):
        """Alias for release_mutex."""
        self.release_mutex()

    def validate_pointers(self) -> tuple:
        """Validate that buffer pointers are valid."""
        return (True, "")

    def get_var_value(self, index: int) -> tuple:
        """
        Get value of a debug variable by index.

        Returns:
            Tuple of (value, error_message)
        """
        if index not in self._variables:
            # Auto-create variable if not exists
            self._variables[index] = MockDebugVariable(index, "INT", 0)

        return (self._variables[index].value, "Success")

    def set_var_value(self, index: int, value: Any) -> tuple:
        """
        Set value of a debug variable by index.

        Returns:
            Tuple of (success, error_message)
        """
        if index not in self._variables:
            self._variables[index] = MockDebugVariable(index, "INT", value)
        else:
            self._variables[index].value = value

        return (True, "Success")

    def get_var_values_batch(self, indices: List[int]) -> Dict[int, Any]:
        """Get multiple variable values at once."""
        result = {}
        for idx in indices:
            val, _ = self.get_var_value(idx)
            result[idx] = val
        return result

    def set_var_values_batch(self, values: Dict[int, Any]) -> tuple:
        """Set multiple variable values at once."""
        for idx, val in values.items():
            self.set_var_value(idx, val)
        return (True, "Success")

    def get_variable(self, index: int) -> MockDebugVariable:
        """Get MockDebugVariable instance for direct manipulation in tests."""
        if index not in self._variables:
            self._variables[index] = MockDebugVariable(index, "INT", 0)
        return self._variables[index]

    def set_variable_type(self, index: int, datatype: str, initial_value: Any = None):
        """Set up a variable with specific type for testing."""
        self._variables[index] = MockDebugVariable(index, datatype, initial_value)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def test_config_path():
    """Return path to test configuration file."""
    return str(TEST_CONFIG_PATH)


@pytest.fixture
def test_config_dict():
    """Load and return test configuration as dictionary."""
    with open(TEST_CONFIG_PATH, 'r') as f:
        return json.load(f)


@pytest.fixture
def mock_buffer_access(test_config_path):
    """
    Create a MockSafeBufferAccess instance initialized with test config.
    """
    return MockSafeBufferAccess(test_config_path)


@pytest.fixture
def mock_buffer_access_empty():
    """
    Create an empty MockSafeBufferAccess for unit tests.
    """
    return MockSafeBufferAccess()


@pytest.fixture
def opcua_config(test_config_path):
    """
    Load OpcuaConfig from test configuration file.
    """
    from config import load_config
    return load_config(test_config_path)


class StubRuntimeArgs:
    """Runtime args stand-in holding one DINT leaf at (0, 0) and counting debug reads and writes."""

    LEAF_SIZE = 4

    def __init__(self):
        from opcua_memory import STATUS_OK

        self._status_ok = STATUS_OK
        self._lock = threading.Lock()
        self._leaf = bytearray(self.LEAF_SIZE)
        self._debug_reads = 0
        self._debug_writes = 0

    @staticmethod
    def _is_leaf(arr: object, elem: object) -> bool:
        """True for the address of the single leaf this stub holds."""
        return int(getattr(arr, "value", arr)) == 0 and int(getattr(elem, "value", elem)) == 0

    def validate_pointers(self) -> tuple:
        return (True, "")

    def debug_array_count(self) -> int:
        return 1

    def debug_elem_count(self, arr: object) -> int:
        return 1

    def debug_size(self, arr: object, elem: object) -> int:
        return self.LEAF_SIZE if self._is_leaf(arr, elem) else 0

    def debug_read(self, arr: object, elem: object, dest: object) -> int:
        with self._lock:
            self._debug_reads += 1
            if not self._is_leaf(arr, elem):
                return 0
            for i, byte in enumerate(self._leaf):
                dest[i] = byte
            return self.LEAF_SIZE

    def debug_write(self, arr: object, elem: object, src: object, length: object) -> int:
        with self._lock:
            self._debug_writes += 1
            if not self._is_leaf(arr, elem):
                return 0
            count = min(int(getattr(length, "value", length)), self.LEAF_SIZE)
            self._leaf[:count] = bytes(bytearray(src[:count]))
            return self._status_ok

    def debug_set(self, arr: object, elem: object, *args: object) -> int:
        return self._status_ok

    def debug_calls(self) -> int:
        """Number of debug_read and debug_write calls received so far."""
        with self._lock:
            return self._debug_reads + self._debug_writes


@pytest.fixture
def stub_runtime_args():
    """Runtime args that let start_loop() run the OPC-UA server without a PLC."""
    return StubRuntimeArgs()


class AnonymousOperatorServer:
    """Stop-lifecycle server configuration: where it listens and the node it publishes."""

    PORT = 14840
    ENDPOINT = f"opc.tcp://127.0.0.1:{PORT}/openplc/opcua"
    NAMESPACE_URI = "urn:openplc:opcua:stop-lifecycle:test"
    NODE_ID = "setpoint"

    def __init__(self, config_path: str):
        self.config_path = config_path


@pytest.fixture
def anonymous_operator_server(tmp_path):
    """opcua.json with an Anonymous "operator" profile and a read-write DINT "setpoint" on 14840."""
    from shared.plugin_config_decode.opcua_config_model import OPCUA_CONFIG_MIN_FORMAT_VERSION

    config = [
        {
            "name": "opcua_stop_lifecycle",
            "protocol": "OPC-UA",
            "config": {
                "format_version": OPCUA_CONFIG_MIN_FORMAT_VERSION,
                "server": {
                    "name": "OpenPLC OPC-UA Stop Lifecycle Test Server",
                    "application_uri": "urn:openplc:opcua:stop-lifecycle:test",
                    "product_uri": "urn:openplc:runtime:test",
                    "endpoint_url": AnonymousOperatorServer.ENDPOINT,
                    "security_profiles": [
                        {
                            "name": "insecure",
                            "enabled": True,
                            "security_policy": "None",
                            "security_mode": "None",
                            "auth_methods": ["Anonymous"],
                            "anonymous_role": "operator",
                        }
                    ],
                },
                "security": {
                    "server_certificate_strategy": "none",
                    "server_certificate_custom": None,
                    "server_private_key_custom": None,
                    "trusted_client_certificates": [],
                },
                "users": [],
                "cycle_time_ms": 100,
                "address_space": {
                    "namespace_uri": AnonymousOperatorServer.NAMESPACE_URI,
                    "variables": [
                        {
                            "node_id": AnonymousOperatorServer.NODE_ID,
                            "browse_name": "setpoint",
                            "display_name": "setpoint",
                            "datatype": "DINT",
                            "description": "Read-write setpoint",
                            "arr": 0,
                            "elem": 0,
                            "size": StubRuntimeArgs.LEAF_SIZE,
                            "permissions": {"viewer": "r", "operator": "rw", "engineer": "rw"},
                        }
                    ],
                    "structures": [],
                    "arrays": [],
                },
            },
        }
    ]
    config_path = tmp_path / "opcua.json"
    config_path.write_text(json.dumps(config))
    return AnonymousOperatorServer(str(config_path))


class _ConfigPathPairAccess(MockSafeBufferAccess):
    """MockSafeBufferAccess whose get_config_path returns the (path, message) pair."""

    def get_config_path(self) -> tuple:
        return (self._config_path, "Success")


class MockSafeBufferAccessFactory:
    """Stands in for plugin.SafeBufferAccess and keeps every MockSafeBufferAccess it builds."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.created: List[MockSafeBufferAccess] = []

    def __call__(self, runtime_args: object) -> MockSafeBufferAccess:
        accessor = _ConfigPathPairAccess(self.config_path, runtime_args=runtime_args)
        self.created.append(accessor)
        return accessor


@pytest.fixture
def mock_buffer_access_factory(anonymous_operator_server):
    """A plugin.SafeBufferAccess replacement bound to the stop-lifecycle configuration."""
    return MockSafeBufferAccessFactory(anonymous_operator_server.config_path)


class RecordingLogAccessor:
    """Logging accessor that keeps every line the OPC-UA logger emits."""

    is_valid = True

    def __init__(self):
        self.lines: List[str] = []

    def log_info(self, message: str) -> None:
        self.lines.append(message)

    def log_warn(self, message: str) -> None:
        self.lines.append(message)

    def log_error(self, message: str) -> None:
        self.lines.append(message)

    def log_debug(self, message: str) -> None:
        self.lines.append(message)


@pytest.fixture
def opcua_log_lines():
    """Route the OPC-UA logger into a list for the test, and drop the route afterwards."""
    from opcua_logging import get_logger, reset_logger

    reset_logger()
    recorder = RecordingLogAccessor()
    assert get_logger().initialize(recorder)
    yield recorder.lines
    reset_logger()


class LevelRecordingLogAccessor(RecordingLogAccessor):
    """Logging accessor that keeps each OPC-UA line with its level and appends it to `lines`."""

    def __init__(self, lines: List[str]):
        super().__init__()
        self.lines = lines
        self.records: List[tuple[str, str]] = []

    def _record(self, level: str, message: str) -> None:
        self.records.append((level, message))
        self.lines.append(message)

    def log_info(self, message: str) -> None:
        self._record("INFO", message)

    def log_warn(self, message: str) -> None:
        self._record("WARN", message)

    def log_error(self, message: str) -> None:
        self._record("ERROR", message)

    def log_debug(self, message: str) -> None:
        self._record("DEBUG", message)

    def lines_at(self, level: str) -> List[str]:
        """The lines logged at the given level, in order."""
        return [message for recorded, message in list(self.records) if recorded == level]


@pytest.fixture
def opcua_log_records(opcua_log_lines):
    """Route the OPC-UA logger into a recorder that keeps each line's level; the lines still reach
    opcua_log_lines."""
    from opcua_logging import get_logger

    recorder = LevelRecordingLogAccessor(opcua_log_lines)
    assert get_logger().initialize(recorder)
    return recorder


class SecureManagerServer:
    """Manager-level server configuration: its endpoint, plugin directory and variable count."""

    PORT = 14841
    ENDPOINT = f"opc.tcp://127.0.0.1:{PORT}/openplc/opcua"
    NAMESPACE_URI = "urn:openplc:opcua:stop-during-start:test"
    VARIABLE_COUNT = 40

    def __init__(self, config_path: str, plugin_dir: Path):
        self.config_path = config_path
        self.plugin_dir = plugin_dir


@pytest.fixture
def secure_manager_server(tmp_path):
    """opcua.json on 14841 with an Anonymous None profile, a Basic256Sha256 SignAndEncrypt profile,
    a self-signed certificate generated in a temporary plugin directory and 40 DINT variables."""
    from shared.plugin_config_decode.opcua_config_model import OPCUA_CONFIG_MIN_FORMAT_VERSION

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    variables = [
        {
            "node_id": f"dint_{index}",
            "browse_name": f"dint_{index}",
            "display_name": f"dint_{index}",
            "datatype": "DINT",
            "description": f"DINT variable {index}",
            "arr": 0,
            "elem": index,
            "size": StubRuntimeArgs.LEAF_SIZE,
            "permissions": {"viewer": "r", "operator": "rw", "engineer": "rw"},
        }
        for index in range(SecureManagerServer.VARIABLE_COUNT)
    ]
    config = [
        {
            "name": "opcua_stop_during_start",
            "protocol": "OPC-UA",
            "config": {
                "format_version": OPCUA_CONFIG_MIN_FORMAT_VERSION,
                "server": {
                    "name": "OpenPLC OPC-UA Stop During Start Test Server",
                    "application_uri": "urn:openplc:opcua:stop-during-start:test",
                    "product_uri": "urn:openplc:runtime:test",
                    "endpoint_url": SecureManagerServer.ENDPOINT,
                    "security_profiles": [
                        {
                            "name": "insecure",
                            "enabled": True,
                            "security_policy": "None",
                            "security_mode": "None",
                            "auth_methods": ["Anonymous"],
                            "anonymous_role": "viewer",
                        },
                        {
                            "name": "secure",
                            "enabled": True,
                            "security_policy": "Basic256Sha256",
                            "security_mode": "SignAndEncrypt",
                            "auth_methods": ["Anonymous"],
                            "anonymous_role": "viewer",
                        },
                    ],
                },
                "security": {
                    "server_certificate_strategy": "auto_self_signed",
                    "server_certificate_custom": None,
                    "server_private_key_custom": None,
                    "trusted_client_certificates": [],
                },
                "users": [],
                "cycle_time_ms": 100,
                "address_space": {
                    "namespace_uri": SecureManagerServer.NAMESPACE_URI,
                    "variables": variables,
                    "structures": [],
                    "arrays": [],
                },
            },
        }
    ]
    config_path = tmp_path / "opcua.json"
    config_path.write_text(json.dumps(config))
    return SecureManagerServer(str(config_path), plugin_dir)


@pytest.fixture
def secure_manager_accessor(secure_manager_server, stub_runtime_args):
    """MockSafeBufferAccess over the manager-level configuration, on the stub runtime args."""
    return MockSafeBufferAccess(secure_manager_server.config_path, runtime_args=stub_runtime_args)


@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ============================================================================
# Variable Index Constants (matching debug.c)
# ============================================================================

class VarIndices:
    """Constants for debug variable indices from the test program."""

    # Simple BOOL variables
    SIMPLE_BOOL = 0
    SIMPLE_BOOL_TRUE = 1

    # Simple BYTE variables
    SIMPLE_BYTE = 2
    SIMPLE_BYTE_MAX = 3

    # Simple INT variables
    SIMPLE_INT = 4
    SIMPLE_INT_NEGATIVE = 5
    SIMPLE_INT_MAX = 6
    SIMPLE_INT_MIN = 7

    # Simple DINT variables
    SIMPLE_DINT = 8
    SIMPLE_DINT_LARGE = 9
    SIMPLE_DINT_NEGATIVE = 10

    # Simple LINT variables
    SIMPLE_LINT = 11
    SIMPLE_LINT_LARGE = 12

    # Simple REAL variables
    SIMPLE_REAL = 13
    SIMPLE_REAL_PI = 14
    SIMPLE_REAL_NEGATIVE = 15

    # Simple STRING variables
    SIMPLE_STRING = 16
    SIMPLE_STRING_HELLO = 17

    # Structure: sensor1
    SENSOR1_SENSOR_ID = 20
    SENSOR1_VALUE = 21
    SENSOR1_IS_VALID = 22

    # Structure: sensor2
    SENSOR2_SENSOR_ID = 26
    SENSOR2_VALUE = 27
    SENSOR2_IS_VALID = 28

    # Structure: robot_position
    ROBOT_POSITION_X = 32
    ROBOT_POSITION_Y = 33
    ROBOT_POSITION_Z = 34

    # Structure: target_position
    TARGET_POSITION_X = 38
    TARGET_POSITION_Y = 39
    TARGET_POSITION_Z = 40

    # Structure: plc_status
    PLC_STATUS_DEVICE_NAME = 44
    PLC_STATUS_ERROR_CODE = 45
    PLC_STATUS_TEMPERATURE = 46
    PLC_STATUS_IS_ONLINE = 47
    PLC_STATUS_UPTIME_SECONDS = 48

    # Arrays
    BOOL_ARRAY_START = 50  # 50-57
    INT_ARRAY_START = 58   # 58-62
    REAL_ARRAY_START = 63  # 63-66
    DINT_ARRAY_START = 67  # 67-69

    # Working variables
    CYCLE_COUNTER = 70
    TOGGLE_OUTPUT = 71


@pytest.fixture
def var_indices():
    """Provide variable indices constants."""
    return VarIndices


# ============================================================================
# Data Type Test Values
# ============================================================================

class TestValues:
    """Test values for each data type with boundary cases."""

    BOOL = {
        "zero": False,
        "one": True,
    }

    BYTE = {
        "zero": 0,
        "mid": 128,
        "max": 255,
    }

    INT = {
        "zero": 0,
        "positive": 1000,
        "negative": -1000,
        "max": 32767,
        "min": -32768,
    }

    DINT = {
        "zero": 0,
        "positive": 100000,
        "negative": -100000,
        "max": 2147483647,
        "min": -2147483648,
    }

    LINT = {
        "zero": 0,
        "positive": 1000000000,
        "negative": -1000000000,
        "large": 9223372036854775807,
    }

    REAL = {
        "zero": 0.0,
        "positive": 3.14159,
        "negative": -273.15,
        "small": 0.000001,
        "large": 1000000.5,
    }

    STRING = {
        "empty": "",
        "hello": "Hello OPC-UA",
        "special": "Test!@#$%",
        "unicode": "Test Unicode",
    }


@pytest.fixture
def test_values():
    """Provide test values for each data type."""
    return TestValues
