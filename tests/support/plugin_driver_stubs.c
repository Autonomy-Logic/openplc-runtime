// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#include "plugin_config.h"
#include "plugin_driver.h"
#include "journal_buffer.h"

#include <pthread.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

// Stub: base_tick_ns (utils.c) -- the runtime stores the PLC scan tick
// interval here (GCD of declared task intervals). Plugin drivers
// (plugin_driver.c) read it during arg construction, so a default
// stub value is enough for the unit tests.
uint64_t base_tick_ns = 0;

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

// No-op stubs: scan_cycle_manager.c takes these around format_timing_
// stats_response. Tests exercising the real lifecycle must link the
// real plc_state_manager.cpp symbols instead.
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