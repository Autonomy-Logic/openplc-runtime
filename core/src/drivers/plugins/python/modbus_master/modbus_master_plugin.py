import sys
import os
import json
import traceback

# Add the parent directory to Python path to find shared module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import the correct type definitions
from shared.python_plugin_types import (
    PluginRuntimeArgs, 
    safe_extract_runtime_args_from_capsule,
    SafeBufferAccess,
    PluginStructureValidator
)

# Import the configuration model
from shared.plugin_config_decode.modbus_master_config_model import ModbusMasterConfig

# Global variables for plugin lifecycle and configuration
runtime_args = None
modbus_master_config: ModbusMasterConfig = None
safe_buffer_accessor: SafeBufferAccess = None

def init(args_capsule):
    """
    Initialize the Modbus Master plugin.
    This function receives the arguments encapsulated by the runtime,
    extracts them, and makes them globally available.
    It also handles parsing the settings from the configuration file.
    """
    global runtime_args, modbus_master_config, safe_buffer_accessor

    print("[MODBUS_MASTER] Python plugin 'modbus_master_plugin' initializing...")

    try:
        # 1. Extract runtime args from capsule using safe method
        print("[MODBUS_MASTER] Attempting to extract runtime arguments...")
        if hasattr(args_capsule, '__class__') and 'PyCapsule' in str(type(args_capsule)):
            # This is a PyCapsule from C - use safe extraction
            runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
            if runtime_args is None:
                print(f"[MODBUS_MASTER] ✗ Failed to extract runtime args: {error_msg}")
                return False
            
            print(f"[MODBUS_MASTER] ✓ Runtime arguments extracted successfully.")
        else:
            # This is a direct object (for testing)
            runtime_args = args_capsule
            print(f"[MODBUS_MASTER] ✓ Using direct runtime args for testing.")

        # 2. Create SafeBufferAccess instance for global use
        print("[MODBUS_MASTER] Creating SafeBufferAccess instance...")
        safe_buffer_accessor = SafeBufferAccess(runtime_args)
        if not safe_buffer_accessor.is_valid:
            print(f"[MODBUS_MASTER] ✗ Failed to create SafeBufferAccess: {safe_buffer_accessor.error_msg}")
            return False
        print(f"[MODBUS_MASTER] ✓ SafeBufferAccess instance created.")

        # 3. Load and parse the configuration file
        print("[MODBUS_MASTER] Attempting to load configuration file...")
        config_file_path = None
        try:
            # Try to get the config file path from runtime_args
            # Assuming plugin_specific_config_file_path is an attribute of runtime_args
            # or accessible via SafeBufferAccess.
            # The modbus_slave example uses SafeBufferAccess(runtime_args).get_config_file_args_as_map()
            # which suggests the path might be embedded or accessed this way.
            # However, ModbusMasterConfig expects a direct file path.
            
            # Let's check if runtime_args has a direct attribute for config path first.
            if hasattr(runtime_args, 'plugin_specific_config_file_path'):
                config_file_path = runtime_args.plugin_specific_config_file_path
            else:
                # If not directly on runtime_args
                print("[MODBUS_MASTER] ⚠ Plugin-specific config file path not found directly in runtime_args.")
                # Fallback to a default path if map loading fails or is empty
                current_dir = os.path.dirname(os.path.abspath(__file__))
                default_config_path = os.path.join(current_dir, "modbus_master.json")
                print(f"[MODBUS_MASTER] Falling back to default config path: {default_config_path}")
                config_file_path = default_config_path


            if not config_file_path or not os.path.exists(config_file_path):
                print(f"[MODBUS_MASTER] ✗ Configuration file not found or path is invalid: {config_file_path}")
                return False

            print(f"[MODBUS_MASTER] ✓ Configuration file path: {config_file_path}")
            
            # Initialize ModbusMasterConfig and load from JSON file
            modbus_master_config = ModbusMasterConfig()
            modbus_master_config.import_config_from_file(file_path=config_file_path)
            
            # Validate the loaded configuration
            modbus_master_config.validate()
            
            print(f"[MODBUS_MASTER] ✓ Configuration loaded and validated successfully.")
            print(f"[MODBUS_MASTER]   Plugin Name: {modbus_master_config.name}")
            print(f"[MODBUS_MASTER]   Protocol: {modbus_master_config.protocol}")
            print(f"[MODBUS_MASTER]   Target Host: {modbus_master_config.host}")
            print(f"[MODBUS_MASTER]   Target Port: {modbus_master_config.port}")
            print(f"[MODBUS_MASTER]   Cycle Time: {modbus_master_config.cycle_time_ms}ms")
            print(f"[MODBUS_MASTER]   Timeout: {modbus_master_config.timeout_ms}ms")
            print(f"[MODBUS_MASTER]   Number of I/O Points: {len(modbus_master_config.io_points)}")
            for i, point in enumerate(modbus_master_config.io_points):
                print(f"[MODBUS_MASTER]     I/O Point {i+1}: FC={point.fc}, Offset='{point.offset}', IEC_Loc='{point.iec_location}', Len={point.length}")


        except FileNotFoundError:
            print(f"[MODBUS_MASTER] ✗ Configuration file not found: {config_file_path}")
            return False
        except json.JSONDecodeError as e:
            print(f"[MODBUS_MASTER] ✗ Error decoding JSON configuration: {e}")
            if config_file_path:
                print(f"[MODBUS_MASTER]   File path: {config_file_path}")
            return False
        except ValueError as e: # Catch validation errors from ModbusMasterConfig
            print(f"[MODBUS_MASTER] ✗ Configuration validation error: {e}")
            return False
        except Exception as config_error:
            print(f"[MODBUS_MASTER] ✗ Unexpected error during configuration loading: {config_error}")
            traceback.print_exc()
            return False

        # 4. Optional: Further initialization based on config and runtime_args
        # For example, initializing Modbus client connections, etc.
        # This will likely go into start_loop or be called from here if needed for init.
        print("[MODBUS_MASTER] ✓ Plugin initialization sequence completed.")

        return True

    except Exception as e:
        print(f"[MODBUS_MASTER] ✗ Plugin initialization failed with an unhandled exception: {e}")
        traceback.print_exc()
        return False

def start_loop():
    """Start the Modbus Master communication loop."""
    global runtime_args, modbus_master_config, safe_buffer_accessor

    if runtime_args is None or modbus_master_config is None or safe_buffer_accessor is None:
        print("[MODBUS_MASTER] Error: Plugin not initialized. Call init() first.")
        return False

    print("[MODBUS_MASTER] Starting Modbus Master communication loop...")
    # Placeholder for Modbus Master client logic
    # - Connect to configured slave(s) (modbus_master_config.host, modbus_master_config.port)
    # - Loop based on modbus_master_config.cycle_time_ms
    # - For each io_point in modbus_master_config.io_points:
    #   - Read/write data via Modbus based on point.fc, point.offset, point.length
    #   - Use safe_buffer_accessor to map data to/from OpenPLC buffers using point.iec_location
    print("[MODBUS_MASTER] Modbus Master loop started (placeholder).")
    return True

def stop_loop():
    """Stop the Modbus Master communication loop."""
    print("[MODBUS_MASTER] Stopping Modbus Master communication loop...")
    # Placeholder for stopping logic
    # - Close Modbus connections
    # - Stop any running threads/tasks
    print("[MODBUS_MASTER] Modbus Master loop stopped (placeholder).")
    return True

def cleanup():
    """Cleanup plugin resources."""
    global runtime_args, modbus_master_config, safe_buffer_accessor
    print("[MODBUS_MASTER] Cleaning up plugin resources...")
    runtime_args = None
    modbus_master_config = None
    safe_buffer_accessor = None
    print("[MODBUS_MASTER] Plugin resources cleaned up.")
    return True

if __name__ == "__main__":
    print("Modbus Master Plugin - Standalone Test Mode")
    
    # Create a mock runtime_args for testing
    class MockRuntimeArgs:
        def __init__(self, config_path):
            self.plugin_specific_config_file_path = config_path # Simulate C providing path
            self.buffer_size = 1024 # Example value
            self.bits_per_buffer = 8 # Example value
            # Mock other attributes that SafeBufferAccess might expect if it directly inspects runtime_args
            # beyond what's needed for get_config_file_args_as_map or direct path access.
            self.bool_output = None 
            self.bool_input = None
            self.int_output = None
            self.int_input = None
            self.buffer_mutex = None # Mock mutex

        def safe_access_buffer_size(self):
            return self.buffer_size, "Success"

        def validate_pointers(self):
            return True, "Mock validation passed"

        def __str__(self):
            return f"MockRuntimeArgs(config_path='{self.plugin_specific_config_file_path}')"

    # Determine the path to the modbus_master.json for testing
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    test_config_path = os.path.join(current_script_dir, "modbus_master.json")
    
    mock_args = MockRuntimeArgs(config_path=test_config_path)
    
    print(f"Attempting to initialize with mock args and config: {test_config_path}")
    
    if init(mock_args):
        print("Init successful.")
        if start_loop():
            print("Start loop successful.")
            # Simulate running for a bit
            import time
            print("Running for 2 seconds...")
            time.sleep(2)
            if stop_loop():
                print("Stop loop successful.")
            else:
                print("Stop loop failed.")
        else:
            print("Start loop failed.")
        
        if cleanup():
            print("Cleanup successful.")
        else:
            print("Cleanup failed.")
    else:
        print("Init failed.")
