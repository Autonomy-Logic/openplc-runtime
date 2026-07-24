# Proposal: Robust C-Python Runtime Data Sharing

## Executive Summary

This document analyzes the current approach for sharing runtime functions and buffers between C and Python plugins in OpenPLC, identifies its weaknesses, and proposes a more robust and simplified architecture.

---

## 1. Current Implementation Analysis

### 1.1 How It Works Today

The current system uses a monolithic C struct (`plugin_runtime_args_t`) that is:
1. Allocated and populated in C (`plugin_driver.c`)
2. Wrapped in a PyCapsule
3. Passed to Python plugins
4. Extracted using ctypes with a manually-maintained mirror struct

```
┌─────────────┐    PyCapsule    ┌─────────────┐    ctypes    ┌─────────────┐
│   C Struct  │ ──────────────> │   Capsule   │ ──────────> │ Python Struct│
│ (456 bytes) │                 │  (pointer)  │              │  (mirror)   │
└─────────────┘                 └─────────────┘              └─────────────┘
```

### 1.2 Current Problems

| Problem | Impact | Severity |
|---------|--------|----------|
| **Manual struct synchronization** | Any field order change in C requires manual Python update | Critical |
| **No version checking** | Incompatible changes cause silent memory corruption | Critical |
| **Complex nested pointers** | `IEC_BOOL *(*bool_input)[8]` is error-prone in ctypes | High |
| **Monolithic struct** | Adding one field requires updating entire struct on both sides | High |
| **No compile-time validation** | Mismatches only discovered at runtime (crashes) | High |
| **Tight coupling** | Python code depends on exact C memory layout | Medium |

### 1.3 Root Cause of Recent Bug

The crash was caused by field order mismatch:

```c
// C struct order (plugin_types.h):
mutex_take
mutex_give
buffer_mutex        // <-- Position 3
get_var_list        // <-- Position 4
get_var_size
get_var_count
```

```python
# Python struct order (plugin_runtime_args.py) - WRONG:
mutex_take
mutex_give
get_var_list        # <-- Position 3 (WRONG!)
get_var_size
get_var_count
buffer_mutex        # <-- Position 6 (WRONG!)
```

This caused Python to read garbage values, leading to segfaults.

---

## 2. Proposed Solution: Layered API Architecture

### 2.1 Design Principles

1. **Separation of Concerns**: Split the monolithic struct into logical groups
2. **Explicit Versioning**: Include version info for compatibility checking
3. **Simplified Interface**: Hide pointer complexity behind C helper functions
4. **Validation First**: Validate compatibility before any data access
5. **Single Source of Truth**: Generate Python bindings from C definitions

### 2.2 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           LAYER 3: Plugin API                           │
│   High-level Python interface (SafeBufferAccess, OpcuaServer, etc.)    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        LAYER 2: C Bridge Functions                      │
│   Simple C functions called via ctypes (no complex pointer passing)    │
│   - plc_read_variable(index) -> value                                  │
│   - plc_write_variable(index, value) -> success                        │
│   - plc_get_var_info(index) -> {size, type, name}                      │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      LAYER 1: Minimal Bootstrap Struct                  │
│   Only contains: version, function pointers to bridge, config path     │
│   Small, stable, rarely changes                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Detailed Implementation

### 3.1 Layer 1: Minimal Bootstrap Struct

Replace the large monolithic struct with a minimal bootstrap struct:

```c
// plugin_api.h - NEW FILE

#define PLUGIN_API_VERSION_MAJOR 2
#define PLUGIN_API_VERSION_MINOR 0

// Simple struct with only essentials - STABLE, rarely changes
typedef struct {
    // Version info for compatibility checking
    uint32_t api_version_major;
    uint32_t api_version_minor;
    uint32_t struct_size;  // For validation

    // Function pointers to bridge layer (Layer 2)
    void* bridge_handle;  // Opaque handle to bridge context

    // Essential function pointers
    int (*read_variable)(void* handle, uint16_t index, void* value, size_t* size);
    int (*write_variable)(void* handle, uint16_t index, const void* value, size_t size);
    int (*get_variable_count)(void* handle, uint16_t* count);
    int (*get_variable_info)(void* handle, uint16_t index, VariableInfo* info);

    // Logging (simple interface)
    void (*log_message)(void* handle, int level, const char* message);

    // Config
    char config_path[256];

} PluginBootstrap;

typedef struct {
    uint16_t index;
    uint8_t  type;        // IEC type enum
    uint8_t  direction;   // INPUT, OUTPUT, MEMORY
    size_t   size;        // Size in bytes
    char     name[64];    // Variable name (optional)
} VariableInfo;
```

**Benefits:**
- Only 11 fields vs 25+ in current struct
- No complex nested pointers
- Version checking built-in
- `struct_size` allows runtime validation

### 3.2 Layer 2: C Bridge Functions

Implement simple C functions that hide the complexity:

```c
// plugin_bridge.c - NEW FILE

typedef struct {
    plugin_driver_t* driver;
    pthread_mutex_t* mutex;
    // Internal state
} BridgeContext;

// Read any variable by index - handles all types internally
int bridge_read_variable(void* handle, uint16_t index, void* value, size_t* size) {
    BridgeContext* ctx = (BridgeContext*)handle;

    // Lock mutex
    pthread_mutex_lock(ctx->mutex);

    // Get variable info
    size_t var_size = ext_get_var_size(index);
    void* var_addr = ext_get_var_addr(index);

    if (!var_addr || var_size == 0) {
        pthread_mutex_unlock(ctx->mutex);
        return PLUGIN_ERR_INVALID_INDEX;
    }

    // Copy value
    memcpy(value, var_addr, var_size);
    *size = var_size;

    pthread_mutex_unlock(ctx->mutex);
    return PLUGIN_OK;
}

// Write any variable by index
int bridge_write_variable(void* handle, uint16_t index, const void* value, size_t size) {
    BridgeContext* ctx = (BridgeContext*)handle;

    pthread_mutex_lock(ctx->mutex);

    size_t var_size = ext_get_var_size(index);
    void* var_addr = ext_get_var_addr(index);

    if (!var_addr || var_size == 0 || size != var_size) {
        pthread_mutex_unlock(ctx->mutex);
        return PLUGIN_ERR_INVALID_INDEX;
    }

    memcpy(var_addr, value, size);

    pthread_mutex_unlock(ctx->mutex);
    return PLUGIN_OK;
}

// Get variable metadata
int bridge_get_variable_info(void* handle, uint16_t index, VariableInfo* info) {
    info->index = index;
    info->size = ext_get_var_size(index);
    info->type = determine_iec_type(index);  // Internal helper
    info->direction = determine_direction(index);
    // name populated if available
    return PLUGIN_OK;
}
```

**Benefits:**
- Mutex handling is internal - Python doesn't manage locks
- Type handling is internal - Python just passes bytes
- Error codes instead of crashes
- No pointer arithmetic in Python

### 3.3 Layer 3: Python Simple Interface

```python
# plugin_api.py - NEW FILE

import ctypes
from enum import IntEnum

class PluginError(IntEnum):
    OK = 0
    INVALID_INDEX = 1
    INVALID_SIZE = 2
    MUTEX_ERROR = 3
    VERSION_MISMATCH = 4

class PluginBootstrap(ctypes.Structure):
    """Minimal bootstrap struct - matches C exactly"""
    _fields_ = [
        ("api_version_major", ctypes.c_uint32),
        ("api_version_minor", ctypes.c_uint32),
        ("struct_size", ctypes.c_uint32),
        ("bridge_handle", ctypes.c_void_p),
        ("read_variable", ctypes.CFUNCTYPE(
            ctypes.c_int,      # return
            ctypes.c_void_p,   # handle
            ctypes.c_uint16,   # index
            ctypes.c_void_p,   # value (output)
            ctypes.POINTER(ctypes.c_size_t)  # size (output)
        )),
        ("write_variable", ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_void_p,
            ctypes.c_size_t
        )),
        ("get_variable_count", ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16)
        )),
        ("get_variable_info", ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_void_p  # VariableInfo*
        )),
        ("log_message", ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_char_p
        )),
        ("config_path", ctypes.c_char * 256),
    ]


class PLCBridge:
    """High-level Python interface to PLC runtime"""

    EXPECTED_VERSION_MAJOR = 2
    EXPECTED_VERSION_MINOR = 0

    def __init__(self, capsule):
        # Extract bootstrap struct
        self._bootstrap = self._extract_bootstrap(capsule)

        # Validate version FIRST
        self._validate_version()

        # Cache handle for function calls
        self._handle = self._bootstrap.bridge_handle

    def _validate_version(self):
        """Check API compatibility before any operations"""
        major = self._bootstrap.api_version_major
        minor = self._bootstrap.api_version_minor
        size = self._bootstrap.struct_size

        if major != self.EXPECTED_VERSION_MAJOR:
            raise RuntimeError(
                f"API version mismatch: expected {self.EXPECTED_VERSION_MAJOR}.x, "
                f"got {major}.{minor}"
            )

        expected_size = ctypes.sizeof(PluginBootstrap)
        if size != expected_size:
            raise RuntimeError(
                f"Struct size mismatch: expected {expected_size}, got {size}. "
                f"This indicates a build mismatch between C and Python."
            )

    def read_variable(self, index: int) -> tuple[bytes, int]:
        """Read a PLC variable by index. Returns (value_bytes, error_code)"""
        value_buffer = ctypes.create_string_buffer(8)  # Max 64-bit
        size = ctypes.c_size_t(0)

        result = self._bootstrap.read_variable(
            self._handle,
            ctypes.c_uint16(index),
            ctypes.cast(value_buffer, ctypes.c_void_p),
            ctypes.byref(size)
        )

        if result != PluginError.OK:
            return None, result

        return value_buffer.raw[:size.value], PluginError.OK

    def write_variable(self, index: int, value: bytes) -> int:
        """Write a PLC variable by index. Returns error_code"""
        return self._bootstrap.write_variable(
            self._handle,
            ctypes.c_uint16(index),
            value,
            len(value)
        )

    def get_variable_count(self) -> tuple[int, int]:
        """Get total number of variables. Returns (count, error_code)"""
        count = ctypes.c_uint16(0)
        result = self._bootstrap.get_variable_count(
            self._handle,
            ctypes.byref(count)
        )
        return count.value, result

    def log(self, level: int, message: str):
        """Log a message through the C runtime"""
        self._bootstrap.log_message(
            self._handle,
            level,
            message.encode('utf-8')
        )
```

---

## 4. Migration Path

### Phase 1: Add Version Checking (Low Risk)

Add version fields to existing struct without breaking compatibility:

```c
// Add to beginning of existing plugin_runtime_args_t
typedef struct {
    // NEW: Version info (add at START for easy access)
    uint32_t api_version;      // = 0x00010000 for v1.0
    uint32_t struct_size;      // = sizeof(plugin_runtime_args_t)

    // ... existing fields unchanged ...
} plugin_runtime_args_t;
```

```python
# Update Python to check version first
def validate_struct(args):
    if args.api_version != 0x00010000:
        raise RuntimeError(f"Version mismatch: {args.api_version:#x}")
    if args.struct_size != ctypes.sizeof(PluginRuntimeArgs):
        raise RuntimeError(f"Size mismatch: {args.struct_size}")
```

### Phase 2: Add Bridge Functions (Medium Risk)

Add new bridge functions alongside existing implementation:

```c
// New bridge functions coexist with direct buffer access
// Plugins can choose which to use
```

### Phase 3: Deprecate Direct Buffer Access (Breaking Change)

Once all plugins migrate to bridge functions, remove direct buffer pointers from the API.

---

## 5. Alternative: Auto-Generated Bindings

### 5.1 Generate Python from C Header

Use a tool to automatically generate Python ctypes from C header:

```bash
# Using ctypesgen (example)
ctypesgen -o plugin_types_generated.py plugin_types.h
```

### 5.2 Compile-Time Struct Validation

Add a C program that validates struct layout at build time:

```c
// validate_struct_layout.c - Run during build
#include "plugin_types.h"
#include <stdio.h>
#include <stddef.h>

int main() {
    printf("STRUCT_SIZE=%zu\n", sizeof(plugin_runtime_args_t));
    printf("OFFSET_mutex_take=%zu\n", offsetof(plugin_runtime_args_t, mutex_take));
    printf("OFFSET_mutex_give=%zu\n", offsetof(plugin_runtime_args_t, mutex_give));
    printf("OFFSET_buffer_mutex=%zu\n", offsetof(plugin_runtime_args_t, buffer_mutex));
    printf("OFFSET_get_var_list=%zu\n", offsetof(plugin_runtime_args_t, get_var_list));
    // ... etc
    return 0;
}
```

Python can read this at runtime to validate:

```python
def validate_offsets():
    """Compare expected vs actual field offsets"""
    expected = read_offsets_from_build_output()
    for field, offset in PluginRuntimeArgs._fields_:
        actual = getattr(PluginRuntimeArgs, field).offset
        if actual != expected[field]:
            raise RuntimeError(f"Offset mismatch for {field}")
```

---

## 6. Comparison

| Aspect | Current | Proposed (Bridge) | Proposed (Auto-gen) |
|--------|---------|-------------------|---------------------|
| **Complexity** | High | Low | Medium |
| **Maintenance** | Manual sync required | Minimal | Automated |
| **Performance** | Direct memory | Function call overhead | Direct memory |
| **Safety** | Crash on mismatch | Error codes | Validated at build |
| **Breaking Changes** | Silent corruption | Version check fails | Build fails |
| **Implementation Effort** | N/A | Medium | Low |

---

## 7. Recommendation

### Short Term (Immediate)
1. Add `api_version` and `struct_size` fields to existing struct
2. Add validation in Python before accessing any fields
3. Add build-time offset validation script

### Medium Term (Next Release)
1. Implement bridge functions for variable access
2. Migrate plugins to use bridge functions
3. Add comprehensive error handling

### Long Term (Future)
1. Deprecate direct buffer pointer access
2. Simplify bootstrap struct to minimal interface
3. Consider using a proper FFI library (cffi) instead of ctypes

---

## 8. Code Examples

### 8.1 Quick Fix: Add Version Validation

```c
// plugin_types.h - Add at the BEGINNING of struct
#define PLUGIN_API_VERSION 0x00020001  // v2.0.1

typedef struct {
    uint32_t api_version;   // MUST be first field
    uint32_t struct_size;   // MUST be second field

    // ... rest of existing fields ...
} plugin_runtime_args_t;

// plugin_driver.c - Set version when creating
args->api_version = PLUGIN_API_VERSION;
args->struct_size = sizeof(plugin_runtime_args_t);
```

```python
# plugin_runtime_args.py - Add validation
EXPECTED_API_VERSION = 0x00020001

class PluginRuntimeArgs(ctypes.Structure):
    _fields_ = [
        ("api_version", ctypes.c_uint32),    # NEW - must be first
        ("struct_size", ctypes.c_uint32),    # NEW - must be second
        # ... rest unchanged ...
    ]

    def validate(self):
        if self.api_version != EXPECTED_API_VERSION:
            raise RuntimeError(
                f"API version mismatch: C={self.api_version:#x}, "
                f"Python={EXPECTED_API_VERSION:#x}"
            )
        expected_size = ctypes.sizeof(PluginRuntimeArgs)
        if self.struct_size != expected_size:
            raise RuntimeError(
                f"Struct size mismatch: C={self.struct_size}, "
                f"Python={expected_size}"
            )
```

### 8.2 Build-Time Validation Script

```python
#!/usr/bin/env python3
# scripts/validate_struct_layout.py

import subprocess
import ctypes
import sys

# Compile and run C validation program
result = subprocess.run(
    ['./build/validate_struct_layout'],
    capture_output=True, text=True
)

# Parse C offsets
c_offsets = {}
for line in result.stdout.strip().split('\n'):
    key, value = line.split('=')
    c_offsets[key] = int(value)

# Compare with Python
from plugin_runtime_args import PluginRuntimeArgs

py_size = ctypes.sizeof(PluginRuntimeArgs)
if py_size != c_offsets['STRUCT_SIZE']:
    print(f"ERROR: Size mismatch C={c_offsets['STRUCT_SIZE']} Python={py_size}")
    sys.exit(1)

# Check each field offset
for name, ctype in PluginRuntimeArgs._fields_:
    field = getattr(PluginRuntimeArgs, name)
    py_offset = field.offset
    c_key = f'OFFSET_{name}'
    if c_key in c_offsets:
        if py_offset != c_offsets[c_key]:
            print(f"ERROR: {name} offset mismatch C={c_offsets[c_key]} Python={py_offset}")
            sys.exit(1)

print("All struct validations passed!")
sys.exit(0)
```

---

## 9. Conclusion

The current implementation's fragility stems from:
1. Manual synchronization of complex struct layouts
2. No version checking
3. Direct memory access without validation

The proposed solution addresses these by:
1. Adding explicit version and size validation
2. Providing a simpler bridge API that hides complexity
3. Optionally auto-generating bindings from C headers

**Recommended immediate action**: Add `api_version` and `struct_size` fields to catch mismatches early, preventing silent memory corruption and crashes.
