// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/*
 * debug_handler_mocks.c — implementation of the test-side debugger ABI
 * fakes. See debug_handler_mocks.h for the contract.
 *
 * These functions are wired into the runtime by overwriting the
 * ext_strucpp_debug_* function pointers (declared extern in
 * image_tables.h, defined in image_tables.cpp) and the program-MD5
 * char* pointer (declared in utils.h, defined in utils.c).
 *
 * Because the runtime declares those externs in C++ (image_tables.cpp)
 * and we install from C, the declarations are reproduced here under
 * `extern "C"`-equivalent linkage. The installed function pointer
 * signatures must match exactly — a mismatch silently corrupts the
 * call frame.
 */

#include "debug_handler_mocks.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* The runtime's normal home for these is image_tables.cpp (function
 * pointers) and utils.c (md5 char *). image_tables.cpp pulls in the
 * full strucpp ABI and a lot of C++ infrastructure that's irrelevant
 * to the debugger wire-protocol tests, so we provide the storage here
 * in test-support land instead. Ceedling resolves the externs in
 * debug_handler.c against these definitions and never compiles
 * image_tables.cpp.
 *
 * scan_counter (referenced by debug_handler.c for the tick field of
 * GET / GET_LIST responses) is owned by utils.c — that file is small
 * and gets pulled in normally.
 *
 * If a future test ever wants the real image_tables.cpp definitions,
 * gate this block with #ifndef MOCK_DEBUG_OWNS_EXTERNS or split it
 * into a separate support file. */
uint8_t  (*ext_strucpp_debug_array_count)(void)                          = NULL;
uint16_t (*ext_strucpp_debug_elem_count) (uint8_t)                       = NULL;
uint16_t (*ext_strucpp_debug_size)       (uint8_t, uint16_t)             = NULL;
uint8_t  (*ext_strucpp_debug_set)        (uint8_t, uint16_t, bool,
                                          const uint8_t *, uint16_t)     = NULL;
uint16_t (*ext_strucpp_debug_read)       (uint8_t, uint16_t, uint8_t *)  = NULL;
uint8_t  (*ext_strucpp_debug_write)      (uint8_t, uint16_t,
                                          const uint8_t *, uint16_t)     = NULL;

/* ext_strucpp_program_md5 lives in utils.c; just bring the declaration
 * in via the public header. */
extern char *ext_strucpp_program_md5;

/* -----------------------------------------------------------------------
 * State backing the fakes. Reset between tests.
 * --------------------------------------------------------------------- */

static uint8_t            g_arr_count                                       = 0;
static uint16_t           g_elem_counts[MOCK_DEBUG_MAX_ARRAYS]              = {0};
static mock_debug_elem_t  g_elems[MOCK_DEBUG_MAX_ARRAYS][MOCK_DEBUG_MAX_ELEMS];

static mock_debug_set_capture_t g_last_set     = {0};
static uint8_t                  g_set_status   = 0x7E; /* MB_DEBUG_SUCCESS */

/* MD5 storage. We own the buffer so tests can install non-terminated
 * strings to exercise the bounded-read fix. The +2 leaves room for an
 * optional trailing null. */
static char    g_md5_buf[64 + 2];
static char   *g_md5_ptr = NULL;

/* -----------------------------------------------------------------------
 * Faked entry points.
 * --------------------------------------------------------------------- */

static uint8_t fake_array_count(void)
{
    return g_arr_count;
}

static uint16_t fake_elem_count(uint8_t arr)
{
    if (arr >= MOCK_DEBUG_MAX_ARRAYS) return 0;
    return g_elem_counts[arr];
}

static uint16_t fake_size(uint8_t arr, uint16_t elem)
{
    if (arr >= MOCK_DEBUG_MAX_ARRAYS) return 0;
    if (elem >= MOCK_DEBUG_MAX_ELEMS) return 0;
    return g_elems[arr][elem].size;
}

static uint16_t fake_read(uint8_t arr, uint16_t elem, uint8_t *dest)
{
    if (arr >= MOCK_DEBUG_MAX_ARRAYS) return 0;
    if (elem >= MOCK_DEBUG_MAX_ELEMS) return 0;
    uint16_t sz = g_elems[arr][elem].size;
    if (sz == 0 || dest == NULL) return 0;
    memcpy(dest, g_elems[arr][elem].bytes, sz);
    return sz;
}

static uint8_t fake_set(uint8_t arr, uint16_t elem, bool forcing,
                        const uint8_t *bytes, uint16_t len)
{
    g_last_set.called  = true;
    g_last_set.arr     = arr;
    g_last_set.elem    = elem;
    g_last_set.forcing = forcing;
    g_last_set.len     = len;
    if (bytes && len > 0)
    {
        uint16_t copy_len = len > MOCK_DEBUG_MAX_VARSIZE ? MOCK_DEBUG_MAX_VARSIZE : len;
        memcpy(g_last_set.bytes, bytes, copy_len);
    }
    return g_set_status;
}

static uint8_t fake_write(uint8_t arr, uint16_t elem,
                          const uint8_t *bytes, uint16_t len)
{
    g_last_set.called  = true;
    g_last_set.arr     = arr;
    g_last_set.elem    = elem;
    g_last_set.forcing = false;
    g_last_set.len     = len;
    if (bytes && len > 0)
    {
        uint16_t copy_len = len > MOCK_DEBUG_MAX_VARSIZE ? MOCK_DEBUG_MAX_VARSIZE : len;
        memcpy(g_last_set.bytes, bytes, copy_len);
    }
    return g_set_status;
}

/* -----------------------------------------------------------------------
 * Public setters / install hook.
 * --------------------------------------------------------------------- */

void mock_debug_install(void)
{
    ext_strucpp_debug_array_count = fake_array_count;
    ext_strucpp_debug_elem_count  = fake_elem_count;
    ext_strucpp_debug_size        = fake_size;
    ext_strucpp_debug_set         = fake_set;
    ext_strucpp_debug_read        = fake_read;
    ext_strucpp_debug_write       = fake_write;
}

void mock_debug_reset(void)
{
    g_arr_count = 0;
    memset(g_elem_counts, 0, sizeof(g_elem_counts));
    memset(g_elems,        0, sizeof(g_elems));
    memset(&g_last_set,    0, sizeof(g_last_set));
    g_set_status = 0x7E;
    ext_strucpp_program_md5 = NULL;
    g_md5_ptr               = NULL;
    memset(g_md5_buf, 0, sizeof(g_md5_buf));
}

void mock_debug_set_arr_count(uint8_t arr_count)
{
    if (arr_count > MOCK_DEBUG_MAX_ARRAYS) arr_count = MOCK_DEBUG_MAX_ARRAYS;
    g_arr_count = arr_count;
}

void mock_debug_set_elem_count(uint8_t arr, uint16_t elem_count)
{
    if (arr >= MOCK_DEBUG_MAX_ARRAYS) return;
    if (elem_count > MOCK_DEBUG_MAX_ELEMS) elem_count = MOCK_DEBUG_MAX_ELEMS;
    g_elem_counts[arr] = elem_count;
}

void mock_debug_set_elem(uint8_t arr, uint16_t elem,
                         uint16_t size, const uint8_t *bytes)
{
    if (arr  >= MOCK_DEBUG_MAX_ARRAYS) return;
    if (elem >= MOCK_DEBUG_MAX_ELEMS)  return;
    if (size > MOCK_DEBUG_MAX_VARSIZE) size = MOCK_DEBUG_MAX_VARSIZE;
    g_elems[arr][elem].size = size;
    if (bytes && size > 0)
    {
        memcpy(g_elems[arr][elem].bytes, bytes, size);
    }
}

const mock_debug_set_capture_t *mock_debug_last_set(void)
{
    return &g_last_set;
}

void mock_debug_program_set_status(uint8_t status)
{
    g_set_status = status;
}

void mock_debug_set_md5(const char *md5_chars, size_t len, bool terminated)
{
    if (md5_chars == NULL)
    {
        ext_strucpp_program_md5 = NULL;
        g_md5_ptr               = NULL;
        return;
    }
    if (len > sizeof(g_md5_buf) - 1) len = sizeof(g_md5_buf) - 1;
    memcpy(g_md5_buf, md5_chars, len);
    if (terminated)
    {
        g_md5_buf[len] = '\0';
    }
    else
    {
        /* Sentinel byte that should NOT be read by the runtime if the
         * MD5_HEX_LEN bound is honoured. If the runtime reads past the
         * end despite the bound, this byte ends up in the response and
         * the test catches it. */
        g_md5_buf[len] = (char)0xAA;
    }
    g_md5_ptr               = g_md5_buf;
    ext_strucpp_program_md5 = g_md5_ptr;
}
