# Journal Buffer Architecture

## Overview

This document describes the architecture for a journaled buffer system to handle plugin writes to OpenPLC image tables. The journal buffer provides a race-condition-free mechanism for multiple plugins (both native and Python) to write to shared I/O buffers with deterministic conflict resolution.

## Problem Statement

### Current Architecture Issues

The current plugin architecture has several limitations when multiple plugins need to write to the same image table locations:

1. **Race Conditions**: Plugins run in separate threads and can write to buffers at arbitrary times, causing race conditions.

2. **Zero vs. Uninitialized Ambiguity**: When a plugin copies its buffer to the core image tables, there's no way to distinguish between "intentionally wrote zero" and "never touched this location."

3. **Asynchronous Plugin Timing**: Python plugins run in their own threads without `cycle_start`/`cycle_end` hooks, so their writes can occur at any point relative to the PLC scan cycle.

4. **No Conflict Resolution**: When two plugins write to the same location, the outcome depends on thread timing, leading to unpredictable behavior.

### Example Scenario

Consider a system with both S7Comm (native) and Modbus Master (Python) plugins:

```
Timeline:
|---- PLC Scan Cycle N ----|--- Sleep ---|---- PLC Scan Cycle N+1 ----|
  cycle_start    cycle_end                  cycle_start      cycle_end
       │             │                           │               │
  S7→OpenPLC    OpenPLC→S7                  S7→OpenPLC      OpenPLC→S7
                                                 ↑
                                            Overwrites!
                     │
              Modbus Master writes
              new input value here
```

1. **cycle_end N**: OpenPLC inputs (value X) → S7 buffer
2. **Sleep period**: Modbus Master reads remote I/O, writes new value Y to OpenPLC inputs
3. **cycle_start N+1**: S7 buffer (still has old value X) → OpenPLC inputs (overwrites Y with X!)

## Proposed Solution: Journal Buffer

### Core Concept

Instead of plugins writing directly to image tables, all writes go through a journal buffer. The journal is applied atomically at the start of each PLC scan cycle, with "last writer wins" semantics based on write sequence.

```
┌─────────────────────────────────────────────────────────────────┐
│                        PLC Scan Cycle                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐                         ┌─────────────┐       │
│  │   JOURNAL   │  ──── Apply ────────►   │   CORE      │       │
│  │   BUFFER    │      (cycle_start)      │   IMAGE     │       │
│  │             │                         │   TABLES    │       │
│  │  seq=1: ... │                         │             │       │
│  │  seq=2: ... │                         │ bool_input  │       │
│  │  seq=3: ... │                         │ bool_output │       │
│  │  seq=4: ... │                         │ int_input   │       │
│  └──────▲──────┘                         │ ...         │       │
│         │                                └──────┬──────┘       │
│         │                                       │               │
│     WRITES                                   READS              │
│     (journal)                               (direct)            │
│         │                                       │               │
│  ┌──────┴────────────────────────────────────────▼──────┐      │
│  │                                                       │      │
│  │   ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐│      │
│  │   │Plugin A │  │Plugin B │  │Plugin C │  │Plugin D ││      │
│  │   │(S7Comm) │  │(Modbus) │  │(Python) │  │(Native) ││      │
│  │   └─────────┘  └─────────┘  └─────────┘  └─────────┘│      │
│  │                                                       │      │
│  └───────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

### Key Design Principles

1. **Writes Go to Journal**: All plugin writes are recorded in a journal buffer with a sequence number.

2. **Reads Are Direct**: Plugins read directly from image tables, getting the most recent applied values.

3. **Atomic Application**: The entire journal is applied at `cycle_start`, before plugin hooks run.

4. **Last Writer Wins**: Entries are applied in sequence order. If multiple plugins write to the same location, the one with the highest sequence number wins.

5. **Sequence Reset Per Cycle**: The sequence counter resets to zero when the journal is cleared after application.

## Detailed Architecture

### Journal Entry Structure

```c
typedef struct {
    uint32_t sequence;          /* Auto-increment, determines apply order */
    uint8_t  buffer_type;       /* journal_buffer_type_t enum */
    uint8_t  bit_index;         /* For bool types: 0-7, for others: 0xFF */
    uint16_t index;             /* Buffer array index */
    uint64_t value;             /* Value to write (sized for largest type) */
} journal_entry_t;
```

**Size**: 16 bytes per entry (with padding for alignment, may be 20 bytes)

### Buffer Type Enumeration

```c
typedef enum {
    JOURNAL_BOOL_INPUT = 0,
    JOURNAL_BOOL_OUTPUT,
    JOURNAL_BOOL_MEMORY,
    JOURNAL_BYTE_INPUT,
    JOURNAL_BYTE_OUTPUT,
    JOURNAL_INT_INPUT,
    JOURNAL_INT_OUTPUT,
    JOURNAL_INT_MEMORY,
    JOURNAL_DINT_INPUT,
    JOURNAL_DINT_OUTPUT,
    JOURNAL_DINT_MEMORY,
    JOURNAL_LINT_INPUT,
    JOURNAL_LINT_OUTPUT,
    JOURNAL_LINT_MEMORY,
    JOURNAL_TYPE_COUNT
} journal_buffer_type_t;
```

### Static Journal Buffer

```c
#define JOURNAL_MAX_ENTRIES 40960

static journal_entry_t g_entries[JOURNAL_MAX_ENTRIES];
static size_t g_count = 0;
static uint32_t g_next_sequence = 0;
static pthread_mutex_t g_journal_mutex = PTHREAD_MUTEX_INITIALIZER;
```

**Memory Usage**: ~20KB for 1024 entries (fixed, no dynamic allocation)

### Thread Safety Model

Two mutexes are involved:

1. **Journal Mutex** (`g_journal_mutex`): Protects journal buffer state (entries, count, sequence)
2. **Image Table Mutex** (`image_mutex`): Protects core image tables (existing runtime mutex)

**Lock Ordering** (to prevent deadlock): Always acquire `image_mutex` before `journal_mutex` when both are needed.

### Write Flow

```
Plugin calls journal_write_*()
         │
         ▼
┌─────────────────────────┐
│ Acquire journal_mutex   │
└───────────┬─────────────┘
            │
            ▼
    ┌───────────────┐
    │ Journal Full? │
    └───────┬───────┘
            │
     Yes    │    No
    ┌───────┴───────┐
    │               │
    ▼               │
┌─────────────┐     │
│ Emergency   │     │
│ Flush       │     │
│ (see below) │     │
└──────┬──────┘     │
       │            │
       └─────┬──────┘
             │
             ▼
┌─────────────────────────┐
│ Add entry with          │
│ next sequence number    │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ Release journal_mutex   │
└─────────────────────────┘
```

### Apply Flow (at cycle_start)

```
PLC Cycle Manager calls journal_apply_and_clear()
(image_mutex already held)
         │
         ▼
┌─────────────────────────┐
│ Acquire journal_mutex   │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ For each entry in order:│
│   Apply to image table  │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ Reset count = 0         │
│ Reset sequence = 0      │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ Release journal_mutex   │
└─────────────────────────┘
```

### Emergency Flush

When the journal buffer is full and a new write is attempted:

```
(Already holding journal_mutex)
         │
         ▼
┌─────────────────────────┐
│ Release journal_mutex   │  ← Prevent deadlock
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ Acquire image_mutex     │  ← Lock ordering
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ Acquire journal_mutex   │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ Apply all entries       │
│ Clear journal           │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│ Release image_mutex     │
└───────────┬─────────────┘
            │
            ▼
(Continue with write, still holding journal_mutex)
```

## Public API

### C API (for native plugins)

```c
/* Initialize the journal buffer system */
int journal_init(const journal_buffer_ptrs_t* buffer_ptrs);

/* Cleanup journal buffer resources */
void journal_cleanup(void);

/* Write functions */
int journal_write_bool(journal_buffer_type_t type, uint16_t index,
                       uint8_t bit, bool value);
int journal_write_byte(journal_buffer_type_t type, uint16_t index,
                       uint8_t value);
int journal_write_int(journal_buffer_type_t type, uint16_t index,
                      uint16_t value);
int journal_write_dint(journal_buffer_type_t type, uint16_t index,
                       uint32_t value);
int journal_write_lint(journal_buffer_type_t type, uint16_t index,
                       uint64_t value);

/* Apply pending writes (called at cycle_start, image_mutex must be held) */
void journal_apply_and_clear(void);

/* Diagnostics */
size_t journal_pending_count(void);
bool journal_is_initialized(void);
```

### Python API (for Python plugins)

The existing `SafeBufferAccess` class will be extended to route writes through the journal:

```python
class SafeBufferAccess:
    def write_bool(self, buf_type: int, index: int, bit: int, value: bool) -> bool:
        """Write boolean value to journal buffer."""
        return self._journal_write_bool(buf_type, index, bit, value)

    def write_int(self, buf_type: int, index: int, value: int) -> bool:
        """Write 16-bit integer value to journal buffer."""
        return self._journal_write_int(buf_type, index, value)

    # ... similar for other types

    def read_bool(self, buf_type: int, index: int, bit: int) -> Optional[bool]:
        """Read boolean value directly from image table (unchanged)."""
        return self._direct_read_bool(buf_type, index, bit)
```

## Integration Points

### 1. PLC State Manager Integration

In `plc_state_manager.c`, add journal application at cycle start:

```c
void plc_cycle_start(void) {
    /* Acquire image table mutex */
    pthread_mutex_lock(&image_mutex);

    /* Apply journal entries FIRST, before any plugin hooks */
    journal_apply_and_clear();

    /* Call plugin cycle_start hooks */
    plugin_driver_cycle_start();

    /* ... rest of cycle start logic */
}
```

### 2. Runtime Initialization

In runtime initialization, set up journal buffer pointers:

```c
int runtime_init(void) {
    /* ... existing init code ... */

    /* Initialize journal buffer */
    journal_buffer_ptrs_t ptrs = {
        .bool_input = bool_input,
        .bool_output = bool_output,
        .bool_memory = bool_memory,
        .byte_input = byte_input,
        .byte_output = byte_output,
        .int_input = int_input,
        .int_output = int_output,
        .int_memory = int_memory,
        .dint_input = dint_input,
        .dint_output = dint_output,
        .dint_memory = dint_memory,
        .lint_input = lint_input,
        .lint_output = lint_output,
        .lint_memory = lint_memory,
        .buffer_size = BUFFER_SIZE,
        .image_mutex = &bufferLock  /* existing buffer mutex */
    };

    if (journal_init(&ptrs) != 0) {
        log_error("Failed to initialize journal buffer");
        return -1;
    }

    /* ... rest of init ... */
}
```

### 3. Native Plugin Migration

Native plugins using direct buffer access need to migrate to journal writes:

**Before (direct write):**
```c
void sync_to_openplc(void) {
    IEC_BOOL** arr = runtime_args->bool_output;
    if (arr[index][bit] != NULL) {
        *arr[index][bit] = value;  /* Direct write */
    }
}
```

**After (journal write):**
```c
void sync_to_openplc(void) {
    journal_write_bool(JOURNAL_BOOL_OUTPUT, index, bit, value);
}
```

### 4. Python Plugin Migration

Python plugins using `SafeBufferAccess` will automatically use the journal when the `SafeBufferAccess` implementation is updated. No changes required in plugin code.

## Conflict Resolution Examples

### Example 1: Two Plugins Write Same Location

```
Time 0ms:  Plugin A writes %QX0.0 = TRUE  → seq=1
Time 5ms:  Plugin B writes %QX0.0 = FALSE → seq=2
Time 10ms: cycle_start - apply journal

Apply order:
  1. seq=1: %QX0.0 = TRUE
  2. seq=2: %QX0.0 = FALSE  ← This wins (last writer)

Final value: %QX0.0 = FALSE
```

### Example 2: Modbus and S7 Conflict

```
Time 0ms:   S7 client writes %IX0.0 = TRUE → seq=1
Time 50ms:  Modbus Master reads remote I/O, writes %IX0.0 = FALSE → seq=2
Time 100ms: cycle_start - apply journal

Result: %IX0.0 = FALSE (Modbus Master wins because it wrote later)
```

### Example 3: Emergency Flush

```
Time 0ms:   Writes 1-1023 accumulate in journal
Time 50ms:  Write 1024 triggers emergency flush
            - Apply seq 1-1023 to image tables
            - Clear journal
            - Add write 1024 as seq=0
Time 100ms: Writes 1-500 accumulate
Time 200ms: cycle_start - apply journal (seq 0-500)
```

## Performance Considerations

### Memory Usage

- **Journal Buffer**: 1024 entries × ~20 bytes = ~20KB (static allocation)
- **No per-plugin buffers needed**: Single shared journal
- **Total overhead**: Minimal compared to current architecture

### Latency

- **Write latency**: Acquiring journal mutex (~microseconds)
- **Apply latency**: Iterating 1024 entries maximum (~milliseconds)
- **Read latency**: Unchanged (direct buffer access)

### Throughput

- **Maximum writes per cycle**: 40960 (configurable via `JOURNAL_MAX_ENTRIES`); drain cost follows the writes actually made, not the capacity
- **Emergency flush**: Handles overflow gracefully without data loss

## Implementation Phases

### Phase 1: Core Journal Buffer (C Implementation)

**Files to create:**
- `core/src/plc_app/journal_buffer.h` - Public API header
- `core/src/plc_app/journal_buffer.c` - Implementation

**Tasks:**
1. Implement journal entry structure and static buffer
2. Implement write functions with mutex protection
3. Implement `journal_apply_and_clear()` function
4. Implement emergency flush logic
5. Add unit tests for journal operations

**Estimated effort**: 2-3 days

### Phase 2: Runtime Integration

**Files to modify:**
- `core/src/plc_app/plc_state_manager.c` - Add journal apply at cycle_start
- `core/src/plc_app/main.c` or equivalent - Initialize journal at startup

**Tasks:**
1. Initialize journal buffer during runtime startup
2. Call `journal_apply_and_clear()` at cycle_start
3. Cleanup journal buffer at runtime shutdown
4. Integration testing with PLC scan cycle

**Estimated effort**: 1-2 days

### Phase 3: Python API Extension

**Files to modify:**
- `core/src/drivers/plugins/python/shared/safe_buffer_access.py` - Route writes to journal
- `core/src/drivers/plugins/python/shared/__init__.py` - Export new functions

**Tasks:**
1. Add ctypes bindings for journal write functions
2. Modify `SafeBufferAccess` write methods to use journal
3. Keep read methods unchanged (direct buffer access)
4. Update documentation

**Estimated effort**: 1-2 days

### Phase 4: Native Plugin Migration

**Files to modify:**
- `core/src/drivers/plugins/native/s7comm/s7comm_plugin.cpp` - Use journal writes
- Any other native plugins with direct buffer writes

**Tasks:**
1. Replace direct buffer writes with `journal_write_*()` calls
2. Remove plugin-specific double-buffering logic (no longer needed)
3. Simplify `cycle_start`/`cycle_end` hooks
4. Integration testing

**Estimated effort**: 1-2 days per plugin

### Phase 5: Python Plugin Migration

**Files to modify:**
- `core/src/drivers/plugins/python/modbus_master/modbus_master_plugin.py`
- `core/src/drivers/plugins/python/modbus_slave/simple_modbus.py`
- Any other Python plugins

**Tasks:**
1. Verify plugins work with new `SafeBufferAccess` (should be automatic)
2. Integration testing
3. Performance testing

**Estimated effort**: 1-2 days

### Phase 6: Testing and Documentation

**Tasks:**
1. End-to-end testing with multiple plugins
2. Stress testing (high write volume, emergency flush scenarios)
3. Performance benchmarking
4. Update plugin development documentation
5. Update architecture documentation

**Estimated effort**: 2-3 days

## Total Implementation Estimate

| Phase | Effort |
|-------|--------|
| Phase 1: Core Journal Buffer | 2-3 days |
| Phase 2: Runtime Integration | 1-2 days |
| Phase 3: Python API Extension | 1-2 days |
| Phase 4: Native Plugin Migration | 1-2 days per plugin |
| Phase 5: Python Plugin Migration | 1-2 days |
| Phase 6: Testing and Documentation | 2-3 days |
| **Total** | **~10-15 days** |

## Future Enhancements (Out of Scope)

The following features are explicitly NOT part of this design but could be added later:

1. **Write Coalescing**: Merge multiple writes to the same location within a cycle
2. **Read-After-Write Consistency**: Allow plugins to read their own pending writes
3. **Journal Persistence**: Save journal to disk for crash recovery
4. **Journal Replay**: Replay journal for debugging/simulation
5. **Per-Plugin Statistics**: Track write counts per plugin for diagnostics

## Appendix A: Comparison with Alternative Approaches

### Alternative 1: Per-Plugin Shadow Buffers

Each plugin gets its own copy of image tables. Merge at cycle boundaries.

**Pros**: Complete isolation
**Cons**: High memory usage (N copies), complex merge logic, priority configuration needed

### Alternative 2: Timestamp-Based Conflict Resolution

Each write includes a timestamp. Latest timestamp wins.

**Pros**: Most recent data always wins
**Cons**: Clock synchronization issues, storage overhead, complexity

### Alternative 3: Priority-Based System

Each plugin has a priority. Higher priority wins conflicts.

**Pros**: Deterministic, configurable
**Cons**: Requires priority configuration, may not reflect actual "freshness" of data

### Why Journal Buffer?

The journal buffer approach was chosen because:

1. **No configuration needed**: "Last writer wins" is intuitive
2. **Memory efficient**: Single shared buffer
3. **Simple API**: Just replace write calls
4. **Deterministic**: Same sequence = same result
5. **Debuggable**: Can log/inspect journal contents

## Appendix B: Glossary

| Term | Definition |
|------|------------|
| **Image Table** | Core OpenPLC buffer arrays (bool_input, int_output, etc.) |
| **Journal Entry** | Single write record with sequence, type, index, and value |
| **Journal Buffer** | Static array holding pending write entries |
| **Sequence Number** | Auto-incrementing counter determining apply order |
| **Emergency Flush** | Immediate journal application when buffer is full |
| **Apply** | Process of writing journal entries to image tables |
| **Cycle Start** | Beginning of PLC scan cycle, when journal is applied |
