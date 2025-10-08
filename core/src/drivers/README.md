# OpenPLC Runtime Plugin System

This directory contains the OpenPLC Runtime plugin system, which allows extending the runtime with custom drivers and communication protocols. **Currently, the system actively supports Python plugins. Support for native C plugins is planned for a future release.**

## Overview

The plugin system provides a flexible architecture for integrating external hardware drivers, communication protocols, and custom logic into the OpenPLC Runtime. It offers thread-safe access to OpenPLC I/O buffers.

**Current Status:**
*   **Supported:** Python plugins (`.py` files) are fully supported and operational.
*   **Planned:** Native C plugins (`.so` files) are part of the design and API but are not yet implemented or functional. All references to native C plugins in this document describe the intended future functionality.

## Architecture

### Core Components

```
core/src/drivers/
├── plugin_driver.c/h          # Main plugin driver system
├── plugin_config.c/h          # Configuration file parsing
├── python_plugin_bridge.c/h   # Python plugin integration
├── CMakeLists.txt             # Build configuration
├── plugins/python/            # Python plugin implementations
│   ├── examples/              # Python plugin examples
│   ├── modbus_slave/          # Modbus TCP slave Python plugin
│   └── shared/                # Shared Python modules (e.g., type definitions)
└── *.py                      # Standalone Python plugin files (if any)
```

### Plugin Types

1.  **Python Plugins** (`PLUGIN_TYPE_PYTHON = 0`) - **Currently Supported**
    *   Python scripts (`.py` files).
    *   Embedded Python interpreter.
    *   Easier development and debugging.
    *   Enhanced type safety and buffer access with `python_plugin_types.py`.

2.  **Native C Plugins** (`PLUGIN_TYPE_NATIVE = 1`) - **Future Support**
    *   Compiled shared libraries (`.so` files).
    *   Direct C function calls.
    *   Maximum performance (intended).
    *   *Note: This plugin type is defined in the API but not yet implemented.*

## Plugin Interface

### Required Functions

All plugins must implement these core functions. The lifecycle (`init`, `start_loop`, `stop_loop`, `cleanup`) is managed by the plugin driver.

#### Python Plugins (Currently Supported)
```python
def init(runtime_args_capsule):
    """
    Initialize plugin with runtime arguments.
    Args:
        runtime_args_capsule: PyCapsule containing plugin_runtime_args_t.
    Returns:
        bool: True if initialization successful, False otherwise.
    """
    pass

# Optional functions, but recommended for full lifecycle management
def start_loop():
    """Called when plugin should start its main operations (e.g., start a server)."""
    pass

def stop_loop():
    """Called when plugin should stop its main operations (e.g., stop a server)."""
    pass

def cleanup():
    """Called when plugin is being unloaded; release all resources."""
    pass
```

#### Native C Plugins (Future Support - API Defined)
```c
// Mandatory initialization function
int init(plugin_runtime_args_t *args);

// Optional lifecycle functions
void start_loop(void);
void stop_loop(void);
void run_cycle(void);

// Mandatory cleanup function
void cleanup(void);
```
*Note: The C API is defined but the loading and execution mechanism for native plugins is not yet implemented.*

### Runtime Arguments Structure

Plugins receive access to OpenPLC buffers through the `plugin_runtime_args_t` structure, passed as a PyCapsule to Python plugins:

```c
typedef struct {
    // I/O Buffer pointers
    IEC_BOOL *(*bool_input)[8];     // Digital inputs
    IEC_BOOL *(*bool_output)[8];    // Digital outputs
    IEC_BYTE **byte_input;          // Byte inputs
    IEC_BYTE **byte_output;         // Byte outputs
    IEC_UINT **int_input;           // 16-bit integer inputs
    IEC_UINT **int_output;          // 16-bit integer outputs
    IEC_UDINT **dint_input;         // 32-bit integer inputs
    IEC_UDINT **dint_output;        // 32-bit integer outputs
    IEC_ULINT **lint_input;         // 64-bit integer inputs
    IEC_ULINT **lint_output;        // 64-bit integer outputs
    IEC_UINT **int_memory;          // Internal memory (16-bit)
    IEC_UDINT **dint_memory;        // Internal memory (32-bit)
    IEC_ULINT **lint_memory;        // Internal memory (64-bit)
    
    // Thread synchronization
    int (*mutex_take)(pthread_mutex_t *mutex);
    int (*mutex_give)(pthread_mutex_t *mutex);
    pthread_mutex_t *buffer_mutex;
    
    // Buffer metadata
    int buffer_size;                // Number of buffers
    int bits_per_buffer;           // Bits per boolean buffer (typically 8)
} plugin_runtime_args_t;
```

## Thread-Safe Buffer Access

### Enhanced Python SafeBufferAccess (Recommended)

The `plugins/python/shared/python_plugin_types.py` module provides a `SafeBufferAccess` wrapper class for robust and safe buffer operations. This is the recommended way to interact with OpenPLC buffers.

```python
from plugins.python.shared.python_plugin_types import SafeBufferAccess, safe_extract_runtime_args_from_capsule

def init(runtime_args_capsule):
    # Safely extract runtime arguments from the PyCapsule
    runtime_args, error_msg = safe_extract_runtime_args_from_capsule(runtime_args_capsule)
    if runtime_args is None:
        print(f"Failed to extract runtime args: {error_msg}")
        return False

    # Create safe buffer access wrapper
    safe_buffer = SafeBufferAccess(runtime_args)
    if not safe_buffer.is_valid:
        print(f"Failed to create SafeBufferAccess: {safe_buffer.error_msg}")
        return False
        
    global safe_access
    safe_access = safe_buffer
    return True

def example_use():
    # Safe read operation from a boolean output buffer
    value, error_msg = safe_access.safe_read_bool_output(buffer_idx=0, bit_idx=0)
    if error_msg == "Success":
        print(f"Read value: {value}")
    else:
        print(f"Read error: {error_msg}")

    # Safe write operation to a boolean output buffer
    success, error_msg = safe_access.safe_write_bool_output(buffer_idx=0, bit_idx=0, True)
    if error_msg == "Success":
        print("Write successful")
    else:
        print(f"Write error: {error_msg}")
```

### Manual Python Example (Legacy/For Understanding)
While `SafeBufferAccess` is recommended, understanding the underlying mutex operations is useful:
```python
def manual_safe_read_output(runtime_args, buffer_idx, bit_pos):
    """Manually and safely read a boolean output."""
    try:
        if runtime_args.mutex_take(runtime_args.buffer_mutex) == 0:
            value = runtime_args.bool_output[buffer_idx][bit_pos].contents.value
            return bool(value)
    finally:
        runtime_args.mutex_give(runtime_args.buffer_mutex)
    return False # Default or error value

def manual_safe_write_output(runtime_args, buffer_idx, bit_pos, value):
    """Manually and safely write a boolean output."""
    try:
        if runtime_args.mutex_take(runtime_args.buffer_mutex) == 0:
            runtime_args.bool_output[buffer_idx][bit_pos].contents.value = 1 if value else 0
            return True
    finally:
        runtime_args.mutex_give(runtime_args.buffer_mutex)
    return False
```

## Configuration

### Plugin Configuration File Format (e.g., `plugins.conf`)

Plugins are configured via a text file, typically `plugins.conf`, located in the project root. Each line defines a plugin:

```
# Format: name,path,enabled,type,plugin_related_config_path
# Example for a Python Modbus Slave plugin:
modbus_slave,./core/src/drivers/plugins/python/modbus_slave/simple_modbus.py,1,0,./core/src/drivers/plugins/python/modbus_slave/modbus_slave_config.json
# Example for a custom Python plugin:
my_custom_plugin,./core/src/drivers/plugins/python/examples/my_custom_plugin.py,1,0,./my_custom_plugin_config.ini
# Example for a future Native C plugin (not yet supported):
# future_native_plugin,./plugins/native/example.so,1,1,./config/example.conf
```

**Fields:**
*   `name`: A unique identifier for the plugin.
*   `path`: Path to the plugin file (`.py` for Python, `.so` for native C).
*   `enabled`: `1` for enabled, `0` for disabled.
*   `type`: `0` for Python (`PLUGIN_TYPE_PYTHON`), `1` for Native C (`PLUGIN_TYPE_NATIVE`). **Currently, only `0` is functional.**
*   `plugin_related_config_path`: (Optional) Path to a plugin-specific configuration file (e.g., `.ini`, `.json`, `.conf`).

### Loading Configuration in Code
The configuration is loaded and managed by the plugin driver:
```c
#include "plugin_driver.h"

// Create, load config, initialize, and start the plugin system
plugin_driver_t *driver = plugin_driver_create();
if (driver) {
    if (plugin_driver_load_config(driver, "plugins.conf") == 0) {
        if (plugin_driver_init(driver) == 0) {
            plugin_driver_start(driver); // Starts enabled plugins
        }
    }
    // Remember to stop and destroy the driver when shutting down
    // plugin_driver_stop(driver);
    // plugin_driver_destroy(driver);
}
```

## Examples

### 1. Basic Python Plugin Template

See `plugins/python/examples/example_python_plugin.py` for a foundational template demonstrating:
*   Plugin initialization with `safe_extract_runtime_args_from_capsule`.
*   Using `SafeBufferAccess` for I/O.
*   Basic lifecycle management (`init`, `cleanup`).

### 2. Modbus TCP Slave (Python)

The `plugins/python/modbus_slave/simple_modbus.py` provides a comprehensive implementation of a Modbus TCP slave server, mapping OpenPLC I/O points to Modbus registers/coils.

**Features:**
*   Maps `bool_input`/`bool_output` to Modbus Discrete Inputs and Coils.
*   Maps `int_input`/`int_output` (and potentially `dint_*`, `lint_*`) to Modbus Input and Holding Registers.
*   Supports standard Modbus function codes (01, 02, 03, 04, 05, 06, 0F, 10).
*   Full asynchronous operation using `pymodbus` and `asyncio`.
*   Thread-safe buffer access via `SafeBufferAccess`.
*   Configurable via a JSON file (e.g., `modbus_slave_config.json`).

**Configuration Example (`modbus_slave_config.json`):**
```json
{
  "host": "0.0.0.0",
  "port": 5020,
  "max_coils": 8000,
  "max_discrete_inputs": 8000,
  "max_holding_registers": 8000,
  "max_input_registers": 8000,
  "buffer_mappings": {
    "coils_start_buffer": 0,
    "coils_start_bit": 0,
    "discrete_inputs_start_buffer": 0,
    "discrete_inputs_start_bit": 0,
    "holding_registers_start_buffer": 0,
    "input_registers_start_buffer": 0
  }
}
```

**Usage:**
The plugin is typically loaded and started automatically by the OpenPLC runtime if configured in `plugins.conf`.
For standalone testing:
```bash
python3 ./core/src/drivers/plugins/python/modbus_slave/simple_modbus.py
```

## Python Plugin Type System and Safety

### Enhanced Type Safety with `python_plugin_types.py`

The `plugins/python/shared/python_plugin_types.py` module is crucial for developing robust Python plugins. It provides:

#### Key Components

1.  **`PluginRuntimeArgs` (ctypes Structure)**
    *   An exact `ctypes` mapping of the C `plugin_runtime_args_t` structure.
    *   Includes a `safe_access_buffer_size()` method for validated buffer size retrieval.

2.  **`SafeBufferAccess` Wrapper**
    *   The primary interface for thread-safe I/O buffer operations.
    *   Handles `mutex_take`/`mutex_give` automatically.
    *   Provides clear error messages for invalid access attempts (e.g., out of bounds, null pointers).

3.  **`PluginStructureValidator`**
    *   Utilities for debugging, such as `print_structure_info()` to verify `ctypes` structure alignment and sizes against the C definitions.

4.  **`safe_extract_runtime_args_from_capsule(runtime_args_capsule)`**
    *   The **recommended** function to extract the `plugin_runtime_args_t` pointer from the `PyCapsule` passed to the `init` function.
    *   Performs comprehensive error checking (capsule validity, name, null pointer) and returns a tuple `(runtime_args_ptr, error_message)`.

#### Usage Example (Reiterated from SafeBufferAccess)
```python
from plugins.python.shared.python_plugin_types import (
    PluginRuntimeArgs,  # For type hinting or direct use if extraction is manual
    safe_extract_runtime_args_from_capsule,
    SafeBufferAccess,
    PluginStructureValidator
)

def init(runtime_args_capsule):
    global _safe_buffer_access

    # Optional: Print structure info for debugging during development
    # PluginStructureValidator.print_structure_info()

    # Safely extract runtime args from capsule
    runtime_args, error_msg = safe_extract_runtime_args_from_capsule(runtime_args_capsule)
    if runtime_args is None:
        print(f"[Plugin Error] Initialization failed: {error_msg}")
        return False

    # Create the safe buffer access wrapper
    _safe_buffer_access = SafeBufferAccess(runtime_args)
    if not _safe_buffer_access.is_valid:
        print(f"[Plugin Error] Failed to initialize SafeBufferAccess: {_safe_buffer_access.error_msg}")
        return False
    
    print("Plugin initialized successfully.")
    return True

# ... other functions (start_loop, run_cycle, cleanup) can use _safe_buffer_access ...
```

## Development Guide

### Creating a Python Plugin

1.  **Create your plugin file**, e.g., `my_driver.py`, in a suitable location like `plugins/python/` or a project-specific subdirectory.
    ```python
    #!/usr/bin/env python3
    from plugins.python.shared.python_plugin_types import (
        safe_extract_runtime_args_from_capsule,
        SafeBufferAccess
    )

    _safe_buffer_access = None

    def init(runtime_args_capsule):
        global _safe_buffer_access
        print("MyDriver: Initializing...")
        
        runtime_args, error_msg = safe_extract_runtime_args_from_capsule(runtime_args_capsule)
        if runtime_args is None:
            print(f"MyDriver Error: {error_msg}")
            return False
        
        _safe_buffer_access = SafeBufferAccess(runtime_args)
        if not _safe_buffer_access.is_valid:
            print(f"MyDriver Error: SafeBufferAccess init failed: {_safe_buffer_access.error_msg}")
            return False
        
        # Perform other initializations, e.g., loading config, setting up hardware
        print("MyDriver: Initialized successfully.")
        return True

    def start_loop():
        """Start plugin operations, e.g., a communication thread or server."""
        print("MyDriver: Starting operations...")
        # Example: if your plugin runs a server, start it here.
        # self.server_thread = threading.Thread(target=self._run_server)
        # self.server_thread.daemon = True
        # self.server_thread.start()
        pass

    def stop_loop():
        """Stop plugin operations."""
        print("MyDriver: Stopping operations...")
        # Example: signal your server/thread to stop and join it.
        pass

    def run_cycle():
        """Periodic task, if needed. Called by OpenPLC's main loop."""
        # Example: read sensors, update internal state, write to outputs
        # if _safe_buffer_access:
        #     sensor_val = read_hardware_sensor()
        #     _safe_buffer_access.safe_write_int_output(buffer_idx=0, value=sensor_val)
        pass

    def cleanup():
        """Release all resources held by the plugin."""
        print("MyDriver: Cleaning up...")
        # Ensure threads are stopped, files are closed, etc.
        # stop_loop() # Ensure loop is stopped if not already
        pass
    ```

2.  **Add your plugin to `plugins.conf`:**
    ```
    my_driver,./path/to/my_driver.py,1,0,./my_driver_config.json
    ```

3.  **Test your plugin:**
    *   Ensure OpenPLC is compiled with Python plugin support.
    *   Place `my_driver.py` and its config (if any) at the specified paths.
    *   Run OpenPLC. Check logs for "MyDriver: Initializing..." and "MyDriver: Initialized successfully."
    *   Test the functionality of your driver.

### Creating a Native C Plugin (Future Guide)

*This section outlines the intended process for when native C plugin support is implemented.*

1.  **Implement required functions** in a C file (e.g., `my_native_plugin.c`):
    ```c
    #include "plugin_driver.h" // Assuming this header will define the interface
    #include <stdio.h>
    #include <stdlib.h>

    static plugin_runtime_args_t *g_runtime_args = NULL;

    int init(plugin_runtime_args_t *args) {
        if (!args) return -1; // Error
        g_runtime_args = args;
        printf("Native C Plugin: Initialized\n");
        // Perform hardware initialization, etc.
        return 0; // Success
    }

    void start_loop(void) {
        printf("Native C Plugin: Starting operations\n");
        // Start threads, timers, etc.
    }

    void stop_loop(void) {
        printf("Native C Plugin: Stopping operations\n");
        // Signal threads to stop, etc.
    }

    void run_cycle(void) {
        // Example: safely read an input and write to an output
        if (g_runtime_args && g_runtime_args->buffer_mutex) {
            g_runtime_args->mutex_take(g_runtime_args->buffer_mutex);
            // Assuming buffer_size > 0 and buffers are valid
            IEC_UINT input_val = g_runtime_args->int_input[0][0]; // Read first word of first input buffer
            g_runtime_args->int_output[0][0] = input_val;       // Write to first word of first output buffer
            g_runtime_args->mutex_give(g_runtime_args->buffer_mutex);
        }
    }

    void cleanup(void) {
        printf("Native C Plugin: Cleaning up\n");
        // Release resources, close files, destroy threads.
        g_runtime_args = NULL;
    }
    ```

2.  **Compile as a shared library:**
    ```bash
    gcc -shared -fPIC -I<path_to_openPLC_core_includes> -o my_native_plugin.so my_native_plugin.c
    ```
    (The exact include paths and dependencies will be defined when native plugin support is developed).

3.  **Add to `plugins.conf` (for future use):**
    ```
    my_native_plugin,./plugins/my_native_plugin.so,1,1,./config/my_native_plugin.conf
    ```

## Buffer Mapping

The OpenPLC I/O memory is organized into buffers accessible by plugins.

### Boolean Buffers
*   `bool_input[BUFFER_SIZE][8]`: Digital inputs. Plugins typically read from these.
*   `bool_output[BUFFER_SIZE][8]`: Digital outputs. Plugins can read and write to these.
*   Each `bool_input[i]` or `bool_output[i]` is an array of 8 `IEC_BOOL` values.
*   Total boolean I/O capacity: `BUFFER_SIZE * 8`.
*   Access: `bool_output[buffer_index][bit_index]` where `bit_index` is 0-7.

### Integer Buffers
*   `int_input/int_output`: 16-bit unsigned integers (`IEC_UINT`).
*   `dint_input/dint_output`: 32-bit unsigned integers (`IEC_UDINT`).
*   `lint_input/lint_output`: 64-bit unsigned integers (`IEC_ULINT`).
*   `*_memory`: Internal memory areas of corresponding types.
*   Access: `int_output[buffer_index]` (accesses one `IEC_UINT` value).

### Modbus Mapping Example (as used in `simple_modbus.py`)
```
Modbus Coils (Function Code 0x01, 0x05, 0x0F)           -> bool_output[buffer_start + coil_offset / 8][coil_offset % 8]
Modbus Discrete Inputs (Function Code 0x02)             -> bool_input[buffer_start + di_offset / 8][di_offset % 8]
Modbus Holding Registers (Function Code 0x03, 0x06, 0x10) -> int_output/dint_output/lint_output[buffer_start + register_offset]
Modbus Input Registers (Function Code 0x04)             -> int_input/dint_input/lint_input[buffer_start + register_offset]
```
The exact mapping (`buffer_start`, data type sizing for registers) is configurable within plugins like the Modbus slave.

## API Reference (C Library)

These functions are part of the core plugin driver (`plugin_driver.c/h`).

### Plugin Driver Lifecycle

```c
// Create a new plugin driver instance
plugin_driver_t *plugin_driver_create(void);

// Load plugin configurations from a file
// Returns 0 on success, non-zero on error
int plugin_driver_load_config(plugin_driver_t *driver, const char *config_file);

// Initialize loaded plugins (calls their 'init' function)
// Returns 0 on success, non-zero if one or more plugins failed to init
int plugin_driver_init(plugin_driver_t *driver);

// Start initialized plugins (calls their 'start_loop' function)
// Returns 0 on success
int plugin_driver_start(plugin_driver_t *driver);

// Stop running plugins (calls their 'stop_loop' function)
// Returns 0 on success
int plugin_driver_stop(plugin_driver_t *driver);

// Destroy the plugin driver and free resources (calls 'cleanup' on plugins)
void plugin_driver_destroy(plugin_driver_t *driver);
```

*Note: Functions specific to generating arguments for native plugins (`generate_structured_args_with_driver`) and getting symbols from them (`python_plugin_get_symbols` are internal or for future use.)*

## Error Handling

### Common Issues

1.  **Plugin initialization fails (`init` returns `False`):**
    *   Check `plugins.conf` for correct paths and filenames.
    *   Verify Python syntax of your plugin file (`python3 -m py_compile your_plugin.py`).
    *   Check OpenPLC logs for error messages printed by your plugin or the Python bridge.
    *   Ensure `python_plugin_types.py` is accessible (usually in `plugins/python/shared/`).

2.  **Buffer access errors (e.g., "Index out of bounds", "Null pointer"):**
    *   Always use the `SafeBufferAccess` wrapper.
    *   Ensure `buffer_idx` and `bit_idx` are within valid ranges (0 to `buffer_size-1` and 0-7 respectively for booleans).
    *   The `SafeBufferAccess` methods return error messages; log and handle them.

3.  **Python import errors within a plugin:**
    *   Ensure all required Python packages are installed in the environment used by OpenPLC.
    *   If using local modules, ensure `PYTHONPATH` or relative imports are correct. The plugin system adds the plugin's directory to `sys.path`.

### Debugging Tips

1.  **Enable debug output in your plugin:**
    ```python
    import sys
    print(f"[MyPlugin Debug] __file__: {__file__}", file=sys.stderr) # Or to OpenPLC logs
    print(f"[MyPlugin Debug] sys.path: {sys.path}", file=sys.stderr)
    ```

2.  **Check Python plugin symbols (for advanced debugging):**
    ```bash
    # After starting OpenPLC, check if the plugin module loads
    python3 -c "
    import sys
    sys.path.append('./core/src/drivers/plugins/python/modbus_slave') # Example path
    import simple_modbus # Example plugin name
    print(dir(simple_modbus))
    "
    ```

3.  **Monitor buffer access:**
    ```python
    # Inside your plugin, wrap SafeBufferAccess calls with debug prints
    def debug_read_buffer(safe_access, b_idx, bit_idx=None):
        if bit_idx is not None:
            val, err = safe_access.safe_read_bool_output(b_idx, bit_idx)
            print(f"[DEBUG] Read bool_output[{b_idx}][{bit_idx}] = {val}, Err: {err}")
        else:
            val, err = safe_access.safe_read_int_output(b_idx)
            print(f"[DEBUG] Read int_output[{b_idx}] = {val}, Err: {err}")
        return val, err
    ```

## Performance Considerations

1.  **Minimize Mutex Lock Time:**
    *   The `SafeBufferAccess` wrapper is designed for quick operations.
    *   Avoid performing lengthy computations, I/O operations (like network calls or file access), or complex logic while holding the buffer mutex implicitly through `SafeBufferAccess`. Read/write what you need from/to the buffers quickly, then process the data outside the direct buffer access call.

2.  **Plugin Lifecycle Management:**
    *   `init()`: Perform one-time setup (load config, initialize data structures).
    *   `start_loop()`: Start long-running tasks (servers, periodic threads).
    *   `run_cycle()`: Keep this function lightweight if called frequently by the main OpenPLC loop.
    *   `cleanup()`: Reliably release all resources (stop threads, close connections, free memory).

3.  **Memory Management (Python):**
    *   Python's garbage collector handles memory. However, explicitly close files, sockets, or release other external resources in `cleanup()`.

## Dependencies

### Required for Python Plugins
*   OpenPLC Runtime core (compiled with Python support).
*   Python 3.x development headers (for building the Python bridge).
*   Python 3.x interpreter at runtime.
*   `pthread` library (for mutex operations).

### Optional for Python Plugins
*   `pymodbus`: Required for the `simple_modbus.py` plugin.
*   Other Python packages: As needed by specific custom plugins.

### For Future Native C Plugins
*   C Compiler (e.g., GCC).
*   Standard C library.
*   Potentially other system-level libraries depending on the plugin's purpose.

## License

This plugin system is part of the OpenPLC Runtime project and follows the same licensing terms (typically GPLv3 or later).

## Contributing

When contributing new plugins:

1.  **Python Plugins:**
    *   Follow the established interface (`init`, `start_loop`, etc.).
    *   Use `python_plugin_types.py` for type safety and buffer access.
    *   Include comprehensive error handling and logging.
    *   Document configuration options (e.g., via example `.ini` or `.json` files).
    *   Provide clear usage examples in comments or a separate `README`.
    *   Test your plugin thoroughly with various OpenPLC programs and I/O configurations.
    *   Ensure thread safety for all OpenPLC buffer interactions.
2.  **Future Native C Plugins:**
    *   Adhere to the C API once it's fully implemented and documented.
    *   Pay close attention to memory management and thread safety.

## See Also

*   `plugins/python/examples/example_python_plugin.py` - Basic Python plugin template.
*   `plugins/python/modbus_slave/simple_modbus.py` - Advanced Modbus TCP slave implementation.
*   `plugins/python/shared/python_plugin_types.py` - Core type definitions and safety utilities.
*   `plugins.conf` - Example active plugin configuration file.
*   `core/src/drivers/plugin_driver.h` - C API for the plugin system (internal/future facing).
*   `core/src/drivers/python_plugin_bridge.h` - C interface for Python integration.
*   `docs/PLUGIN_VENV_GUIDE.md` - Guide on managing Python virtual environments for plugins.
*   OpenPLC Runtime main documentation.
