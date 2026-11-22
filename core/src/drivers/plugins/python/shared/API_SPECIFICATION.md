# SafeBufferAccess API Specification

## Overview
This document specifies the complete public API of the `SafeBufferAccess` class that must be maintained for backward compatibility during refactoring.

## Class Structure

### Constructor
```python
SafeBufferAccess(runtime_args: PluginRuntimeArgs)
```

**Parameters:**
- `runtime_args`: `PluginRuntimeArgs` instance

**Attributes:**
- `is_valid: bool` - Whether the instance is properly initialized
- `error_msg: str` - Error message if initialization failed

### Public Methods

#### Invalidation
```python
invalidate() -> None
```
Makes the accessor permanently unusable, for a plugin whose stop must cut its access to the
runtime. Afterwards `is_valid` is `False`, `error_msg` is `"Buffer accessor invalidated"`, and
every mutex, buffer, batch and configuration operation returns its failure result (`None` or
`False`, `[]`/`{}` for batch and map results, `""` for the config path) with that message,
without calling into `runtime_args`. A second call does nothing. Call it only when no mutex
acquired through the accessor is held.

#### Mutex Management
```python
acquire_mutex() -> (bool, str)
release_mutex() -> (bool, str)
```

#### Boolean Buffer Operations
```python
read_bool_input(buffer_idx: int, bit_idx: int, thread_safe: bool = True) -> (bool, str)
read_bool_output(buffer_idx: int, bit_idx: int, thread_safe: bool = True) -> (bool, str)
write_bool_output(buffer_idx: int, bit_idx: int, value: bool, thread_safe: bool = True) -> (bool, str)
```

#### Byte Buffer Operations
```python
read_byte_input(buffer_idx: int, thread_safe: bool = True) -> (int, str)
read_byte_output(buffer_idx: int, thread_safe: bool = True) -> (int, str)
write_byte_output(buffer_idx: int, value: int, thread_safe: bool = True) -> (bool, str)
```

#### Integer Buffer Operations (16-bit)
```python
read_int_input(buffer_idx: int, thread_safe: bool = True) -> (int, str)
read_int_output(buffer_idx: int, thread_safe: bool = True) -> (int, str)
write_int_output(buffer_idx: int, value: int, thread_safe: bool = True) -> (bool, str)
read_int_memory(buffer_idx: int, thread_safe: bool = True) -> (int, str)
write_int_memory(buffer_idx: int, value: int, thread_safe: bool = True) -> (bool, str)
```

#### Double Integer Buffer Operations (32-bit)
```python
read_dint_input(buffer_idx: int, thread_safe: bool = True) -> (int, str)
read_dint_output(buffer_idx: int, thread_safe: bool = True) -> (int, str)
write_dint_output(buffer_idx: int, value: int, thread_safe: bool = True) -> (bool, str)
read_dint_memory(buffer_idx: int, thread_safe: bool = True) -> (int, str)
write_dint_memory(buffer_idx: int, value: int, thread_safe: bool = True) -> (bool, str)
```

#### Long Integer Buffer Operations (64-bit)
```python
read_lint_input(buffer_idx: int, thread_safe: bool = True) -> (int, str)
read_lint_output(buffer_idx: int, thread_safe: bool = True) -> (int, str)
write_lint_output(buffer_idx: int, value: int, thread_safe: bool = True) -> (bool, str)
read_lint_memory(buffer_idx: int, thread_safe: bool = True) -> (int, str)
write_lint_memory(buffer_idx: int, value: int, thread_safe: bool = True) -> (bool, str)
```

#### Batch Operations
```python
batch_read_values(operations: List[Tuple]) -> (List[Tuple], str)
batch_write_values(operations: List[Tuple]) -> (List[Tuple], str)
batch_mixed_operations(read_operations: List[Tuple], write_operations: List[Tuple]) -> (Dict, str)
```

#### Debug/Variable Operations
```python
get_var_list(indexes: List[int]) -> (List[int], str)
get_var_size(index: int) -> (int, str)
get_var_value(index: int) -> (Any, str)
set_var_value(index: int, value: Any) -> (bool, str)
get_var_count() -> (int, str)
get_var_info(index: int) -> (Dict, str)
```

#### Configuration Operations
```python
get_config_path() -> (str, str)
get_config_file_args_as_map() -> (Dict, str)
```

## Parameter Details

### Common Parameters
- `buffer_idx: int` - Buffer index (0-based)
- `bit_idx: int` - Bit index within buffer (for boolean operations)
- `value: int/bool` - Value to write
- `thread_safe: bool = True` - Whether to use mutex for thread-safe access

### Value Ranges
- `bool`: `True`/`False`
- `byte`: `0-255`
- `int`: `0-65535` (16-bit unsigned)
- `dint`: `0-4294967295` (32-bit unsigned)
- `lint`: `0-18446744073709551615` (64-bit unsigned)

### Return Values
- **Read operations**: `(value, error_message: str)`
- **Write operations**: `(success: bool, error_message: str)`
- **Batch operations**: `(results: List/Dict, error_message: str)`

## Error Handling
- Invalid buffer/bit indices return appropriate error messages
- Out-of-range values return validation errors
- Mutex acquisition failures return error messages
- All operations return consistent `(result, message)` tuples

## Thread Safety
- Default behavior uses mutex for thread-safe access
- `thread_safe=False` bypasses mutex (for manual control)
- Mutex operations: `acquire_mutex()`/`release_mutex()`

## Batch Operations Format

### Read Operations
```python
[
    ('buffer_type', buffer_idx, bit_idx),  # for bool operations
    ('buffer_type', buffer_idx),           # for other types
    # ...
]
```

### Write Operations
```python
[
    ('buffer_type', buffer_idx, value, bit_idx),  # for bool operations
    ('buffer_type', buffer_idx, value),           # for other types
    # ...
]
```

### Buffer Types
- `'bool_input'`, `'bool_output'`
- `'byte_input'`, `'byte_output'`
- `'int_input'`, `'int_output'`, `'int_memory'`
- `'dint_input'`, `'dint_output'`, `'dint_memory'`
- `'lint_input'`, `'lint_output'`, `'lint_memory'`

## Compatibility Requirements
- All existing tests must pass without modification
- All existing plugins must continue to work
- API signatures must remain identical
- Behavior must be preserved exactly
