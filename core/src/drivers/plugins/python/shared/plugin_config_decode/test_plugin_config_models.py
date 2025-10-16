#!/usr/bin/env python3
"""
Test suite for Plugin Configuration Models
Updated to reflect changes in ModbusMasterConfig and PluginConfigContract.
"""

import os
import sys
import json
import tempfile

# Add current directory to path for imports
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

try:
    from .modbus_master_config_model import ModbusMasterConfig, ModbusIoPointConfig
except ImportError:
    from modbus_master_config_model import ModbusMasterConfig, ModbusIoPointConfig

def test_modbus_config_from_valid_dict():
    """Test ModbusMasterConfig initialization and from_json_file with valid data."""
    valid_config_data = [{
        "name": "test_modbus_device",
        "protocol": "MODBUS",
        "config": {
            "type": "MASTER",  # ModbusMasterConfig expects this, though doesn't enforce
            "host": "192.168.1.100",
            "port": 502,
            "cycle_time_ms": 200,
            "timeout_ms": 5000,
            "io_points": [
                {
                    "fc": 1,
                    "offset": "0x0001",
                    "iec_location": "%IX0.0",
                    "len": 8
                },
                {
                    "fc": 5,
                    "offset": "0x0010",
                    "iec_location": "%QX0.0",
                    "len": 1
                }
            ]
        }
    }]

    print("--- Testing ModbusMasterConfig from valid data ---")
    tmp_file_path = None
    try:
        # Create a temporary JSON file
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".json") as tmp_file:
            json.dump(valid_config_data, tmp_file)
            tmp_file_path = tmp_file.name

        config_instance = ModbusMasterConfig(config_path="dummy_init_path")
        config_instance.from_json_file(tmp_file_path) # Load data from the temp file

        # Assertions for top-level attributes
        assert config_instance.name == "test_modbus_device", f"Expected name 'test_modbus_device', got {config_instance.name}"
        assert config_instance.protocol == "MODBUS", f"Expected protocol 'MODBUS', got {config_instance.protocol}"
        
        # Assertions for nested config attributes
        assert config_instance.type == "MASTER", f"Expected type 'MASTER', got {config_instance.type}"
        assert config_instance.host == "192.168.1.100", f"Expected host '192.168.1.100', got {config_instance.host}"
        assert config_instance.port == 502, f"Expected port 502, got {config_instance.port}"
        assert config_instance.cycle_time_ms == 200, f"Expected cycle_time_ms 200, got {config_instance.cycle_time_ms}"
        assert config_instance.timeout_ms == 5000, f"Expected timeout_ms 5000, got {config_instance.timeout_ms}"

        # Assertions for io_points
        assert len(config_instance.io_points) == 2, f"Expected 2 io_points, got {len(config_instance.io_points)}"
        
        point1 = config_instance.io_points[0]
        assert isinstance(point1, ModbusIoPointConfig), "io_point should be an instance of ModbusIoPointConfig"
        assert point1.fc == 1, f"Point 1: Expected fc 1, got {point1.fc}"
        assert point1.offset == "0x0001", f"Point 1: Expected offset '0x0001', got {point1.offset}"
        assert point1.iec_location == "%IX0.0", f"Point 1: Expected iec_location '%IX0.0', got {point1.iec_location}"
        assert point1.length == 8, f"Point 1: Expected length 8, got {point1.length}"

        point2 = config_instance.io_points[1]
        assert isinstance(point2, ModbusIoPointConfig), "io_point should be an instance of ModbusIoPointConfig"
        assert point2.fc == 5, f"Point 2: Expected fc 5, got {point2.fc}"
        assert point2.offset == "0x0010", f"Point 2: Expected offset '0x0010', got {point2.offset}"
        assert point2.iec_location == "%QX0.0", f"Point 2: Expected iec_location '%QX0.0', got {point2.iec_location}"
        assert point2.length == 1, f"Point 2: Expected length 1, got {point2.length}"
        
        print("Successfully loaded and validated ModbusMasterConfig from valid data.")
        return True
    except Exception as e:
        print(f"Error in test_modbus_config_from_valid_dict: {e}")
        return False
    finally:
        if tmp_file_path and os.path.exists(tmp_file_path):
            os.remove(tmp_file_path)

def test_modbus_config_from_file():
    """Test ModbusMasterConfig by loading from an actual modbus_master.json file."""
    # Determine the path to modbus_master.json relative to this test script
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    # Path: core/src/drivers/plugins/python/shared/plugin_config_decode/test_plugin_config_models.py
    # To: core/src/drivers/plugins/python/modbus_master/modbus_master.json
    config_file_path = os.path.join(current_script_dir, "../../modbus_master/modbus_master.json")
    
    print(f"\n--- Testing ModbusMasterConfig from file: {config_file_path} ---")
    if not os.path.exists(config_file_path):
        print(f"Test file not found: {config_file_path}. Skipping file parsing test.")
        return True # Not a failure of the model, but a missing test dependency

    try:
        config_instance = ModbusMasterConfig(config_path="dummy_init_path_for_file_test")
        config_instance.from_json_file(config_file_path)

        print(f"Successfully parsed from file: {config_file_path}")
        print(f"Name: {config_instance.name}")
        print(f"Protocol: {config_instance.protocol}")
        print(f"Type: {config_instance.type}")
        print(f"Host: {config_instance.host}")
        print(f"Port: {config_instance.port}")
        print(f"Cycle Time (ms): {config_instance.cycle_time_ms}")
        print(f"Timeout (ms): {config_instance.timeout_ms}")
        print("I/O Points:")
        for point in config_instance.io_points:
            print(f"    {point}")

        # Basic checks, assuming we know the content of a typical modbus_master.json
        assert config_instance.name != "UNDEFINED", "Name should be parsed from file."
        assert config_instance.protocol == "MODBUS", "Protocol should be MODBUS."
        assert config_instance.host != "UNDEFINED", "Host should be parsed from file."
        assert isinstance(config_instance.port, int) and config_instance.port > 0, "Port should be a valid integer."
        assert isinstance(config_instance.io_points, list), "io_points should be a list."

        print("Successfully validated ModbusMasterConfig from file.")
        return True
    except FileNotFoundError:
        print(f"Test file not found at expected path: {config_file_path}. Skipping file parsing test.")
        return True
    except Exception as e:
        print(f"Error in test_modbus_config_from_file: {e}")
        return False

def test_modbus_io_point_config_from_dict():
    """Test ModbusIoPointConfig.from_dict() with valid and invalid data."""
    print("\n--- Testing ModbusIoPointConfig.from_dict ---")
    valid_point_data = {
        "fc": 3,
        "offset": "0x0100",
        "iec_location": "%IW10",
        "len": 10
    }
    try:
        point = ModbusIoPointConfig.from_dict(valid_point_data)
        assert point.fc == 3
        assert point.offset == "0x0100"
        assert point.iec_location == "%IW10"
        assert point.length == 10
        print("Successfully created ModbusIoPointConfig from valid dict.")
    except Exception as e:
        print(f"Failed to create ModbusIoPointConfig from valid dict: {e}")
        return False

    invalid_point_data_missing_key = {
        "fc": 3,
        "offset": "0x0100",
        # "iec_location": "%IW10", # Missing
        "len": 10
    }
    try:
        ModbusIoPointConfig.from_dict(invalid_point_data_missing_key)
        print("ERROR: Should have failed for missing 'iec_location' key.")
        return False
    except ValueError as e:
        print(f"Successfully caught ValueError for missing key: {e}")
    except Exception as e:
        print(f"Caught unexpected error for missing key: {e}")
        return False
    
    invalid_point_data_wrong_key_name = { # ModbusIoPointConfig expects "len", not "length"
        "fc": 3,
        "offset": "0x0100",
        "iec_location": "%IW10",
        "length": 10 # Should be "len"
    }
    try:
        ModbusIoPointConfig.from_dict(invalid_point_data_wrong_key_name)
        print("ERROR: Should have failed for wrong key name 'length' instead of 'len'.")
        return False
    except ValueError as e:
        print(f"Successfully caught ValueError for wrong key name 'length': {e}")
    except Exception as e:
        print(f"Caught unexpected error for wrong key name 'length': {e}")
        return False
        
    return True


def test_modbus_config_error_handling():
    """Test ModbusMasterConfig error handling with invalid files or data."""
    print("\n--- Testing ModbusMasterConfig Error Handling ---")
    
    config_instance = ModbusMasterConfig(config_path="dummy_path_for_error_tests")

    # Test with a non-existent file
    try:
        config_instance.from_json_file("non_existent_file.json")
        print("ERROR: Should have raised FileNotFoundError for non-existent file.")
        return False
    except FileNotFoundError:
        print("Successfully caught FileNotFoundError for non-existent file.")
    except Exception as e:
        print(f"Caught unexpected error for non-existent file: {e}")
        return False

    # Test with malformed JSON
    malformed_json_content = '{"name": "test", "protocol": "MODBUS", "config": {"type": "MASTER", "host": "localhost"' # Missing closing braces
    tmp_file_path = None # Initialize
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".json") as tmp_file:
        tmp_file.write(malformed_json_content)
        tmp_file_path = tmp_file.name
    
    try:
        config_instance.from_json_file(tmp_file_path)
        print("ERROR: Should have raised json.JSONDecodeError for malformed JSON.")
        return False
    except json.JSONDecodeError:
        print("Successfully caught json.JSONDecodeError for malformed JSON.")
    except Exception as e:
        print(f"Caught unexpected error for malformed JSON: {e}")
        return False
    finally:
        if tmp_file_path and os.path.exists(tmp_file_path):
            os.remove(tmp_file_path)

    # Test with JSON missing top-level keys (e.g., "config")
    # Current ModbusMasterConfig.from_json_file uses .get() with defaults, so it shouldn't fail.
    json_missing_config_key = '{"name": "test_device", "protocol": "MODBUS"}'
    tmp_file_path_missing_config = None # Initialize
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".json") as tmp_file:
        tmp_file.write(json_missing_config_key)
        tmp_file_path_missing_config = tmp_file.name
    try:
        config_instance.from_json_file(tmp_file_path_missing_config)
        # Expect default values to be used
        assert config_instance.name == "test_device"
        assert config_instance.config == {} # Default from .get()
        assert config_instance.type == "UNDEFINED" # Default from .get() on self.config
        print("Successfully handled JSON missing 'config' key by using defaults.")
    except Exception as e:
        print(f"Unexpected error when handling JSON missing 'config' key: {e}")
        return False
    finally:
        if tmp_file_path_missing_config and os.path.exists(tmp_file_path_missing_config):
            os.remove(tmp_file_path_missing_config)
    
    return True

def main():
    """Main test function."""
    print("=== Updated Plugin Configuration Models Test Suite ===\n")
    
    test_results = []
    
    # Run tests
    test_results.append(("Modbus Config from Valid Dict", test_modbus_config_from_valid_dict()))
    test_results.append(("Modbus Config from File", test_modbus_config_from_file()))
    test_results.append(("Modbus IO Point Config from Dict", test_modbus_io_point_config_from_dict()))
    test_results.append(("Modbus Config Error Handling", test_modbus_config_error_handling()))
    
    # Print results summary
    print("\n=== Test Results Summary ===")
    all_passed = True
    for test_name, result in test_results:
        status = "PASSED" if result else "FAILED"
        print(f"{test_name}: {status}")
        if not result:
            all_passed = False
    
    print(f"\nOverall Result: {'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")
    print("\n--- End of Tests ---")
    
    return all_passed

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
