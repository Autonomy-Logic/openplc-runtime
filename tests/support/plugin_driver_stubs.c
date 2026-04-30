#include "plugin_config.h"
#include "plugin_driver.h"
#include "journal_buffer.h"
#include "plugin_utils.h"

#include <pthread.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

// Stub: ext_common_ticktime__ (utils.c) -- the runtime publishes the PLC
// scan tick interval here. Plugin drivers (plugin_driver.c) read it during
// arg construction, so a NULL stub is enough for the unit tests.
unsigned long long *ext_common_ticktime__ = NULL;

// Stub implementations for external buffer variables (image_tables.c)
IEC_BOOL *bool_input[BUFFER_SIZE][8];
IEC_BOOL *bool_output[BUFFER_SIZE][8];
IEC_BYTE *byte_input[BUFFER_SIZE];
IEC_BYTE *byte_output[BUFFER_SIZE];
IEC_UINT *int_input[BUFFER_SIZE];
IEC_UINT *int_output[BUFFER_SIZE];
IEC_UDINT *dint_input[BUFFER_SIZE];
IEC_UDINT *dint_output[BUFFER_SIZE];
IEC_ULINT *lint_input[BUFFER_SIZE];
IEC_ULINT *lint_output[BUFFER_SIZE];
IEC_UINT *int_memory[BUFFER_SIZE];
IEC_UDINT *dint_memory[BUFFER_SIZE];
IEC_ULINT *lint_memory[BUFFER_SIZE];
IEC_BOOL *bool_memory[BUFFER_SIZE][8];

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

// Stub: get_var_* (plugin_utils.c)
void get_var_list(size_t num_vars, size_t *indexes, void **result)
{
    (void)num_vars;
    (void)indexes;
    (void)result;
}

size_t get_var_size(size_t idx)
{
    (void)idx;
    return 0;
}

uint16_t get_var_count(void)
{
    return 0;
}

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