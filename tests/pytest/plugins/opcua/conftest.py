# SPDX-License-Identifier: MIT
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

    def __init__(self, config_path: str = None):
        self.is_valid = True
        self.error_msg = ""
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
