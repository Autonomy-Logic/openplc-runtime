// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/* Fakes for the strucpp debugger ABI. install() replaces the
 * ext_strucpp_debug_* pointers, reset() clears the fake table.
 * Per-test setters program layout, bytes, writes and OOB returns. */

#ifndef TESTS_SUPPORT_DEBUG_HANDLER_MOCKS_H
#define TESTS_SUPPORT_DEBUG_HANDLER_MOCKS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Maximum mockable arrays / elems per array. Generous enough for all
 * tests; tighter than the on-wire max so accidental bugs surface fast. */
#define MOCK_DEBUG_MAX_ARRAYS  16
#define MOCK_DEBUG_MAX_ELEMS   64
#define MOCK_DEBUG_MAX_VARSIZE 32

typedef struct
{
    /* If non-zero, debug_size() returns this for the elem; otherwise 0. */
    uint16_t size;
    /* Bytes that debug_read() will copy into dest[]. */
    uint8_t  bytes[MOCK_DEBUG_MAX_VARSIZE];
} mock_debug_elem_t;

/* Last-seen arguments to debug_set / debug_write so tests can assert. */
typedef struct
{
    bool     called;
    uint8_t  arr;
    uint16_t elem;
    bool     forcing;
    uint8_t  bytes[MOCK_DEBUG_MAX_VARSIZE];
    uint16_t len;
    /* Programmed return value; 0x7E (MB_DEBUG_SUCCESS) by default. */
    uint8_t  return_status;
} mock_debug_set_capture_t;

/* Install function pointers into the runtime's externs. Idempotent. */
void mock_debug_install(void);

/* Clear all programmed state. Does NOT uninstall the function pointers. */
void mock_debug_reset(void);

/* Configure the array layout. arr_count <= MOCK_DEBUG_MAX_ARRAYS. */
void mock_debug_set_arr_count(uint8_t arr_count);
void mock_debug_set_elem_count(uint8_t arr, uint16_t elem_count);
void mock_debug_set_elem(uint8_t arr, uint16_t elem,
                         uint16_t size, const uint8_t *bytes);

/* Read out the most recent debug_set() / debug_write() invocation. */
const mock_debug_set_capture_t *mock_debug_last_set(void);

/* Override the return value of the next debug_set() / debug_write() call. */
void mock_debug_program_set_status(uint8_t status);

/* Install (NULL clears) the program MD5 string. `terminated` controls
 * whether a trailing NUL is written. */
void mock_debug_set_md5(const char *md5_chars, size_t len, bool terminated);

#ifdef __cplusplus
}
#endif

#endif /* TESTS_SUPPORT_DEBUG_HANDLER_MOCKS_H */
