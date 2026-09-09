#include "plugin_config.h"
#include "plugin_driver.h"
#include "journal_buffer.h"
#include "image_tables.h"

#include <pthread.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

// Stub: base_tick_ns (utils.c) -- the runtime stores the PLC scan tick
// interval here (GCD of declared task intervals). Plugin drivers
// (plugin_driver.c) read it during arg construction, so a default
// stub value is enough for the unit tests.
uint64_t base_tick_ns = 0;

// Stub storage for the image tables (defined in image_tables.cpp in the real
// build). One symbol rather than fourteen, and it takes its shape from
// image_tables.h -- which is the point: the stub used to spell the fourteen
// arrays out by hand at BUFFER_SIZE=128 (project.yml) while plugin_driver.c
// saw them at 1024, a disagreement the linker was happy to accept.
image_tables_t g_image;

// Stub: plugin_manager_destroy (plcapp_manager.c)
void plugin_manager_destroy(PluginManager *manager)
{
    (void)manager;
}

// Stub: init_rt_mutex (utils.c) - weak so tests can override with their own mock
__attribute__((weak)) int init_rt_mutex(pthread_mutex_t *mutex)
{
    (void)mutex;
    return 0;
}

// Stub: journal_write_* (journal_buffer.c)
int journal_write_bool(journal_buffer_type_t type, uint16_t index,
                       uint8_t bit, bool value)
{
    (void)type;
    (void)index;
    (void)bit;
    (void)value;
    return 0;
}

int journal_write_byte(journal_buffer_type_t type, uint16_t index,
                       uint8_t value)
{
    (void)type;
    (void)index;
    (void)value;
    return 0;
}

int journal_write_int(journal_buffer_type_t type, uint16_t index,
                      uint16_t value)
{
    (void)type;
    (void)index;
    (void)value;
    return 0;
}

int journal_write_dint(journal_buffer_type_t type, uint16_t index,
                       uint32_t value)
{
    (void)type;
    (void)index;
    (void)value;
    return 0;
}

int journal_write_lint(journal_buffer_type_t type, uint16_t index,
                       uint64_t value)
{
    (void)type;
    (void)index;
    (void)value;
    return 0;
}

// The MatIEC-era flat-index API (get_var_list / get_var_size /
// get_var_count from plugin_utils.c) was removed alongside the rest of
// the MatIEC pipeline. Plugins now receive structured runtime args
// (plugin_runtime_args_t) constructed from the STruC++ debug map; the
// debugger ABI is exercised in test_debug_handler.c.

// Stubs: plc_tasks_reader_lock / plc_tasks_reader_unlock (plc_state_manager.cpp).
// scan_cycle_manager.c calls these around format_timing_stats_response to
// keep the reader from racing the bootstrap thread freeing plc_tasks. The
// real lock lives in plc_state_manager.cpp; tests don't pull that .cpp in,
// so we provide no-op stubs. Tests that exercise the lifecycle (rather
// than just the per-tracker math) will need to link the real symbols.
void plc_tasks_reader_lock(void)   {}
void plc_tasks_reader_unlock(void) {}

// Stub: log_* (log.c)
void log_info(const char *fmt, ...)
{
    (void)fmt;
}

void log_debug(const char *fmt, ...)
{
    (void)fmt;
}

void log_warn(const char *fmt, ...)
{
    (void)fmt;
}

void log_error(const char *fmt, ...)
{
    (void)fmt;
}