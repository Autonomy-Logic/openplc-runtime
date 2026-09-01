# OpenPLC Runtime Plugin System

This directory contains the OpenPLC Runtime plugin system, which allows extending the runtime with custom drivers and communication protocols. The system supports both Python plugins and native C/C++ plugins (compiled shared libraries).

## Overview

The plugin system provides a flexible architecture for integrating external hardware drivers, communication protocols, and custom logic into the OpenPLC Runtime. It offers thread-safe access to OpenPLC I/O buffers.

**Current Status:**
*   **Supported:** Python plugins (`.py` files) are fully supported and operational.
*   **Supported:** Native C/C++ plugins (`.so` files) are fully supported and operational.

## Architecture

### Core Components

```
core/src/drivers/
├── plugin_driver.c/h          # Main plugin driver system
├── plugin_config.c/h          # Configuration file parsing
├── python_plugin_bridge.h     # Python plugin integration
├── CMakeLists.txt             # Build configuration
├── plugins/
│   ├── python/                # Python plugin implementations
│   │   ├── examples/          # Python plugin examples
│   │   ├── modbus_master/     # Modbus TCP master Python plugin
│   │   ├── modbus_slave/      # Modbus TCP slave Python plugin
│   │   └── shared/            # Shared Python modules (e.g., type definitions)
│   └── native/                # Native C/C++ plugin implementations
│       └── examples/          # Native plugin examples and templates
└── README.md                  # This documentation
```

### Plugin Types

1.  **Python Plugins** (`PLUGIN_TYPE_PYTHON = 0`) - **Supported**
    *   Python scripts (`.py` files).
    *   Embedded Python interpreter.
    *   Easier development and debugging.
    *   Enhanced type safety and buffer access with the `shared` module.
    *   Support for isolated virtual environments per plugin.

2.  **Native C/C++ Plugins** (`PLUGIN_TYPE_NATIVE = 1`) - **Supported**
    *   Compiled shared libraries (`.so` files).
    *   Direct C function calls via `dlopen`/`dlsym`.
    *   Maximum performance for time-critical operations.
    *   No Python interpreter overhead.

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

#### Native C/C++ Plugins (Supported)
```c
// Mandatory initialization function
// args: pointer to plugin_runtime_args_t structure
// IMPORTANT: The args pointer is freed after init() returns.
// You must copy any data you need to retain during init().
int init(void *args);

// Optional lifecycle functions
void start_loop(void);  // Called when plugin should start operations
void stop_loop(void);   // Called when plugin should stop operations
void cleanup(void);     // Called when plugin is being unloaded

// Per-cycle hooks (called during each PLC scan cycle, synchronized with PLC execution)
void cycle_start(void); // Called at start of each scan cycle, before PLC logic
void cycle_end(void);   // Called at end of each scan cycle, after PLC logic

// Optional: retain-variable storage. A plugin exporting both retain_save and
// retain_load becomes the device's retain store (first one found wins),
// displacing the runtime's built-in file store. retain_flush is optional on top
// of that; a plugin without it is assumed to commit inside retain_save.
int retain_save(const uint8_t *blob, uint16_t len);                 // Persist the blob. Must not block.
int retain_load(const char *program_md5, uint16_t md5_len,
                uint8_t *out, uint16_t cap, uint16_t *out_len);     // Restore the blob for THIS program.
int retain_flush(void);                                             // Commit anything still held.
```

**Important: Native Plugin Args Lifetime**

The `plugin_runtime_args_t*` pointer passed to `init()` is freed immediately after the function returns. Native plugins must copy the structure contents (or the specific fields they need) into plugin-owned storage during `init()`. Do not store the pointer itself for later use, as this will result in a use-after-free bug.

```c
// Example: Properly storing runtime args in a native plugin
static plugin_runtime_args_t g_args;  // Plugin-owned copy

int init(void *args) {
    if (!args) return -1;
    // Copy the entire structure
    memcpy(&g_args, args, sizeof(plugin_runtime_args_t));
    // Now g_args can be safely used in start_loop, stop_loop, etc.
    return 0;
}
```

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

The `plugins/python/shared/` module provides a `SafeBufferAccess` wrapper class for robust and safe buffer operations. This is the recommended way to interact with OpenPLC buffers.

```python
from shared import SafeBufferAccess, safe_extract_runtime_args_from_capsule

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
# Format: name,path,enabled,type,plugin_related_config_path,venv_path
# Example for a Python Modbus Slave plugin:
modbus_slave,./core/src/drivers/plugins/python/modbus_slave/simple_modbus.py,1,0,./core/src/drivers/plugins/python/modbus_slave/modbus_slave_config.json,./venvs/modbus_slave
# Example for a custom Python plugin:
my_custom_plugin,./core/src/drivers/plugins/python/examples/my_custom_plugin.py,1,0,./my_custom_plugin_config.ini,./venvs/my_custom_plugin
# Example for a Native C/C++ plugin:
my_native_plugin,./core/src/drivers/plugins/native/my_plugin.so,1,1,./config/my_native_plugin.conf
```

**Fields:**
*   `name`: A unique identifier for the plugin.
*   `path`: Path to the plugin file (`.py` for Python, `.so` for native C/C++).
*   `enabled`: `1` for enabled, `0` for disabled.
*   `type`: `0` for Python (`PLUGIN_TYPE_PYTHON`), `1` for Native C/C++ (`PLUGIN_TYPE_NATIVE`).
*   `plugin_related_config_path`: (Optional) Path to a plugin-specific configuration file (e.g., `.ini`, `.json`, `.conf`).
*   `venv_path`: (Optional, Python only) Path to a Python virtual environment for the plugin.

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

### Enhanced Type Safety with the `shared` Module

The `plugins/python/shared/` package is crucial for developing robust Python plugins. It provides:

#### Key Components

1.  **`PluginRuntimeArgs` (ctypes Structure)**
    *   An exact `ctypes` mapping of the C `plugin_runtime_args_t` structure.
    *   Includes a `safe_access_buffer_size()` method for validated buffer size retrieval.

2.  **`SafeBufferAccess` Wrapper**
    *   The primary interface for thread-safe I/O buffer operations.
    *   Handles `mutex_take`/`mutex_give` automatically.
    *   Provides clear error messages for invalid access attempts (e.g., out of bounds, null pointers).

3.  **`SafeLoggingAccess` Wrapper**
    *   Provides safe access to the runtime logging functions.
    *   Supports `log_info`, `log_debug`, `log_warn`, and `log_error` methods.

4.  **`PluginStructureValidator`**
    *   Utilities for debugging, such as `print_structure_info()` to verify `ctypes` structure alignment and sizes against the C definitions.

5.  **`safe_extract_runtime_args_from_capsule(runtime_args_capsule)`**
    *   The **recommended** function to extract the `plugin_runtime_args_t` pointer from the `PyCapsule` passed to the `init` function.
    *   Performs comprehensive error checking (capsule validity, name, null pointer) and returns a tuple `(runtime_args_ptr, error_message)`.

#### Usage Example (Reiterated from SafeBufferAccess)
```python
from shared import (
    PluginRuntimeArgs,  # For type hinting or direct use if extraction is manual
    safe_extract_runtime_args_from_capsule,
    SafeBufferAccess,
    SafeLoggingAccess,
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
    from shared import (
        safe_extract_runtime_args_from_capsule,
        SafeBufferAccess,
        SafeLoggingAccess
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

### Creating a Native C/C++ Plugin

1.  **Implement required functions** in a C file (e.g., `my_native_plugin.c`):
    ```c
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>
    #include <pthread.h>

    // Include IEC types from the OpenPLC runtime
    #include "iec_types.h"

    // Define plugin_runtime_args_t structure locally to avoid Python dependencies
    typedef struct {
        IEC_BOOL *(*bool_input)[8];
        IEC_BOOL *(*bool_output)[8];
        IEC_BYTE **byte_input;
        IEC_BYTE **byte_output;
        IEC_UINT **int_input;
        IEC_UINT **int_output;
        IEC_UDINT **dint_input;
        IEC_UDINT **dint_output;
        IEC_ULINT **lint_input;
        IEC_ULINT **lint_output;
        IEC_UINT **int_memory;
        IEC_UDINT **dint_memory;
        IEC_ULINT **lint_memory;
        int (*mutex_take)(pthread_mutex_t *mutex);
        int (*mutex_give)(pthread_mutex_t *mutex);
        pthread_mutex_t *buffer_mutex;
        char plugin_specific_config_file_path[256];
        int buffer_size;
        int bits_per_buffer;
    } plugin_runtime_args_t;

    // IMPORTANT: Copy args during init - the pointer is freed after init returns
    static plugin_runtime_args_t g_args;
    static int plugin_initialized = 0;

    int init(void *args) {
        if (!args) return -1;
        // Copy the entire structure - do NOT store the pointer
        memcpy(&g_args, args, sizeof(plugin_runtime_args_t));
        plugin_initialized = 1;
        printf("Native Plugin: Initialized (buffer_size=%d)\n", g_args.buffer_size);
        return 0;
    }

    void start_loop(void) {
        if (!plugin_initialized) return;
        printf("Native Plugin: Starting operations\n");
        // Start threads, timers, etc.
    }

    void stop_loop(void) {
        printf("Native Plugin: Stopping operations\n");
        // Signal threads to stop, etc.
    }

    void cleanup(void) {
        printf("Native Plugin: Cleaning up\n");
        plugin_initialized = 0;
    }
    ```

2.  **Compile as a shared library:**
    ```bash
    gcc -shared -fPIC -I/path/to/openplc-runtime/core/lib -o my_native_plugin.so my_native_plugin.c -lpthread
    ```
    See `plugins/native/examples/Makefile` for a complete build example.

3.  **Add to `plugins.conf`:**
    ```
    my_native_plugin,./path/to/my_native_plugin.so,1,1,./config/my_native_plugin.conf
    ```

4.  **Test your plugin:**
    *   Place the `.so` file at the specified path.
    *   Run OpenPLC. Check logs for your plugin's initialization messages.
    *   See `plugins/native/examples/test_plugin_loader.c` for a standalone test harness.

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

// Call cycle_start for all active native plugins (called at start of each PLC scan cycle)
// Plugins opt-in by implementing cycle_start(); opt-out by not implementing it
void plugin_driver_cycle_start(plugin_driver_t *driver);

// Call cycle_end for all active native plugins (called at end of each PLC scan cycle)
// Plugins opt-in by implementing cycle_end(); opt-out by not implementing it
void plugin_driver_cycle_end(plugin_driver_t *driver);

// Find the plugin acting as this device's retain store (first plugin that
// exports both retain_save and retain_load), or NULL if none does
plugin_instance_t *plugin_driver_find_retain_store(plugin_driver_t *driver);

// Save/load/flush the retain blob through the chosen store plugin
// Return 0 on success; retain_save/retain_load return -1 if `store` isn't a
// valid store, retain_flush is a no-op (returns 0) if the plugin has no
// retain_flush export
int plugin_driver_retain_save(plugin_instance_t *store, const uint8_t *blob, uint16_t len);
int plugin_driver_retain_load(plugin_instance_t *store, const char *program_md5, uint16_t md5_len,
                              uint8_t *out, uint16_t cap, uint16_t *out_len);
int plugin_driver_retain_flush(plugin_instance_t *store);

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
    *   `cleanup()`: Reliably release all resources (stop threads, close connections, free memory).

3.  **Native Plugin Cycle Hooks (Real-Time Synchronization):**

    Native plugins can optionally implement `cycle_start()` and `cycle_end()` functions to synchronize with the PLC scan cycle. These hooks are called during each scan cycle while the buffer mutex is held, allowing direct access to I/O buffers without additional locking.

    *   `cycle_start()`: Called at the beginning of each scan cycle, before PLC logic execution. Use this to read inputs or prepare data for the PLC program.
    *   `cycle_end()`: Called at the end of each scan cycle, after PLC logic execution. Use this to process outputs or perform post-cycle operations.

    **Opt-in/Opt-out:** Plugins opt-in by implementing these functions. If a plugin does not implement them (NULL pointer), the runtime simply skips calling them for that plugin. This allows plugins to choose between:
    *   **Synchronous operation**: Implement cycle hooks to run in lockstep with the PLC scan cycle (real-time).
    *   **Asynchronous operation**: Use `start_loop()` to create separate threads that run independently.

    **Important:** Keep cycle hook implementations as fast as possible since they run in the critical path of the PLC scan cycle. Long-running operations will increase scan cycle time and may cause timing issues.

    ```c
    // Example: Native plugin with cycle hooks
    static plugin_runtime_args_t g_args;

    int init(void *args) {
        memcpy(&g_args, args, sizeof(plugin_runtime_args_t));
        return 0;
    }

    void cycle_start(void) {
        // Read inputs before PLC logic runs
        // Buffer mutex is already held - safe to access buffers directly
    }

    void cycle_end(void) {
        // Process outputs after PLC logic runs
        // Buffer mutex is still held - safe to access buffers directly
    }
    ```

4.  **Native Plugin Retain Store:**

    A native plugin that owns retention hardware (FRAM, battery-backed SRAM, a data partition, etc.) can become the device's retain store by exporting `retain_save()` and `retain_load()`, displacing the runtime's built-in file store. The runtime marshals retained variables into an opaque blob and hands it to the store; the plugin only needs to persist and return bytes, with no knowledge of what they mean.

    The three calls map onto the PLC lifecycle, and the same three names and the same contract describe baremetal's `openplc_retain.h` — one page, written twice:

    | Flow | Call |
    |---|---|
    | start | `retain_load()`, once, before the first scan |
    | scan | `retain_save()`, every cycle, **while running only** |
    | stop | `retain_flush()`, once, as the program is unloaded |

    *   `retain_save(blob, len)`: Called once per scan cycle, unconditionally, from the scan's quiescent window, for as long as the PLC is running. Must return promptly and must not block — this runs inside the scan cycle. **This is the durability path.** The runtime does not diff and does not rate-limit: holding the bytes, skipping an unchanged commit, and committing on a schedule the medium can sustain are all the plugin's job, because only the plugin knows what a write costs on this board.
    *   `retain_load(program_md5, md5_len, out, cap, out_len)`: Called at program start to restore the blob **for this program**. `program_md5` is `md5_len` characters of lower-case hex and is **not guaranteed NUL-terminated** — compare it with `memcmp`, never `strcmp`. The plugin decides whether the bytes it holds still belong to the running program: if the identity differs, discard them, log one line saying storage was cleared, and report empty (`*out_len = 0`) so every retained variable starts at its declared initial value. **Do not persist the new identity here** — hold it and commit it alongside the blob on the next `retain_save`, so a load never mutates storage and identity and bytes are always written as one unit.
    *   `retain_flush(void)`: Optional. Commit anything still held, now. Called once as the program is unloaded. **A hint, not the durability mechanism** — retention exists for the power cut nobody schedules, and a power cut does not call `retain_flush`. A plugin that commits solely here loses everything in exactly the case it was written for. A plugin that already commits inside `retain_save` has nothing to do and should return 0.

    **Both `retain_save` and `retain_load` are required** to be selected as the store; a plugin exporting only one is ignored. If more than one plugin exports both, the first one found wins and the rest are logged and ignored.

5.  **Memory Management (Python):**
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

### For Native C/C++ Plugins
*   C Compiler (e.g., GCC) with `-fPIC` and `-shared` support.
*   Standard C library.
*   `pthread` library for mutex operations.
*   `dl` library for dynamic loading (usually included by default).

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
2.  **Native C/C++ Plugins:**
    *   Adhere to the C API defined in this document.
    *   Pay close attention to memory management and thread safety.
    *   Remember to copy `plugin_runtime_args_t` contents during `init()` - do not store the pointer.
    *   Test with the standalone loader before integrating with the full runtime.

## See Also

*   `plugins/python/examples/example_python_plugin.py` - Basic Python plugin template.
*   `plugins/python/modbus_slave/simple_modbus.py` - Advanced Modbus TCP slave implementation.
*   `plugins/python/shared/` - Core type definitions and safety utilities for Python plugins.
*   `plugins/native/examples/test_plugin.c` - Example native C plugin implementation.
*   `plugins/native/examples/Makefile` - Build configuration for native plugins.
*   `plugins/native/examples/test_plugin_loader.c` - Standalone test harness for native plugins.
*   `plugins.conf` - Example active plugin configuration file.
*   `core/src/drivers/plugin_driver.h` - C API for the plugin system.
*   `core/src/drivers/python_plugin_bridge.h` - C interface for Python integration.
*   `docs/PLUGIN_VENV_GUIDE.md` - Guide on managing Python virtual environments for plugins.
*   OpenPLC Runtime main documentation.
