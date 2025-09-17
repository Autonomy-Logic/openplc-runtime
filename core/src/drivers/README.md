# OpenPLC Runtime Plugin System

This directory contains the OpenPLC Runtime plugin system, which allows extending the runtime with custom drivers and communication protocols through both native C and Python plugins.

## Overview

The plugin system provides a flexible architecture for integrating external hardware drivers, communication protocols, and custom logic into the OpenPLC Runtime. It offers thread-safe access to OpenPLC I/O buffers and supports both native C plugins and Python plugins.

## Architecture

### Core Components

```
core/src/drivers/
├── plugin_driver.c/h          # Main plugin driver system
├── plugin_config.c/h          # Configuration file parsing
├── python_plugin_bridge.c/h   # Python plugin integration
├── CMakeLists.txt             # Build configuration
├── examples/                  # Plugin examples
└── *.py                      # Python plugin implementations
```

### Plugin Types

1. **Python Plugins** (`PLUGIN_TYPE_PYTHON = 0`)
   - Python scripts (.py files)
   - Embedded Python interpreter
   - Easier development and debugging
   - Enhanced type safety with `python_plugin_types.py`

2. **Native C Plugins** (`PLUGIN_TYPE_NATIVE = 1`)
   - Compiled shared libraries (.so files)
   - Direct C function calls
   - Maximum performance

## Plugin Interface

### Required Functions

All plugins must implement these core functions:

#### Python Plugins
```python
def init(runtime_args_capsule):
    """
    Initialize plugin with runtime arguments
    Args:
        runtime_args_capsule: PyCapsule containing plugin_runtime_args_t
    Returns:
        bool: True if initialization successful
    """
    pass

# Optional functions
def start_loop():
    """Called when plugin should start operations"""
    pass

def stop_loop():
    """Called when plugin should stop operations"""
    pass

def run_cycle():
    """Called periodically during runtime"""
    pass

def cleanup():
    """Called when plugin is being unloaded"""
    pass
```

#### Native C Plugins
```c
int init(plugin_runtime_args_t *args);
void start_loop(void);
void stop_loop(void);
void run_cycle(void);
void cleanup(void);
```

### Runtime Arguments Structure

Plugins receive access to OpenPLC buffers through the `plugin_runtime_args_t` structure:

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
    IEC_UINT **int_memory;          // Internal memory
    IEC_UDINT **dint_memory;        // Internal memory
    IEC_ULINT **lint_memory;        // Internal memory
    
    // Thread synchronization
    int (*mutex_take)(pthread_mutex_t *mutex);
    int (*mutex_give)(pthread_mutex_t *mutex);
    pthread_mutex_t *buffer_mutex;
    
    // Buffer metadata
    int buffer_size;                // Number of buffers
    int bits_per_buffer;           // Bits per boolean buffer
} plugin_runtime_args_t;
```

## Thread-Safe Buffer Access

### Enhanced Python SafeBufferAccess

The `python_plugin_types.py` module provides a `SafeBufferAccess` wrapper class for safe buffer operations:

```python
from python_plugin_types import SafeBufferAccess

# Create safe buffer access wrapper
safe_buffer_access = SafeBufferAccess(runtime_args)

# Safe read operation
value, error_msg = safe_buffer_access.safe_read_bool_output(buffer_idx, bit_idx)
if error_msg == "Success":
    print(f"Read value: {value}")
else:
    print(f"Read error: {error_msg}")

# Safe write operation
success, error_msg = safe_buffer_access.safe_write_bool_output(buffer_idx, bit_idx, True)
if error_msg == "Success":
    print("Write successful")
else:
    print(f"Write error: {error_msg}")
```

### Manual Python Example (Legacy)
```python
def safe_read_output(runtime_args, buffer_idx, bit_pos):
    """Safely read a boolean output"""
    try:
        if runtime_args.mutex_take(runtime_args.buffer_mutex) == 0:
            value = runtime_args.bool_output[buffer_idx][bit_pos].contents.value
            return bool(value)
    finally:
        runtime_args.mutex_give(runtime_args.buffer_mutex)
    return False

def safe_write_output(runtime_args, buffer_idx, bit_pos, value):
    """Safely write a boolean output"""
    try:
        if runtime_args.mutex_take(runtime_args.buffer_mutex) == 0:
            runtime_args.bool_output[buffer_idx][bit_pos].contents.value = 1 if value else 0
            return True
    finally:
        runtime_args.mutex_give(runtime_args.buffer_mutex)
    return False
```

## Configuration

### Plugin Configuration File Format

```
# Format: name,path,enabled,type,plugin_related_config_path
python_plugin,../core/src/drivers/example_python_plugin.py,1,0,./modbus_slave_config.ini
native_plugin,./plugins/example1.so,1,1,./config/example1.conf
```

**Fields:**
- `name`: Plugin identifier
- `path`: Path to plugin file (.py for Python, .so for native)
- `enabled`: 1 = enabled, 0 = disabled
- `type`: 0 = Python (`PLUGIN_TYPE_PYTHON`), 1 = Native C (`PLUGIN_TYPE_NATIVE`)
- `plugin_related_config_path`: Path to plugin-specific configuration

**Note:** The type values are: 0 for Python plugins, 1 for Native C plugins (opposite of what might be expected).

### Loading Configuration
```c
plugin_driver_t *driver = plugin_driver_create();
plugin_driver_load_config(driver, "plugins.conf");
plugin_driver_init(driver);
plugin_driver_start(driver);
```

## Examples

### 1. Simple Python Plugin

See `example_python_plugin.py` for a basic template that demonstrates:
- Plugin initialization
- Runtime arguments extraction
- Buffer access patterns
- Lifecycle management

### 2. Modbus TCP Slave

The `modbus_slave.py` provides a complete implementation of a Modbus TCP slave server:

**Features:**
- Maps OpenPLC bool_input/bool_output to Modbus coils and discrete inputs
- Maps OpenPLC int_input/int_output to Modbus registers
- Supports standard Modbus function codes (01, 02, 03, 04, 05, 06, 0F, 10)
- Thread-safe buffer access
- Configurable host/port
- Enhanced functionality with pymodbus (optional)

**Configuration:**
```ini
[plugin_modbus_slave]
type = PLUGIN_TYPE_PYTHON
path = /path/to/modbus_slave.py
enabled = true
host = 172.29.65.104
port = 5020
max_coils = 8000
max_discrete_inputs = 8000
```

**Usage:**
```python
# Initialize and start Modbus slave
if init(runtime_args_capsule):
    start_loop()  # Starts server on configured port
```

### 3. Advanced Modbus Implementation

The `simple_modbus.py` provides an advanced asynchronous Modbus TCP server implementation:

**Features:**
- Uses pymodbus for full Modbus protocol support
- Asynchronous operation with asyncio
- Custom OpenPLCModbusDataBlock that directly mirrors OpenPLC buffers
- Enhanced error handling and debugging
- Configurable host/port settings
- Thread-safe buffer access using SafeBufferAccess

**Usage:**
```python
# Can be run standalone for testing
python3 simple_modbus.py

# Or loaded as a plugin via configuration
```

## Python Plugin Type System

### Enhanced Type Safety with `python_plugin_types.py`

The `python_plugin_types.py` module provides comprehensive type definitions and safety utilities for Python plugins:

#### Key Components

1. **PluginRuntimeArgs Structure**
   - Exact ctypes mapping of the C `plugin_runtime_args_t` structure
   - Built-in validation methods
   - Safe buffer size access

2. **SafeBufferAccess Wrapper**
   - Thread-safe buffer operations
   - Automatic mutex handling
   - Error reporting and validation

3. **PluginStructureValidator**
   - Structure alignment validation
   - Debugging information
   - Platform compatibility checks

4. **Safe Capsule Extraction**
   - Robust PyCapsule handling
   - Comprehensive error checking
   - Memory safety validation

#### Usage Example
```python
from python_plugin_types import (
    PluginRuntimeArgs, 
    safe_extract_runtime_args_from_capsule,
    SafeBufferAccess,
    PluginStructureValidator
)

def init(runtime_args_capsule):
    # Validate structure for debugging
    PluginStructureValidator.print_structure_info()
    
    # Safe extraction with error handling
    runtime_args, error_msg = safe_extract_runtime_args_from_capsule(runtime_args_capsule)
    if runtime_args is None:
        print(f"Extraction failed: {error_msg}")
        return False
    
    # Create safe buffer access
    safe_access = SafeBufferAccess(runtime_args)
    if not safe_access.is_valid:
        print(f"Buffer access failed: {safe_access.error_msg}")
        return False
    
    return True
```

## Development Guide

### Creating a Python Plugin

1. **Create plugin file:**
```python
#!/usr/bin/env python3
import ctypes
from ctypes import *

# Import the enhanced type definitions
from python_plugin_types import (
    PluginRuntimeArgs, 
    safe_extract_runtime_args_from_capsule,
    SafeBufferAccess,
    PluginStructureValidator
)

def init(runtime_args_capsule):
    """Initialize your plugin"""
    global _runtime_args, _safe_buffer_access
    
    print("Plugin initializing...")
    
    try:
        # Validate structure alignment for debugging
        PluginStructureValidator.print_structure_info()
        
        # Extract runtime args from capsule using safe method
        runtime_args, error_msg = safe_extract_runtime_args_from_capsule(runtime_args_capsule)
        if runtime_args is None:
            print(f"Failed to extract runtime args: {error_msg}")
            return False
        
        # Validate buffer size
        buffer_size, size_error = runtime_args.safe_access_buffer_size()
        if buffer_size == -1:
            print(f"Failed to access buffer size: {size_error}")
            return False
        
        # Create safe buffer access wrapper
        _safe_buffer_access = SafeBufferAccess(runtime_args)
        if not _safe_buffer_access.is_valid:
            print(f"Failed to create safe buffer access: {_safe_buffer_access.error_msg}")
            return False
        
        _runtime_args = runtime_args
        print("Plugin initialized successfully")
        return True
        
    except Exception as e:
        print(f"Plugin initialization failed: {e}")
        return False

def start_loop():
    """Start plugin operations"""
    pass

def cleanup():
    """Cleanup resources"""
    pass
```

2. **Add to configuration:**
```
my_plugin,/path/to/my_plugin.py,1,0,/path/to/config.ini
```

3. **Test plugin:**
```bash
# Load and test plugin
python3 my_plugin.py
```

### Creating a Native C Plugin

1. **Implement required functions:**
```c
#include "plugin_driver.h"

int init(plugin_runtime_args_t *args) {
    // Initialize plugin
    return 0; // Success
}

void start_loop(void) {
    // Start operations
}

void cleanup(void) {
    // Cleanup resources
}
```

2. **Compile as shared library:**
```bash
gcc -shared -fPIC -o my_plugin.so my_plugin.c
```

3. **Add to configuration:**
```
my_plugin,./plugins/my_plugin.so,1,0,./config/my_plugin.conf
```

## Buffer Mapping

### Boolean Buffers
- `bool_input[BUFFER_SIZE][8]` - Digital inputs (read-only for plugins)
- `bool_output[BUFFER_SIZE][8]` - Digital outputs (read/write)
- Each buffer contains 8 boolean values
- Total capacity: BUFFER_SIZE × 8 boolean I/O points

### Integer Buffers
- `int_input/int_output` - 16-bit integers
- `dint_input/dint_output` - 32-bit integers  
- `lint_input/lint_output` - 64-bit integers
- `*_memory` - Internal memory buffers

### Modbus Mapping Example
```
Modbus Coils (0x01)           -> bool_output[0-999][0-7]
Modbus Discrete Inputs (0x02) -> bool_input[0-999][0-7]
Modbus Holding Registers (0x03) -> int_output[0-999]
Modbus Input Registers (0x04)   -> int_input[0-999]
```

## API Reference

### Plugin Driver Functions

```c
// Driver lifecycle
plugin_driver_t *plugin_driver_create(void);
int plugin_driver_load_config(plugin_driver_t *driver, const char *config_file);
int plugin_driver_init(plugin_driver_t *driver);
int plugin_driver_start(plugin_driver_t *driver);
int plugin_driver_stop(plugin_driver_t *driver);
void plugin_driver_destroy(plugin_driver_t *driver);

// Runtime arguments
void *generate_structured_args_with_driver(plugin_type_t type, plugin_driver_t *driver);
void free_structured_args(plugin_runtime_args_t *args);

// Python plugin support
int python_plugin_get_symbols(plugin_instance_t *plugin);
```

### Configuration Functions

```c
// Parse plugin configuration file
int parse_plugin_config(const char *config_file, plugin_config_t *configs, int max_configs);
```

## Error Handling

### Common Issues

1. **Plugin initialization fails:**
   - Check plugin file path and permissions
   - Verify Python syntax for Python plugins
   - Check runtime arguments extraction

2. **Buffer access errors:**
   - Always use mutex protection
   - Check buffer bounds before access
   - Handle null pointer cases

3. **Python import errors:**
   - Ensure Python path includes plugin directory
   - Check for missing dependencies
   - Verify Python interpreter initialization

### Debugging

1. **Enable debug output:**
```python
import sys
print(f"Plugin loaded from: {__file__}", file=sys.stderr)
```

2. **Check plugin symbols:**
```bash
# For native plugins
nm -D plugin.so | grep init

# For Python plugins
python3 -c "import plugin; print(dir(plugin))"
```

3. **Monitor buffer access:**
```python
def debug_buffer_access(runtime_args, operation, buffer_name, index):
    print(f"[DEBUG] {operation} {buffer_name}[{index}]")
```

## Performance Considerations

1. **Minimize mutex lock time:**
   - Read/write buffers quickly
   - Avoid complex operations while holding mutex
   - Use local variables for processing

2. **Plugin lifecycle:**
   - Initialize resources in `init()`
   - Start threads/servers in `start_loop()`
   - Clean up in `cleanup()`

3. **Memory management:**
   - Python plugins: Let Python GC handle memory
   - Native plugins: Free allocated memory in `cleanup()`

## Dependencies

### Required
- OpenPLC Runtime core
- pthread library
- Python 3.x (for Python plugins)

### Optional
- pymodbus (for enhanced Modbus functionality)
- Additional Python packages as needed by specific plugins

## License

This plugin system is part of the OpenPLC Runtime project and follows the same licensing terms.

## Contributing

When contributing new plugins:

1. Follow the established plugin interface
2. Include comprehensive error handling
3. Document configuration options
4. Provide usage examples
5. Test with multiple buffer configurations
6. Ensure thread safety for buffer access

## See Also

- `example_python_plugin.py` - Basic plugin template with enhanced type safety
- `simple_modbus.py` - Advanced asynchronous Modbus TCP slave implementation
- `python_plugin_types.py` - Enhanced type definitions and safety utilities
- `plugin_config_example.txt` - Configuration file format examples
- `plugins.conf` - Active plugin configuration file
- `modbus_slave_config.ini` - Modbus plugin configuration example
- OpenPLC Runtime documentation
