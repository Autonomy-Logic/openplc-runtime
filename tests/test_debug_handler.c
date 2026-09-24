// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/*
 * test_debug_handler.c — wire-level tests for the new STruC++ debugger
 * ABI (FC 0x41-0x45) at the `process_debug_data` boundary.
 *
 * Replaces the MatIEC-era flat-index API tests (get_var_list /
 * get_var_size / get_var_count) deleted with the runtime cleanup. Goal
 * is to lock the on-wire frame format (mirrored by the editor's debug
 * client at src/frontend/utils/debug-parser.ts and the Arduino
 * StrucppBaremetal/ModbusSlave.cpp), and to pin the defensive bounds
 * checks the runtime added on top of the .so's internal validation.
 *
 * Tests use the mock debugger ABI in tests/support/debug_handler_mocks.*
 * to drive controlled `arr_count` / `elem_count` / `read` / `set`
 * behavior. process_debug_data is called directly with a constructed
 * frame; the response is unpacked and compared against expected bytes.
 */

#include "debug_handler.h"
#include "debug_handler_mocks.h"
#include "unity.h"

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

/* Declared (not defined) here so the "symbols not loaded" test can
 * NULL it out without dragging image_tables.h's full C++ surface in.
 * Storage is in tests/support/debug_handler_mocks.c. */
extern uint8_t (*ext_strucpp_debug_array_count)(void);

#define MB_FC_DEBUG_INFO     0x41
#define MB_FC_DEBUG_SET      0x42
#define MB_FC_DEBUG_GET      0x43
#define MB_FC_DEBUG_GET_LIST 0x44
#define MB_FC_DEBUG_GET_MD5  0x45

#define MB_DEBUG_SUCCESS              0x7E
#define MB_DEBUG_ERROR_OUT_OF_BOUNDS  0x81
#define MB_DEBUG_ERROR_NOT_LOADED     0x83

/* Wire frame buffer. Sized comfortably within the runtime's
 * MAX_DEBUG_FRAME = 4096 limit. */
static uint8_t frame[4096];

/* ----- Helpers ---------------------------------------------------------- */

static uint16_t read_u16_be(const uint8_t *p)
{
    return (uint16_t)((p[0] << 8) | p[1]);
}

static uint32_t read_u32_be(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
}

static void write_u16_be(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)(v & 0xFF);
}

void setUp(void)
{
    mock_debug_install();
    mock_debug_reset();
    memset(frame, 0, sizeof(frame));
}

void tearDown(void)
{
    mock_debug_reset();
}

/* =======================================================================
 *  FC 0x41 — DEBUG_INFO
 * ======================================================================= */

void test_debug_info_returns_array_layout(void)
{
    mock_debug_set_arr_count(3);
    mock_debug_set_elem_count(0,  5);
    mock_debug_set_elem_count(1, 10);
    mock_debug_set_elem_count(2,  7);

    frame[0] = MB_FC_DEBUG_INFO;
    size_t len = process_debug_data(frame, 1);

    /* Expected layout: [FC][arr_count][status][u16 BE per array] */
    TEST_ASSERT_EQUAL_UINT8(MB_FC_DEBUG_INFO,  frame[0]);
    TEST_ASSERT_EQUAL_UINT8(3,                 frame[1]);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_SUCCESS,  frame[2]);
    TEST_ASSERT_EQUAL_UINT16(5,  read_u16_be(&frame[3]));
    TEST_ASSERT_EQUAL_UINT16(10, read_u16_be(&frame[5]));
    TEST_ASSERT_EQUAL_UINT16(7,  read_u16_be(&frame[7]));
    TEST_ASSERT_EQUAL_size_t(9, len);
}

void test_debug_info_when_symbols_not_loaded_returns_not_loaded(void)
{
    /* Don't install — leave function pointers NULL */
    ext_strucpp_debug_array_count = NULL;

    frame[0] = MB_FC_DEBUG_INFO;
    size_t len = process_debug_data(frame, 1);

    TEST_ASSERT_EQUAL_UINT8(MB_FC_DEBUG_INFO,        frame[0]);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_ERROR_NOT_LOADED, frame[1]);
    TEST_ASSERT_EQUAL_size_t(2, len);

    /* Restore so subsequent tests aren't broken by tearDown ordering */
    mock_debug_install();
}

/* =======================================================================
 *  FC 0x42 — DEBUG_SET (force / unforce)
 * ======================================================================= */

void test_debug_set_force_passes_through_to_so(void)
{
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 4);

    /* Frame: [FC][arr][elem hi][elem lo][force][len hi][len lo][value...] */
    frame[0] = MB_FC_DEBUG_SET;
    frame[1] = 0;       /* arr */
    write_u16_be(&frame[2], 2);   /* elem */
    frame[4] = 1;       /* forcing = true */
    write_u16_be(&frame[5], 2);   /* val_len = 2 */
    frame[7] = 0xCA;
    frame[8] = 0xFE;

    size_t len = process_debug_data(frame, 9);

    TEST_ASSERT_EQUAL_UINT8(MB_FC_DEBUG_SET,    frame[0]);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_SUCCESS,   frame[1]);
    TEST_ASSERT_EQUAL_size_t(2, len);

    const mock_debug_set_capture_t *cap = mock_debug_last_set();
    TEST_ASSERT_TRUE(cap->called);
    TEST_ASSERT_EQUAL_UINT8(0,    cap->arr);
    TEST_ASSERT_EQUAL_UINT16(2,   cap->elem);
    TEST_ASSERT_TRUE(cap->forcing);
    TEST_ASSERT_EQUAL_UINT16(2,   cap->len);
    TEST_ASSERT_EQUAL_UINT8(0xCA, cap->bytes[0]);
    TEST_ASSERT_EQUAL_UINT8(0xFE, cap->bytes[1]);
}

void test_debug_set_unforce_clears_forcing_flag(void)
{
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 4);

    frame[0] = MB_FC_DEBUG_SET;
    frame[1] = 0;
    write_u16_be(&frame[2], 0);
    frame[4] = 0;       /* forcing = false → unforce */
    write_u16_be(&frame[5], 0);

    size_t len = process_debug_data(frame, 7);
    TEST_ASSERT_EQUAL_size_t(2, len);

    const mock_debug_set_capture_t *cap = mock_debug_last_set();
    TEST_ASSERT_TRUE(cap->called);
    TEST_ASSERT_FALSE(cap->forcing);
    TEST_ASSERT_EQUAL_UINT16(0, cap->len);
}

void test_debug_set_rejects_oob_arr_at_runtime_gate(void)
{
    /* Configures the .so as having ONE array. The wire request asks
     * to set arr=5 — way out of range. Without the runtime gate (the
     * fix for review issue #17), this would call into the .so's
     * debug_set with an OOB arr index and rely on the .so to validate.
     * With the gate, the runtime returns OUT_OF_BOUNDS without ever
     * dispatching. */
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 4);

    frame[0] = MB_FC_DEBUG_SET;
    frame[1] = 5;       /* arr — OOB */
    write_u16_be(&frame[2], 0);
    frame[4] = 1;
    write_u16_be(&frame[5], 0);

    size_t len = process_debug_data(frame, 7);

    TEST_ASSERT_EQUAL_UINT8(MB_FC_DEBUG_SET,             frame[0]);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_ERROR_OUT_OF_BOUNDS, frame[1]);
    TEST_ASSERT_EQUAL_size_t(2, len);
    /* The .so must NOT have been called. */
    TEST_ASSERT_FALSE(mock_debug_last_set()->called);
}

void test_debug_set_rejects_truncated_frame(void)
{
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 4);

    /* val_len declares 4 bytes but the frame only carries 1 (length
     * argument to process_debug_data is 8, body would need 11). */
    frame[0] = MB_FC_DEBUG_SET;
    frame[1] = 0;
    write_u16_be(&frame[2], 0);
    frame[4] = 1;
    write_u16_be(&frame[5], 4);
    frame[7] = 0xAA;

    size_t len = process_debug_data(frame, 8);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_ERROR_OUT_OF_BOUNDS, frame[1]);
    TEST_ASSERT_EQUAL_size_t(2, len);
    TEST_ASSERT_FALSE(mock_debug_last_set()->called);
}

/* =======================================================================
 *  FC 0x43 — DEBUG_GET (read a contiguous range)
 * ======================================================================= */

void test_debug_get_packs_contiguous_range(void)
{
    /* arr=0 has 4 elements, sizes 2/2/4/2 with known bytes. */
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 4);
    uint8_t e0[] = {0x01, 0x02};
    uint8_t e1[] = {0x03, 0x04};
    uint8_t e2[] = {0x05, 0x06, 0x07, 0x08};
    uint8_t e3[] = {0x09, 0x0A};
    mock_debug_set_elem(0, 0, sizeof e0, e0);
    mock_debug_set_elem(0, 1, sizeof e1, e1);
    mock_debug_set_elem(0, 2, sizeof e2, e2);
    mock_debug_set_elem(0, 3, sizeof e3, e3);

    frame[0] = MB_FC_DEBUG_GET;
    frame[1] = 0;                     /* arr */
    write_u16_be(&frame[2], 0);       /* start */
    write_u16_be(&frame[4], 3);       /* end inclusive */

    size_t len = process_debug_data(frame, 6);

    /* Header layout: [FC][STATUS][last_elem u16][tick u32][resp_size u16][data...] */
    TEST_ASSERT_EQUAL_UINT8(MB_FC_DEBUG_GET,   frame[0]);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_SUCCESS,  frame[1]);
    TEST_ASSERT_EQUAL_UINT16(3, read_u16_be(&frame[2]));
    TEST_ASSERT_EQUAL_UINT16(2 + 2 + 4 + 2, read_u16_be(&frame[8]));
    /* Body bytes back-to-back from element 0 → 3. */
    TEST_ASSERT_EQUAL_UINT8(0x01, frame[10]);
    TEST_ASSERT_EQUAL_UINT8(0x02, frame[11]);
    TEST_ASSERT_EQUAL_UINT8(0x03, frame[12]);
    TEST_ASSERT_EQUAL_UINT8(0x04, frame[13]);
    TEST_ASSERT_EQUAL_UINT8(0x05, frame[14]);
    TEST_ASSERT_EQUAL_UINT8(0x06, frame[15]);
    TEST_ASSERT_EQUAL_UINT8(0x07, frame[16]);
    TEST_ASSERT_EQUAL_UINT8(0x08, frame[17]);
    TEST_ASSERT_EQUAL_UINT8(0x09, frame[18]);
    TEST_ASSERT_EQUAL_UINT8(0x0A, frame[19]);
    TEST_ASSERT_EQUAL_size_t(20, len);
}

void test_debug_get_rejects_oob_arr_at_runtime_gate(void)
{
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 4);

    frame[0] = MB_FC_DEBUG_GET;
    frame[1] = 250;                    /* arr — OOB */
    write_u16_be(&frame[2], 0);
    write_u16_be(&frame[4], 0);

    size_t len = process_debug_data(frame, 6);
    TEST_ASSERT_EQUAL_UINT8(MB_FC_DEBUG_GET,             frame[0]);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_ERROR_OUT_OF_BOUNDS, frame[1]);
    TEST_ASSERT_EQUAL_size_t(2, len);
}

void test_debug_get_rejects_inverted_range(void)
{
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 4);

    frame[0] = MB_FC_DEBUG_GET;
    frame[1] = 0;
    write_u16_be(&frame[2], 3);   /* start > end */
    write_u16_be(&frame[4], 1);

    size_t len = process_debug_data(frame, 6);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_ERROR_OUT_OF_BOUNDS, frame[1]);
    TEST_ASSERT_EQUAL_size_t(2, len);
}

/* =======================================================================
 *  FC 0x44 — DEBUG_GET_LIST (cross-array batch read)
 * ======================================================================= */

void test_debug_get_list_packs_multiple_arrays(void)
{
    mock_debug_set_arr_count(2);
    mock_debug_set_elem_count(0, 3);
    mock_debug_set_elem_count(1, 3);
    uint8_t a[] = {0xAA, 0xBB};
    uint8_t b[] = {0xCC};
    uint8_t c[] = {0xDD, 0xEE, 0xFF};
    mock_debug_set_elem(0, 1, sizeof a, a);
    mock_debug_set_elem(1, 0, sizeof b, b);
    mock_debug_set_elem(1, 2, sizeof c, c);

    /* Request: 3 entries (arr,elem) — each 3 bytes. */
    frame[0] = MB_FC_DEBUG_GET_LIST;
    write_u16_be(&frame[1], 3);
    /* (0, 1) */
    frame[3] = 0;
    write_u16_be(&frame[4], 1);
    /* (1, 0) */
    frame[6] = 1;
    write_u16_be(&frame[7], 0);
    /* (1, 2) */
    frame[9]  = 1;
    write_u16_be(&frame[10], 2);

    size_t len = process_debug_data(frame, 12);

    TEST_ASSERT_EQUAL_UINT8(MB_FC_DEBUG_GET_LIST, frame[0]);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_SUCCESS,     frame[1]);
    TEST_ASSERT_EQUAL_UINT16(2, read_u16_be(&frame[2])); /* last_req_idx */
    TEST_ASSERT_EQUAL_UINT16(2 + 1 + 3, read_u16_be(&frame[8]));
    TEST_ASSERT_EQUAL_UINT8(0xAA, frame[10]);
    TEST_ASSERT_EQUAL_UINT8(0xBB, frame[11]);
    TEST_ASSERT_EQUAL_UINT8(0xCC, frame[12]);
    TEST_ASSERT_EQUAL_UINT8(0xDD, frame[13]);
    TEST_ASSERT_EQUAL_UINT8(0xEE, frame[14]);
    TEST_ASSERT_EQUAL_UINT8(0xFF, frame[15]);
    TEST_ASSERT_EQUAL_size_t(16, len);
}

void test_debug_get_list_skips_oob_arr_entries(void)
{
    /* The runtime should silently advance past entries with OOB arr
     * (instead of dispatching them to the .so) — defense-in-depth for
     * a malformed batch request. The frame's last_req_idx still
     * advances, so the editor can resume from i+1. */
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 2);
    uint8_t v[] = {0x42};
    mock_debug_set_elem(0, 1, sizeof v, v);

    frame[0] = MB_FC_DEBUG_GET_LIST;
    write_u16_be(&frame[1], 2);
    /* (255, 0) → OOB; must be skipped */
    frame[3] = 255;
    write_u16_be(&frame[4], 0);
    /* (0, 1) → valid */
    frame[6] = 0;
    write_u16_be(&frame[7], 1);

    size_t len = process_debug_data(frame, 9);

    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_SUCCESS, frame[1]);
    TEST_ASSERT_EQUAL_UINT16(1, read_u16_be(&frame[2])); /* last_req_idx */
    TEST_ASSERT_EQUAL_UINT16(1, read_u16_be(&frame[8]));
    TEST_ASSERT_EQUAL_UINT8(0x42, frame[10]);
    TEST_ASSERT_EQUAL_size_t(11, len);
}

/* =======================================================================
 *  FC 0x45 — DEBUG_GET_MD5 (with endianness probe echo)
 * ======================================================================= */

void test_debug_get_md5_returns_string_with_echo_bytes(void)
{
    const char md5[] = "0123456789abcdef0123456789abcdef";
    mock_debug_set_md5(md5, 32, true);

    frame[0] = MB_FC_DEBUG_GET_MD5;
    frame[1] = 0xDE;       /* echo hi */
    frame[2] = 0xAD;       /* echo lo */

    size_t len = process_debug_data(frame, 3);

    /* Expected: [FC][STATUS][md5...32][echo hi][echo lo] = 36 bytes */
    TEST_ASSERT_EQUAL_UINT8(MB_FC_DEBUG_GET_MD5, frame[0]);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_SUCCESS,    frame[1]);
    TEST_ASSERT_EQUAL_MEMORY(md5, &frame[2], 32);
    TEST_ASSERT_EQUAL_UINT8(0xDE, frame[34]);
    TEST_ASSERT_EQUAL_UINT8(0xAD, frame[35]);
    TEST_ASSERT_EQUAL_size_t(36, len);
}

void test_debug_get_md5_when_symbol_missing_returns_not_loaded(void)
{
    /* No mock_debug_set_md5() call — pointer stays NULL */

    frame[0] = MB_FC_DEBUG_GET_MD5;
    frame[1] = 0xDE;
    frame[2] = 0xAD;

    size_t len = process_debug_data(frame, 3);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_ERROR_NOT_LOADED, frame[1]);
    TEST_ASSERT_EQUAL_size_t(2, len);
}

void test_debug_get_md5_bounds_unterminated_string(void)
{
    /* Validates the fix for review issue #18: a malformed .so exporting
     * a non-null-terminated strucpp_program_md5 must not cause an
     * unbounded read off the end of the .so's .rodata. The length
     * cap is MD5_HEX_LEN (32 bytes) regardless of termination. */
    char raw[33];
    memset(raw, 'X', 32);
    raw[32] = '\0';                 /* placeholder; mock will overwrite */
    mock_debug_set_md5(raw, 32, /* terminated */ false);

    frame[0] = MB_FC_DEBUG_GET_MD5;
    frame[1] = 0x12;
    frame[2] = 0x34;

    size_t len = process_debug_data(frame, 3);

    /* Exactly 32 bytes of payload should be copied — no more, no less. */
    TEST_ASSERT_EQUAL_size_t(36, len);
    /* All 32 payload bytes are 'X'; sentinel 0xAA installed by the mock
     * past the 32-byte boundary must NOT appear in the response. */
    for (size_t i = 0; i < 32; ++i)
    {
        TEST_ASSERT_EQUAL_UINT8('X', frame[2 + i]);
    }
    TEST_ASSERT_EQUAL_UINT8(0x12, frame[34]);
    TEST_ASSERT_EQUAL_UINT8(0x34, frame[35]);
}

/* =======================================================================
 *  Frame dispatcher — unknown FC and short frames
 * ======================================================================= */

void test_unknown_function_code_returns_zero_length(void)
{
    /* Any FC outside 0x41-0x45 should be rejected by the dispatcher. */
    frame[0] = 0xFF;
    size_t len = process_debug_data(frame, 1);
    TEST_ASSERT_EQUAL_size_t(0, len);
}

void test_zero_length_frame_returns_zero(void)
{
    size_t len = process_debug_data(frame, 0);
    TEST_ASSERT_EQUAL_size_t(0, len);
}

void test_get_list_zero_entries_returns_oob(void)
{
    /* num_indexes = 0 is a malformed batch; the runtime rejects it
     * up front rather than emitting an empty body. */
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 1);

    frame[0] = MB_FC_DEBUG_GET_LIST;
    write_u16_be(&frame[1], 0);

    size_t len = process_debug_data(frame, 3);
    TEST_ASSERT_EQUAL_UINT8(MB_DEBUG_ERROR_OUT_OF_BOUNDS, frame[1]);
    TEST_ASSERT_EQUAL_size_t(2, len);
}

void test_get_unknown_status_propagated_from_so(void)
{
    /* The runtime forwards whatever status byte the .so returns from
     * debug_set, even if it's a non-canonical value — the editor
     * decides how to surface it. */
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 1);
    mock_debug_program_set_status(0xC1);  /* arbitrary non-success */

    frame[0] = MB_FC_DEBUG_SET;
    frame[1] = 0;
    write_u16_be(&frame[2], 0);
    frame[4] = 1;
    write_u16_be(&frame[5], 1);
    frame[7] = 0xAA;

    size_t len = process_debug_data(frame, 8);
    TEST_ASSERT_EQUAL_UINT8(0xC1, frame[1]);
    TEST_ASSERT_EQUAL_size_t(2, len);
}

void test_response_tick_field_is_present(void)
{
    /* The DEBUG_GET response carries the runtime's scan_counter at
     * frame[4..7] (u32 BE). Tests don't bump scan_counter, so we just
     * assert the field exists and is readable — it's a wire format
     * regression guard, not a value test. */
    mock_debug_set_arr_count(1);
    mock_debug_set_elem_count(0, 1);
    uint8_t v[] = {0x42};
    mock_debug_set_elem(0, 0, sizeof v, v);

    frame[0] = MB_FC_DEBUG_GET;
    frame[1] = 0;
    write_u16_be(&frame[2], 0);
    write_u16_be(&frame[4], 0);

    size_t len = process_debug_data(frame, 6);
    TEST_ASSERT_EQUAL_size_t(11, len);
    /* Tick field is in the response, value not asserted. */
    (void)read_u32_be(&frame[4]);
}
