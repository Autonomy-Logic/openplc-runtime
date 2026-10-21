// SPDX-License-Identifier: GPL-3.0-only
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

    // The tables are heap-allocated and each carries its own length, so the
    // image has to exist before anything can be bound into it. Deliberately
    // UNEQUAL lengths: a zeroing loop driven by one figure passes on a square
    // image and walks off the end of the shortest table on a real one.
    image_sizes_t sizes;
    for (int t = 0; t < IMAGE_TABLE_COUNT; ++t)
        sizes.elements[t] = (uint32_t)(4 + t);
    if (!image_tables_alloc(&sizes))
    {
        std::printf("  FAIL  image_tables_alloc\n");
        return 1;
    }

    image_tables_fill_null_pointers();
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_BOOL_OUTPUT]; ++i)
        for (int b = 0; b < 8; ++b)
            *g_image.bool_output[i][b] = 1;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_BOOL_INPUT]; ++i)
        for (int b = 0; b < 8; ++b)
            *g_image.bool_input[i][b] = 1;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_BOOL_MEMORY]; ++i)
        for (int b = 0; b < 8; ++b)
            *g_image.bool_memory[i][b] = 1;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_BYTE_OUTPUT]; ++i)
        *g_image.byte_output[i] = 0xAB;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_INT_OUTPUT]; ++i)
        *g_image.int_output[i] = 0xABCD;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_DINT_OUTPUT]; ++i)
        *g_image.dint_output[i] = 0xABCDEF01u;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_LINT_OUTPUT]; ++i)
        *g_image.lint_output[i] = 0xABCDEF0123456789ull;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_INT_INPUT]; ++i)
        *g_image.int_input[i] = 7;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_INT_MEMORY]; ++i)
        *g_image.int_memory[i] = 9;

    image_tables_zero_outputs();

    bool outputs_zero = true, others_kept = true;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_BOOL_OUTPUT]; ++i)
        for (int b = 0; b < 8; ++b)
            outputs_zero &= *g_image.bool_output[i][b] == 0;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_BOOL_INPUT]; ++i)
        for (int b = 0; b < 8; ++b)
            others_kept &= *g_image.bool_input[i][b] == 1;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_BOOL_MEMORY]; ++i)
        for (int b = 0; b < 8; ++b)
            others_kept &= *g_image.bool_memory[i][b] == 1;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_BYTE_OUTPUT]; ++i)
        outputs_zero &= *g_image.byte_output[i] == 0;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_INT_OUTPUT]; ++i)
        outputs_zero &= *g_image.int_output[i] == 0;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_DINT_OUTPUT]; ++i)
        outputs_zero &= *g_image.dint_output[i] == 0;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_LINT_OUTPUT]; ++i)
        outputs_zero &= *g_image.lint_output[i] == 0;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_INT_INPUT]; ++i)
        others_kept &= *g_image.int_input[i] == 7;
    for (uint32_t i = 0; i < sizes.elements[IMAGE_TABLE_INT_MEMORY]; ++i)
        others_kept &= *g_image.int_memory[i] == 9;
    CHECK(outputs_zero, "every %Q slot is 0");
    CHECK(others_kept, "inputs and memory are untouched");

    image_tables_clear_null_pointers();
    image_tables_zero_outputs();
    CHECK(true, "zeroing with unbound (NULL) slots does not crash");

    image_tables_free();
    image_tables_zero_outputs();
    CHECK(true, "zeroing with no image allocated does not crash");

    if (g_failures == 0)
        std::printf("all cases passed\n");
    return g_failures == 0 ? 0 : 1;
}
