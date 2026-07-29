/**
 * @file plugin_types.h
 * @brief Common type definitions for OpenPLC plugins
 *
 * This header defines the essential types and structures shared between
 * the plugin driver system and native plugins. It provides:
 * - Logging function pointer types
 * - The plugin_runtime_args_t structure for runtime buffer access
 *
 * Both Python and native plugins receive a pointer to plugin_runtime_args_t
 * during initialization, giving them access to PLC I/O buffers, mutex
 * functions, and centralized logging.
 */

#ifndef PLUGIN_TYPES_H
#define PLUGIN_TYPES_H

#include "../lib/iec_types.h"
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>

/**
 * @brief Logging function pointer types
 *
 * These function pointers are provided to plugins for routing log messages
 * through the central OpenPLC logging system. Messages logged through these
 * functions will appear in the OpenPLC Editor's log viewer.
 */
typedef void (*plugin_log_info_func_t)(const char *fmt, ...);
typedef void (*plugin_log_debug_func_t)(const char *fmt, ...);
typedef void (*plugin_log_warn_func_t)(const char *fmt, ...);
typedef void (*plugin_log_error_func_t)(const char *fmt, ...);

/**
 * @brief Journal write function pointer types
 *
 * These function pointers allow plugins to write to I/O buffers through
 * the journal buffer system, ensuring race-condition-free writes.
 * All writes are applied atomically at the start of the next PLC scan cycle.
 *
 * Buffer type values (matching journal_buffer_type_t):
 *   0=BOOL_INPUT, 1=BOOL_OUTPUT, 2=BOOL_MEMORY
 *   3=BYTE_INPUT, 4=BYTE_OUTPUT
 *   5=INT_INPUT, 6=INT_OUTPUT, 7=INT_MEMORY
 *   8=DINT_INPUT, 9=DINT_OUTPUT, 10=DINT_MEMORY
 *   11=LINT_INPUT, 12=LINT_OUTPUT, 13=LINT_MEMORY
 */
typedef int (*plugin_journal_write_bool_func_t)(int type, int index, int bit, int value);
typedef int (*plugin_journal_write_byte_func_t)(int type, int index, int value);
typedef int (*plugin_journal_write_int_func_t)(int type, int index, int value);
typedef int (*plugin_journal_write_dint_func_t)(int type, int index, unsigned int value);
typedef int (*plugin_journal_write_lint_func_t)(int type, int index, unsigned long long value);

/**
 * @brief Variable-access function pointer types (STruC++ debugger surface)
 *
 * These wrap the strucpp_debug_* exports of the loaded program .so:
 *
 *   debug_array_count() / debug_elem_count(arr) — table sizes
 *   debug_size(arr, elem)                       — bytes consumed by force/read/write
 *   debug_read(arr, elem, dest)                 — read current value (respects forcing)
 *   debug_set(arr, elem, forcing, bytes, len)   — force / unforce
 *   debug_write(arr, elem, bytes, len)          — soft write (respects existing force)
 *
 * Variables are addressed by (arr, elem) — array and element indices into
 * the per-project debug Entry tables emitted by STruC++ codegen. The map
 * from human-readable names to (arr, elem) lives in debug-map.json,
 * produced by the editor at compile time. Plugins do not interact with
 * the map directly; the editor resolves user-selected variables and
 * writes the (arr, elem) tuples into each plugin's per-plugin config.
 *
 * All five thunks are NULL-safe: they short-circuit when no program is
 * loaded yet (ext_strucpp_debug_* still nullptr after symbols_init has
 * not run). debug_array_count returns 0 in that state.
 */
typedef uint8_t  (*plugin_debug_array_count_func_t)(void);
typedef uint16_t (*plugin_debug_elem_count_func_t)(uint8_t arr);
typedef uint16_t (*plugin_debug_size_func_t)(uint8_t arr, uint16_t elem);
typedef uint16_t (*plugin_debug_read_func_t)(uint8_t arr, uint16_t elem,
                                             uint8_t *dest);
typedef uint8_t  (*plugin_debug_set_func_t)(uint8_t arr, uint16_t elem,
                                            bool forcing,
                                            const uint8_t *bytes, uint16_t len);
typedef uint8_t  (*plugin_debug_write_func_t)(uint8_t arr, uint16_t elem,
                                              const uint8_t *bytes, uint16_t len);

/**
 * @brief PLC stop request from a plugin
 *
 * Asks the runtime to transition from RUNNING to STOPPED through the normal
 * shutdown path (stop all plugins cleanly, unload program, clear image
 * tables). Used by plugins that detect unrecoverable hardware faults — e.g.
 * a fieldbus plugin losing communication with its backplane — so the PLC
 * scan thread stops consuming stale inputs.
 *
 * The call is non-blocking: the runtime spawns a worker thread for the
 * transition and returns immediately. `reason` is logged at error level.
 * Safe to call from any plugin thread.
 */
typedef void (*plugin_request_plc_stop_func_t)(const char *reason);

/**
 * @brief PLC start request from a plugin
 *
 * Mirror of plugin_request_plc_stop: both are thin wrappers over the same
 * transition path the socket START / STOP commands drive, so a plugin-initiated
 * run/stop is byte-for-byte the flow the editor already exercises.
 *
 * Refused while the stored mode-switch position is STOP (see
 * plugin_set_switch_position_func_t) -- hardware is authoritative regardless of
 * who asks. Non-blocking; `reason` is logged. Safe to call from any plugin
 * thread, including while the PLC is stopped: the runtime keeps every plugin
 * mapped across a stop, so this is how a hardware STOP -> RUN edge starts the
 * PLC again.
 */
typedef void (*plugin_request_plc_start_func_t)(const char *reason);

/**
 * @brief Report the run/stop mode-switch position
 *
 * THIS is the hardware interface on Linux. The runtime never reads hardware and
 * never polls: the package decides how and how often it samples (GPIO
 * interrupt, sysfs poll, fieldbus callback, its own thread) and calls this
 * whenever the position changes.
 *
 * Stores only -- it starts nothing and stops nothing. On a switch change the
 * plugin calls this and THEN asks for the matching transition:
 *
 *     args->set_switch_position(PLC_SWITCH_STOP);
 *     args->request_plc_stop("mode switch moved to STOP");
 *
 * Always store the position BEFORE requesting the transition. On a falling edge
 * that closes the window where a start could slip in; on a rising edge it stops
 * the request being refused by the guard still reading the stale STOP.
 *
 * `position` is a plc_switch_t (0 = STOP, 1 = RUN). Declared as int here so
 * this header stays free of the plc_app include tree. Idempotent, and safe to
 * call from any plugin thread.
 *
 * Sample the switch from a thread started in init() and torn down in cleanup(),
 * NOT in start_loop() / stop_loop(): the runtime calls stop_loop when the PLC
 * stops, which is precisely when the position matters most. Make that start
 * idempotent, because init() is re-entered on each PLC start. Reporting the
 * initial position from init() also gates the boot auto-start, so a device
 * powered up with its switch in STOP never starts and then bounces.
 */
typedef void (*plugin_set_switch_position_func_t)(int position);

/**
 * @brief Read the current PLC state
 *
 * Returns 0 = STOPPED, 1 = RUNNING, 2 = ERROR (matching the values FC 0x49
 * reports on baremetal targets, so vendor code can share one mapping).
 *
 * There is no state-change callback: a plugin driving a panel LED polls this.
 * A device with no LED never calls it and the runtime is unaware.
 *
 * Poll it from the plugin's OWN thread -- the same sampler that reads the mode
 * switch is the natural home. Do NOT poll it from cycle_end: cycle_end fires
 * once per scan, so it stops being called the moment the PLC stops, and an LED
 * driven from there would freeze showing RUNNING exactly when it needs to show
 * STOPPED.
 *
 * IMPORTANT (applies to every field of plugin_runtime_args_t): the runtime
 * frees the args pointer as soon as init() returns. Copy the struct BY VALUE
 * during init -- `memcpy(&my_args, args, sizeof(plugin_runtime_args_t))` --
 * and call through the copy. Caching the pointer is a use-after-free; calling
 * a function pointer read back out of the freed struct crashes. See the
 * s7comm plugin for the established pattern.
 */
typedef int (*plugin_get_plc_state_func_t)(void);

/**
 * @brief Runtime buffer access structure for plugins
 *
 * This structure is passed to plugins during initialization, providing
 * access to:
 * - PLC I/O buffers (bool, byte, int, dint, lint for inputs/outputs/memory)
 * - Mutex functions for thread-safe buffer access
 * - Plugin-specific configuration file path
 * - Buffer size information
 * - Centralized logging functions
 *
 * Plugins read buffers under image_lock()/image_unlock() (flush-on-lock) and
 * write through journal_write_* (lock-free), keeping access race-free against
 * the PLC scan cycle.
 */
typedef struct
{
    /* Buffer pointers */
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
    IEC_BOOL *(*bool_memory)[8];

    /* Flush-on-lock image read API for thread-safe buffer access.
     *
     * image_lock() takes the runtime's image mutex and drains the journal so
     * the holder sees every committed write; image_unlock() releases it. Writes
     * never take this lock -- use journal_write_* (lock-free). Prefer the bulk
     * pattern for reads: lock, copy the region to a local buffer, unlock, then
     * do any slow work (network, conversion) on the buffer OUTSIDE the lock. */
    void (*image_lock)(void);
    void (*image_unlock)(void);

    /* STruC++ debugger variable-access surface.
     * Replaces the MatIEC-era flat-index API (get_var_list /
     * get_var_size / get_var_count). Plugins like OPC-UA receive
     * pre-resolved (arr, elem) tuples from the editor in their
     * per-plugin config and forward them through these thunks. */
    plugin_debug_array_count_func_t debug_array_count;
    plugin_debug_elem_count_func_t  debug_elem_count;
    plugin_debug_size_func_t        debug_size;
    plugin_debug_read_func_t        debug_read;
    plugin_debug_set_func_t         debug_set;
    plugin_debug_write_func_t       debug_write;

    /* Plugin configuration */
    char plugin_specific_config_file_path[256];

    /* Buffer size information */
    int buffer_size;
    int bits_per_buffer;

    /* Logging functions - route messages through central logging system */
    plugin_log_info_func_t log_info;
    plugin_log_debug_func_t log_debug;
    plugin_log_warn_func_t log_warn;
    plugin_log_error_func_t log_error;

    /* Journal write functions - race-condition-free buffer writes */
    plugin_journal_write_bool_func_t journal_write_bool;
    plugin_journal_write_byte_func_t journal_write_byte;
    plugin_journal_write_int_func_t journal_write_int;
    plugin_journal_write_dint_func_t journal_write_dint;
    plugin_journal_write_lint_func_t journal_write_lint;

    /* Async request to stop the whole PLC — see plugin_request_plc_stop_func_t. */
    plugin_request_plc_stop_func_t request_plc_stop;

    /* PLC base tick time in nanoseconds (GCD of all IEC task intervals).
     * Populated when the runtime initializes the plugin; may be 0 if
     * symbols are not yet resolved (plugin must guard against zero). */
    unsigned long long base_tick_ns;

    /* ---------------------------------------------------------------------
     * Run/stop control. Appended at the end of the struct so plugin binaries
     * compiled against an earlier layout keep their field offsets.
     *
     * A plugin that ignores all three behaves exactly as before: the switch
     * position stays at its RUN default, so every start path is unguarded.
     * ------------------------------------------------------------------- */

    /* Async request to run — see plugin_request_plc_start_func_t. */
    plugin_request_plc_start_func_t request_plc_start;

    /* Report the mode-switch position — see
     * plugin_set_switch_position_func_t. The hardware interface on Linux. */
    plugin_set_switch_position_func_t set_switch_position;

    /* Current PLC state, for driving a status LED — see
     * plugin_get_plc_state_func_t. */
    plugin_get_plc_state_func_t get_plc_state;
} plugin_runtime_args_t;

#endif /* PLUGIN_TYPES_H */
