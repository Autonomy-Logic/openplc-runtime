# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Integration tests for OPC-UA data type handling.

Tests:
- Simple variable creation and initial values
- Structure (struct) variable handling
- Array variable handling
- Read/write operations for all data types
- Boundary value testing
"""

import pytest
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

# Add plugin paths
_plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "core" / "src" / "drivers" / "plugins" / "python"
sys.path.insert(0, str(_plugin_dir / "opcua"))
sys.path.insert(0, str(_plugin_dir / "shared"))

from asyncua import Server, ua
from config import load_config
from opcua_utils import map_plc_to_opcua_type, convert_value_for_opcua


class TestConfigLoading:
    """Tests for loading and parsing the test configuration."""

    def test_config_loads_successfully(self, test_config_path):
        """Configuration should load without errors."""
        config = load_config(test_config_path)
        assert config is not None

    def test_config_has_server_settings(self, test_config_path):
        """Configuration should have server settings."""
        config = load_config(test_config_path)
        assert config.server is not None
        assert config.server.name == "OpenPLC OPC-UA DataType Test Server"
        assert "4840" in config.server.endpoint_url

    def test_config_has_security_profiles(self, test_config_path):
        """Configuration should have security profiles."""
        config = load_config(test_config_path)
        assert len(config.server.security_profiles) == 3

        # Check insecure profile
        insecure = next(p for p in config.server.security_profiles if p.name == "insecure")
        assert insecure.enabled is True
        assert insecure.security_policy == "None"

    def test_config_has_users(self, test_config_path):
        """Configuration should have user definitions."""
        config = load_config(test_config_path)
        assert len(config.users) == 3

        usernames = [u.username for u in config.users]
        assert "viewer" in usernames
        assert "operator" in usernames
        assert "engineer" in usernames

    def test_config_has_simple_variables(self, test_config_path):
        """Configuration should have simple variables."""
        config = load_config(test_config_path)
        assert len(config.address_space.variables) == 20

    def test_config_has_structures(self, test_config_path):
        """Configuration should have structure definitions."""
        config = load_config(test_config_path)
        assert len(config.address_space.structures) == 5

        struct_names = [s.browse_name for s in config.address_space.structures]
        assert "sensor1" in struct_names
        assert "sensor2" in struct_names
        assert "robot_position" in struct_names
        assert "target_position" in struct_names
        assert "plc_status" in struct_names

    def test_config_has_arrays(self, test_config_path):
        """Configuration should have array definitions."""
        config = load_config(test_config_path)
        assert len(config.address_space.arrays) == 4

        array_names = [a.browse_name for a in config.address_space.arrays]
        assert "bool_array" in array_names
        assert "int_array" in array_names
        assert "real_array" in array_names
        assert "dint_array" in array_names


class TestSimpleVariables:
    """Tests for simple variable definitions in configuration."""

    def test_bool_variables_defined(self, test_config_path):
        """BOOL variables should be defined correctly."""
        config = load_config(test_config_path)
        vars_by_name = {v.browse_name: v for v in config.address_space.variables}

        # simple_bool
        assert "simple_bool" in vars_by_name
        var = vars_by_name["simple_bool"]
        assert var.datatype == "BOOL"
        assert var.index == 0
        assert var.initial_value is False

        # simple_bool_true
        assert "simple_bool_true" in vars_by_name
        var = vars_by_name["simple_bool_true"]
        assert var.datatype == "BOOL"
        assert var.index == 1
        assert var.initial_value is True

    def test_byte_variables_defined(self, test_config_path):
        """BYTE variables should be defined correctly."""
        config = load_config(test_config_path)
        vars_by_name = {v.browse_name: v for v in config.address_space.variables}

        # simple_byte
        assert "simple_byte" in vars_by_name
        var = vars_by_name["simple_byte"]
        assert var.datatype == "BYTE"
        assert var.index == 2
        assert var.initial_value == 0

        # simple_byte_max
        assert "simple_byte_max" in vars_by_name
        var = vars_by_name["simple_byte_max"]
        assert var.datatype == "BYTE"
        assert var.index == 3
        assert var.initial_value == 255

    def test_int_variables_defined(self, test_config_path):
        """INT variables should be defined correctly."""
        config = load_config(test_config_path)
        vars_by_name = {v.browse_name: v for v in config.address_space.variables}

        # simple_int
        assert "simple_int" in vars_by_name
        var = vars_by_name["simple_int"]
        assert var.datatype == "INT"
        assert var.index == 4

        # simple_int_max
        assert "simple_int_max" in vars_by_name
        var = vars_by_name["simple_int_max"]
        assert var.initial_value == 32767

        # simple_int_min
        assert "simple_int_min" in vars_by_name
        var = vars_by_name["simple_int_min"]
        assert var.initial_value == -32768

    def test_dint_variables_defined(self, test_config_path):
        """DINT variables should be defined correctly."""
        config = load_config(test_config_path)
        vars_by_name = {v.browse_name: v for v in config.address_space.variables}

        assert "simple_dint" in vars_by_name
        var = vars_by_name["simple_dint"]
        assert var.datatype == "DINT"
        assert var.index == 8

        assert "simple_dint_large" in vars_by_name
        var = vars_by_name["simple_dint_large"]
        assert var.initial_value == 100000

    def test_lint_variables_defined(self, test_config_path):
        """LINT variables should be defined correctly."""
        config = load_config(test_config_path)
        vars_by_name = {v.browse_name: v for v in config.address_space.variables}

        assert "simple_lint" in vars_by_name
        var = vars_by_name["simple_lint"]
        assert var.datatype == "LINT"
        assert var.index == 11

        assert "simple_lint_large" in vars_by_name
        var = vars_by_name["simple_lint_large"]
        assert var.initial_value == 1000000000

    def test_real_variables_defined(self, test_config_path):
        """REAL variables should be defined correctly."""
        config = load_config(test_config_path)
        vars_by_name = {v.browse_name: v for v in config.address_space.variables}

        assert "simple_real" in vars_by_name
        var = vars_by_name["simple_real"]
        assert var.datatype == "REAL"
        assert var.index == 13

        assert "simple_real_pi" in vars_by_name
        var = vars_by_name["simple_real_pi"]
        assert abs(var.initial_value - 3.14159) < 0.0001

        assert "simple_real_negative" in vars_by_name
        var = vars_by_name["simple_real_negative"]
        assert abs(var.initial_value - (-273.15)) < 0.01

    def test_string_variables_defined(self, test_config_path):
        """STRING variables should be defined correctly."""
        config = load_config(test_config_path)
        vars_by_name = {v.browse_name: v for v in config.address_space.variables}

        assert "simple_string" in vars_by_name
        var = vars_by_name["simple_string"]
        assert var.datatype == "STRING"
        assert var.index == 16
        assert var.initial_value == ""

        assert "simple_string_hello" in vars_by_name
        var = vars_by_name["simple_string_hello"]
        assert var.initial_value == "Hello OPC-UA"


class TestStructureVariables:
    """Tests for structure variable definitions."""

    def test_sensor_structure_defined(self, test_config_path):
        """Sensor structures should be defined with correct fields."""
        config = load_config(test_config_path)
        structs_by_name = {s.browse_name: s for s in config.address_space.structures}

        # sensor1
        assert "sensor1" in structs_by_name
        sensor1 = structs_by_name["sensor1"]
        assert len(sensor1.fields) == 3

        fields_by_name = {f.name: f for f in sensor1.fields}
        assert "sensor_id" in fields_by_name
        assert fields_by_name["sensor_id"].datatype == "INT"
        assert fields_by_name["sensor_id"].index == 20

        assert "value" in fields_by_name
        assert fields_by_name["value"].datatype == "REAL"
        assert fields_by_name["value"].index == 21

        assert "is_valid" in fields_by_name
        assert fields_by_name["is_valid"].datatype == "BOOL"
        assert fields_by_name["is_valid"].index == 22

    def test_position_structure_defined(self, test_config_path):
        """Position structures should be defined with x, y, z fields."""
        config = load_config(test_config_path)
        structs_by_name = {s.browse_name: s for s in config.address_space.structures}

        # robot_position
        assert "robot_position" in structs_by_name
        pos = structs_by_name["robot_position"]
        assert len(pos.fields) == 3

        fields_by_name = {f.name: f for f in pos.fields}
        assert "x" in fields_by_name
        assert "y" in fields_by_name
        assert "z" in fields_by_name

        # All should be REAL
        for name in ["x", "y", "z"]:
            assert fields_by_name[name].datatype == "REAL"

    def test_plc_status_structure_defined(self, test_config_path):
        """PLC status structure should have mixed types."""
        config = load_config(test_config_path)
        structs_by_name = {s.browse_name: s for s in config.address_space.structures}

        assert "plc_status" in structs_by_name
        status = structs_by_name["plc_status"]
        assert len(status.fields) == 5

        fields_by_name = {f.name: f for f in status.fields}

        assert fields_by_name["device_name"].datatype == "STRING"
        assert fields_by_name["error_code"].datatype == "DINT"
        assert fields_by_name["temperature"].datatype == "REAL"
        assert fields_by_name["is_online"].datatype == "BOOL"
        assert fields_by_name["uptime_seconds"].datatype == "LINT"


class TestArrayVariables:
    """Tests for array variable definitions."""

    def test_bool_array_defined(self, test_config_path):
        """BOOL array should be defined correctly."""
        config = load_config(test_config_path)
        arrays_by_name = {a.browse_name: a for a in config.address_space.arrays}

        assert "bool_array" in arrays_by_name
        arr = arrays_by_name["bool_array"]
        assert arr.datatype == "BOOL"
        assert arr.length == 8
        assert arr.index == 50

    def test_int_array_defined(self, test_config_path):
        """INT array should be defined correctly."""
        config = load_config(test_config_path)
        arrays_by_name = {a.browse_name: a for a in config.address_space.arrays}

        assert "int_array" in arrays_by_name
        arr = arrays_by_name["int_array"]
        assert arr.datatype == "INT"
        assert arr.length == 5
        assert arr.index == 58

    def test_real_array_defined(self, test_config_path):
        """REAL array should be defined correctly."""
        config = load_config(test_config_path)
        arrays_by_name = {a.browse_name: a for a in config.address_space.arrays}

        assert "real_array" in arrays_by_name
        arr = arrays_by_name["real_array"]
        assert arr.datatype == "REAL"
        assert arr.length == 4
        assert arr.index == 63

    def test_dint_array_defined(self, test_config_path):
        """DINT array should be defined correctly."""
        config = load_config(test_config_path)
        arrays_by_name = {a.browse_name: a for a in config.address_space.arrays}

        assert "dint_array" in arrays_by_name
        arr = arrays_by_name["dint_array"]
        assert arr.datatype == "DINT"
        assert arr.length == 3
        assert arr.index == 67


class TestVariablePermissions:
    """Tests for variable permission definitions."""

    def test_readwrite_permissions(self, test_config_path):
        """Variables with rw permissions should allow writes."""
        config = load_config(test_config_path)
        vars_by_name = {v.browse_name: v for v in config.address_space.variables}

        var = vars_by_name["simple_int"]
        assert var.permissions.viewer == "r"
        assert var.permissions.operator == "rw"
        assert var.permissions.engineer == "rw"

    def test_readonly_permissions(self, test_config_path):
        """Readonly variables should have r-only permissions."""
        config = load_config(test_config_path)
        vars_by_name = {v.browse_name: v for v in config.address_space.variables}

        var = vars_by_name["cycle_counter"]
        assert var.permissions.viewer == "r"
        assert var.permissions.operator == "r"
        assert var.permissions.engineer == "r"

    def test_structure_field_permissions(self, test_config_path):
        """Structure fields should have correct permissions."""
        config = load_config(test_config_path)
        structs_by_name = {s.browse_name: s for s in config.address_space.structures}

        sensor1 = structs_by_name["sensor1"]
        fields_by_name = {f.name: f for f in sensor1.fields}

        # is_valid is readonly
        is_valid = fields_by_name["is_valid"]
        assert is_valid.permissions.viewer == "r"
        assert is_valid.permissions.operator == "r"
        assert is_valid.permissions.engineer == "r"

        # sensor_id is readwrite
        sensor_id = fields_by_name["sensor_id"]
        assert sensor_id.permissions.operator == "rw"


class TestMockBufferAccess:
    """Tests for the MockSafeBufferAccess fixture."""

    def test_mock_loads_config(self, mock_buffer_access, var_indices):
        """Mock should load variable definitions from config."""
        # Check a known variable exists
        val, err = mock_buffer_access.get_var_value(var_indices.SIMPLE_INT)
        assert err == "Success"

    def test_mock_get_set_value(self, mock_buffer_access):
        """Mock should support get/set operations."""
        # Set a value
        success, err = mock_buffer_access.set_var_value(100, 42)
        assert success is True
        assert err == "Success"

        # Get the value back
        val, err = mock_buffer_access.get_var_value(100)
        assert val == 42
        assert err == "Success"

    def test_mock_batch_operations(self, mock_buffer_access):
        """Mock should support batch get/set operations."""
        # Set multiple values
        values = {100: 1, 101: 2, 102: 3}
        success, err = mock_buffer_access.set_var_values_batch(values)
        assert success is True

        # Get multiple values
        result = mock_buffer_access.get_var_values_batch([100, 101, 102])
        assert result[100] == 1
        assert result[101] == 2
        assert result[102] == 3

    def test_mock_thread_safety(self, mock_buffer_access):
        """Mock should support locking operations."""
        mock_buffer_access.acquire_mutex()
        mock_buffer_access.release_mutex()

        mock_buffer_access.lock()
        mock_buffer_access.unlock()

    def test_mock_config_path(self, mock_buffer_access, test_config_path):
        """Mock should return config path."""
        assert mock_buffer_access.get_config_path() == test_config_path


class TestTypeMapping:
    """Tests for PLC to OPC-UA type mapping."""

    @pytest.mark.parametrize("plc_type,expected_opcua_type", [
        ("BOOL", ua.VariantType.Boolean),
        ("BYTE", ua.VariantType.Byte),
        ("INT", ua.VariantType.Int16),
        ("DINT", ua.VariantType.Int32),
        ("INT32", ua.VariantType.Int32),
        ("LINT", ua.VariantType.Int64),
        ("FLOAT", ua.VariantType.Float),
        ("REAL", ua.VariantType.Float),
        ("STRING", ua.VariantType.String),
    ])
    def test_type_mapping(self, plc_type, expected_opcua_type):
        """Each PLC type should map to correct OPC-UA type."""
        assert map_plc_to_opcua_type(plc_type) == expected_opcua_type


class TestValueConversion:
    """Tests for value conversion to OPC-UA format."""

    @pytest.mark.parametrize("datatype,input_value,expected_type", [
        ("BOOL", True, bool),
        ("BOOL", False, bool),
        ("BYTE", 128, int),
        ("INT", 1000, int),
        ("DINT", 100000, int),
        ("LINT", 1000000000, int),
        ("FLOAT", 3.14, float),
        ("STRING", "test", str),
    ])
    def test_conversion_returns_correct_type(self, datatype, input_value, expected_type):
        """Converted values should have correct Python type."""
        result = convert_value_for_opcua(datatype, input_value)
        assert isinstance(result, expected_type)


class TestIndexMapping:
    """Tests verifying correct index mapping from debug.c."""

    def test_simple_variable_indices(self, test_config_path, var_indices):
        """Simple variable indices should match debug.c."""
        config = load_config(test_config_path)
        vars_by_name = {v.browse_name: v for v in config.address_space.variables}

        assert vars_by_name["simple_bool"].index == var_indices.SIMPLE_BOOL
        assert vars_by_name["simple_byte"].index == var_indices.SIMPLE_BYTE
        assert vars_by_name["simple_int"].index == var_indices.SIMPLE_INT
        assert vars_by_name["simple_dint"].index == var_indices.SIMPLE_DINT
        assert vars_by_name["simple_lint"].index == var_indices.SIMPLE_LINT
        assert vars_by_name["simple_real"].index == var_indices.SIMPLE_REAL
        assert vars_by_name["simple_string"].index == var_indices.SIMPLE_STRING

    def test_structure_field_indices(self, test_config_path, var_indices):
        """Structure field indices should match debug.c."""
        config = load_config(test_config_path)
        structs_by_name = {s.browse_name: s for s in config.address_space.structures}

        # sensor1
        sensor1 = structs_by_name["sensor1"]
        fields = {f.name: f for f in sensor1.fields}
        assert fields["sensor_id"].index == var_indices.SENSOR1_SENSOR_ID
        assert fields["value"].index == var_indices.SENSOR1_VALUE
        assert fields["is_valid"].index == var_indices.SENSOR1_IS_VALID

        # robot_position
        robot = structs_by_name["robot_position"]
        fields = {f.name: f for f in robot.fields}
        assert fields["x"].index == var_indices.ROBOT_POSITION_X
        assert fields["y"].index == var_indices.ROBOT_POSITION_Y
        assert fields["z"].index == var_indices.ROBOT_POSITION_Z

    def test_array_indices(self, test_config_path, var_indices):
        """Array indices should match debug.c."""
        config = load_config(test_config_path)
        arrays_by_name = {a.browse_name: a for a in config.address_space.arrays}

        assert arrays_by_name["bool_array"].index == var_indices.BOOL_ARRAY_START
        assert arrays_by_name["int_array"].index == var_indices.INT_ARRAY_START
        assert arrays_by_name["real_array"].index == var_indices.REAL_ARRAY_START
        assert arrays_by_name["dint_array"].index == var_indices.DINT_ARRAY_START
