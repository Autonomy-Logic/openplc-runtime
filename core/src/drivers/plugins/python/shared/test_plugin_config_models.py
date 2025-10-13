#!/usr/bin/env python3
"""
Test suite for Plugin Configuration Models
Extracted from plugin_config_models.py for better code organization.
"""

import os
import sys

# Add current directory to path for imports
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from plugin_config_models import (
    PluginConfigParser,
    ModbusTcpConfig
)
from base_protocol_config import (
    PluginConfigError,
    ModbusIoPointConfig
)

def test_json_string_parsing():
    """Test parsing from JSON string with example data."""
    example_json_data_str = """
    [
      {
        "name": "device_1",
        "protocol": "MODBUS",
        "config": {
          "type": "SLAVE",
          "host": "172.29.65.104",
          "port": 5024,
          "cycle_time_ms": 100,
          "timeout_ms": 1000,
          "io_points": [
            {
              "fc": 5,
              "offset": "0x0000",
              "iec_location": "%QW10",
              "len": 5
            },
            {
              "fc": 2,
              "offset": "0x0000",
              "iec_location": "%IX0.0",
              "len": 16
            }
          ]
        }
      },
      {
        "name": "device_2",
        "protocol": "MODBUS",
        "config": {
          "type": "SLAVE",
          "host": "172.29.66.104",
          "port": 5024,
          "cycle_time_ms": 100,
          "timeout_ms": 1000,
          "io_points": [
            {
              "fc": 2,
              "offset": "0x0000",
              "iec_location": "%IX0.0",
              "len": 5
            }
          ]
        }
      }
    ]
    """

    print("--- Testing Plugin Configuration Models ---")

    try:
        print("\nAttempting to parse example JSON string...")
        parsed_configs = PluginConfigParser.parse_from_json_string(example_json_data_str)
        print("Successfully parsed JSON string.\n")

        for i, config in enumerate(parsed_configs):
            print(f"--- Configuration {i+1} ---")
            print(f"Name: {config.name}")
            print(f"Protocol: {config.protocol}")
            print("Protocol Specific Config:")
            print(f"  Type: {config.protocol_config.type}")
            print(f"  Host: {config.protocol_config.host}")
            print(f"  Port: {config.protocol_config.port}")
            print(f"  Cycle Time (ms): {config.protocol_config.cycle_time_ms}")
            print(f"  Timeout (ms): {config.protocol_config.timeout_ms}")
            print("  I/O Points:")
            for point in config.protocol_config.io_points:
                print(f"    {point}")
            print("-" * 25)

        return True
    except PluginConfigError as e:
        print(f"Failed to parse configuration: {e}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return False

def test_file_parsing():
    """Test parsing from JSON file."""
    # Test with the provided modbus_master.json file path
    # This assumes the script is run from a context where the path is accessible
    # For example, if this script is in 'core/src/drivers/plugins/python/shared/'
    # and modbus_master.json is in 'core/src/drivers/plugins/python/modbus_master/'
    config_file_path = "../modbus_master/modbus_master.json"
    print(f"\n--- Attempting to parse from file: {config_file_path} ---")
    file_to_parse = config_file_path  # Initialize with a default path
    
    try:
        # Adjust path if running from a different directory structure
        # For robust path handling, consider using pathlib or os.path.join
        # For this example, assuming a relative path from where this script might be run.
        # If running from /home/marcone/Documents/Github/openplc-runtime/core/src/drivers/plugins/python/shared/
        # then ../modbus_master/modbus_master.json is correct.
        
        # For a more robust test if this script is run from openplc-runtime root:
        # Assuming this script is in core/src/drivers/plugins/python/shared/
        current_dir = os.path.dirname(os.path.abspath(__file__))
        file_to_parse = os.path.join(current_dir, "../modbus_master/modbus_master.json")
        
        if not os.path.exists(file_to_parse):
            # Fallback if __file__ is not reliable or structure is different
            # This might happen if running in an environment where __file__ is not set (e.g. some interactive shells)
            # Or if the script's location relative to the json is different.
            # For the provided structure, this should work.
            print(f"Warning: Could not find config at {file_to_parse}, trying fallback path: {config_file_path}")
            file_to_parse = config_file_path  # Use the simpler relative path as a fallback

        parsed_file_configs = PluginConfigParser.parse_from_json_file(file_to_parse)
        print(f"Successfully parsed from file: {file_to_parse}\n")

        for i, config in enumerate(parsed_file_configs):
            print(f"--- File Configuration {i+1} ---")
            print(f"Name: {config.name}")
            print(f"Protocol: {config.protocol}")
            print("Protocol Specific Config:")
            if isinstance(config.protocol_config, ModbusTcpConfig):
                modbus_config: ModbusTcpConfig = config.protocol_config
                print(f"  Type: {modbus_config.type}")
                print(f"  Host: {modbus_config.host}")
                print(f"  Port: {modbus_config.port}")
                print(f"  Cycle Time (ms): {modbus_config.cycle_time_ms}")
                print(f"  Timeout (ms): {modbus_config.timeout_ms}")
                print("  I/O Points:")
                for point in modbus_config.io_points:
                    print(f"    {point}")
            else:
                print(f"  Unknown protocol config type: {type(config.protocol_config)}")
            print("-" * 25)

        return True
    except PluginConfigError as e:
        print(f"Failed to parse configuration from file: {e}")
        return False
    except FileNotFoundError:
        print(f"Test file not found at expected path: {file_to_parse}. Skipping file parsing test.")
        return True  # Not a failure, just missing test file
    except Exception as e:
        print(f"An unexpected error occurred during file parsing: {e}")
        return False

def test_error_handling():
    """Test error handling with invalid configurations."""
    print("\n--- Testing Error Handling ---")
    
    # Test missing 'config' field
    invalid_json_missing_field = '{"name": "test", "protocol": "MODBUS"}'  # Missing "config"
    try:
        PluginConfigParser.parse_from_json_string(invalid_json_missing_field)
        print("ERROR: Should have caught missing 'config' field error")
        return False
    except PluginConfigError as e:
        print(f"Caught expected error for missing 'config' field: {e}")

    # Test unsupported protocol
    invalid_json_bad_protocol = '{"name": "test", "protocol": "DNE", "config": {}}'
    try:
        PluginConfigParser.parse_from_json_string(invalid_json_bad_protocol)
        print("ERROR: Should have caught unsupported protocol error")
        return False
    except PluginConfigError as e:
        print(f"Caught expected error for unsupported protocol 'DNE': {e}")
    
    # Test invalid Modbus config
    invalid_modbus_config = '{"name": "test", "protocol": "MODBUS", "config": {"type": "MASTER", "host": "localhost"}}'
    try:
        PluginConfigParser.parse_from_json_string(invalid_modbus_config)
        print("ERROR: Should have caught invalid Modbus type error")
        return False
    except PluginConfigError as e:
        print(f"Caught expected error for invalid Modbus 'type': {e}")

    return True

def main():
    """Main test function."""
    print("=== Plugin Configuration Models Test Suite ===\n")
    
    test_results = []
    
    # Run tests
    test_results.append(("JSON String Parsing", test_json_string_parsing()))
    test_results.append(("File Parsing", test_file_parsing()))
    test_results.append(("Error Handling", test_error_handling()))
    
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