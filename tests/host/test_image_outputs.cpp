// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Host test: image_tables_zero_outputs clears every output slot and nothing else.

#include "image_tables.h"
#include "journal_buffer.h"

#include <cstdarg>
#include <cstdio>

extern "C" {
void log_info(const char *, ...) {}
void log_warn(const char *, ...) {}
void log_error(const char *, ...) {}
void log_debug(const char *, ...) {}
void *plugin_manager_get_symbol(PluginManager *, const char *) { return nullptr; }
void *plugin_manager_try_get_symbol(PluginManager *, const char *) { return nullptr; }
void journal_apply_and_clear(void) {}
int  journal_write_bool(journal_buffer_type_t, uint16_t, uint8_t, bool) { return 0; }
int  journal_write_byte(journal_buffer_type_t, uint16_t, uint8_t) { return 0; }
int  journal_write_int(journal_buffer_type_t, uint16_t, uint16_t) { return 0; }
int  journal_write_dint(journal_buffer_type_t, uint16_t, uint32_t) { return 0; }
int  journal_write_lint(journal_buffer_type_t, uint16_t, uint64_t) { return 0; }
uint64_t base_tick_ns           = 0;
char    *ext_strucpp_program_md5 = nullptr;
}

static int g_failures = 0;

#define CHECK(cond, what)                                                                          \
    do                                                                                             \
    {                                                                                              \
        if (!(cond))                                                                               \
        {                                                                                          \
            std::printf("  FAIL  %s\n", what);                                                     \
            g_failures++;                                                                          \
        }                                                                                          \
    } while (0)

int main()
{
    std::printf("image_tables: outputs forced to 0 on stop\n");

    image_tables_fill_null_pointers();
    for (int i = 0; i < BUFFER_SIZE; ++i)
    {
        for (int b = 0; b < 8; ++b)
        {
            *bool_output[i][b] = 1;
            *bool_input[i][b]  = 1;
            *bool_memory[i][b] = 1;
        }
        *byte_output[i] = 0xAB;
        *int_output[i]  = 0xABCD;
        *dint_output[i] = 0xABCDEF01u;
        *lint_output[i] = 0xABCDEF0123456789ull;
        *int_input[i]   = 7;
        *int_memory[i]  = 9;
    }

    image_tables_zero_outputs();

    bool outputs_zero = true, others_kept = true;
    for (int i = 0; i < BUFFER_SIZE; ++i)
    {
        for (int b = 0; b < 8; ++b)
        {
            outputs_zero &= *bool_output[i][b] == 0;
            others_kept &= *bool_input[i][b] == 1 && *bool_memory[i][b] == 1;
        }
        outputs_zero &= *byte_output[i] == 0 && *int_output[i] == 0 && *dint_output[i] == 0 &&
                        *lint_output[i] == 0;
        others_kept &= *int_input[i] == 7 && *int_memory[i] == 9;
    }
    CHECK(outputs_zero, "every %Q slot is 0");
    CHECK(others_kept, "inputs and memory are untouched");

    image_tables_clear_null_pointers();
    image_tables_zero_outputs();
    CHECK(true, "zeroing with unbound (NULL) slots does not crash");

    if (g_failures == 0)
        std::printf("all cases passed\n");
    return g_failures == 0 ? 0 : 1;
}
