// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file test_ethercat_iomap.c
 * @brief Unit tests for the EtherCAT client's mapping file, layout join and per-cycle copies.
 */

#include "ethercat_iomap.h"
#include "unity.h"

#include <stdio.h>
#include <string.h>

TEST_SOURCE_FILE("core/src/drivers/plugins/native/cjson/cJSON.c")

static const char *TMPFILE = "test_ethercat_iomap_tmp.json";

/* EK1818-shaped layout: 4 output bits, 8 input bits, one byte each way. */
static const char *LAYOUT =
    "{\"status\":\"success\",\"masters\":[{\"index\":0,\"name\":\"m0\",\"state\":\"OPERATIONAL\","
    "\"ready\":true,\"output_bytes\":3,\"input_bytes\":3,\"entries\":["
    "{\"slave\":1,\"pdo\":\"0x1600\",\"index\":\"0x7000\",\"subindex\":1,\"direction\":\"output\","
    "\"bit_offset\":0,\"bit_length\":1,\"data_type\":\"BOOL\",\"name\":\"Out1\"},"
    "{\"slave\":1,\"pdo\":\"0x1601\",\"index\":\"0x7010\",\"subindex\":1,\"direction\":\"output\","
    "\"bit_offset\":8,\"bit_length\":16,\"data_type\":\"UINT16\",\"name\":\"Word\"},"
    "{\"slave\":1,\"pdo\":\"0x1a00\",\"index\":\"0x6000\",\"subindex\":1,\"direction\":\"input\","
    "\"bit_offset\":3,\"bit_length\":1,\"data_type\":\"BOOL\",\"name\":\"In1\"},"
    "{\"slave\":1,\"pdo\":\"0x1a01\",\"index\":\"0x6010\",\"subindex\":1,\"direction\":\"input\","
    "\"bit_offset\":8,\"bit_length\":16,\"data_type\":\"UINT16\",\"name\":\"InWord\"}]}]}";

/* --- fake runtime image and journal ---------------------------------------------------- */

#define BUF 16
static IEC_BOOL bool_out_vals[BUF][8];
static IEC_BOOL *bool_out[BUF][8];
static IEC_BOOL *bool_in[BUF][8];
static IEC_UINT int_out_vals[BUF];
static IEC_UINT *int_out[BUF];
static IEC_UINT *int_in[BUF];
static plugin_runtime_args_t args;

static int last_bool_index, last_bool_bit, last_bool_value, bool_writes;
static int last_int_index;
static unsigned last_int_value;

static int fake_bool(int type, int index, int bit, int value)
{
    (void)type;
    last_bool_index = index;
    last_bool_bit = bit;
    last_bool_value = value;
    bool_writes++;
    return 0;
}
static int fake_byte(int type, int index, int v) { (void)type; (void)index; (void)v; return 0; }
static int fake_int(int type, int index, int v)
{
    (void)type;
    last_int_index = index;
    last_int_value = (unsigned)v;
    return 0;
}
static int fake_dint(int type, int index, unsigned int v) { (void)type; (void)index; (void)v; return 0; }
static int fake_lint(int type, int index, unsigned long long v)
{
    (void)type;
    (void)index;
    (void)v;
    return 0;
}

static ecat_iomap_t map;
static ecat_bound_map_t bound;

void setUp(void)
{
    memset(&args, 0, sizeof(args));
    memset(bool_out_vals, 0, sizeof(bool_out_vals));
    memset(int_out_vals, 0, sizeof(int_out_vals));
    for (int i = 0; i < BUF; i++) {
        for (int b = 0; b < 8; b++) {
            bool_out[i][b] = &bool_out_vals[i][b];
            bool_in[i][b] = &bool_out_vals[i][b];
        }
        int_out[i] = &int_out_vals[i];
        int_in[i] = &int_out_vals[i];
    }
    args.bool_output = bool_out;
    args.bool_input = bool_in;
    args.int_output = int_out;
    args.int_input = int_in;
    args.buffer_size = BUF;
    args.journal_write_bool = fake_bool;
    args.journal_write_byte = fake_byte;
    args.journal_write_int = fake_int;
    args.journal_write_dint = fake_dint;
    args.journal_write_lint = fake_lint;
    bool_writes = 0;
}

void tearDown(void)
{
    remove(TMPFILE);
}

static void write_mapping(const char *entries)
{
    FILE *fp = fopen(TMPFILE, "w");
    TEST_ASSERT_NOT_NULL(fp);
    fprintf(fp, "{\"version\":1,\"masters\":[{\"name\":\"m0\",\"entries\":[%s]}]}", entries);
    fclose(fp);
}

static int bind_mapping(const char *entries, char *err, size_t err_size)
{
    write_mapping(entries);
    TEST_ASSERT_EQUAL_INT(0, ecat_iomap_load(TMPFILE, &map, err, err_size));
    cJSON *layout = cJSON_Parse(LAYOUT);
    TEST_ASSERT_NOT_NULL(layout);
    int rc = ecat_iomap_bind(&map, layout, &args, &bound, err, err_size);
    cJSON_Delete(layout);
    return rc;
}

/* --- loading ------------------------------------------------------------------------------ */

void test_load_rejects_wrong_version(void)
{
    char err[256];
    FILE *fp = fopen(TMPFILE, "w");
    fputs("{\"version\":2,\"masters\":[]}", fp);
    fclose(fp);
    TEST_ASSERT_EQUAL_INT(-1, ecat_iomap_load(TMPFILE, &map, err, sizeof(err)));
}

void test_load_rejects_bad_iec_location(void)
{
    char err[256];
    write_mapping("{\"slave\":1,\"index\":\"0x6000\",\"subindex\":1,\"iec_location\":\"%IZ0\"}");
    TEST_ASSERT_EQUAL_INT(-1, ecat_iomap_load(TMPFILE, &map, err, sizeof(err)));
    TEST_ASSERT_NOT_NULL(strstr(err, "%IZ0"));
}

/* --- binding ----------------------------------------------------------------------------- */

void test_bind_resolves_inputs_and_outputs(void)
{
    char err[256] = "";
    int rc = bind_mapping(
        "{\"slave\":1,\"index\":\"0x7000\",\"subindex\":1,\"iec_location\":\"%QX0.1\"},"
        "{\"slave\":1,\"index\":\"0x7010\",\"subindex\":1,\"iec_location\":\"%QW2\"},"
        "{\"slave\":1,\"index\":\"0x6000\",\"subindex\":1,\"iec_location\":\"%IX1.4\"},"
        "{\"slave\":1,\"index\":\"0x6010\",\"subindex\":1,\"iec_location\":\"%IW3\"}",
        err, sizeof(err));
    TEST_ASSERT_EQUAL_INT_MESSAGE(0, rc, err);
    TEST_ASSERT_TRUE(bound.masters[0].active);
    TEST_ASSERT_EQUAL_INT(2, bound.masters[0].output_count);
    TEST_ASSERT_EQUAL_INT(2, bound.masters[0].input_count);
}

void test_bind_fails_on_missing_entry(void)
{
    char err[256] = "";
    int rc = bind_mapping("{\"slave\":2,\"index\":\"0x6000\",\"subindex\":1,\"iec_location\":\"%IX0.0\"}",
                          err, sizeof(err));
    TEST_ASSERT_EQUAL_INT(-1, rc);
    TEST_ASSERT_NOT_NULL(strstr(err, "no matching process data entry"));
}

void test_bind_fails_on_direction_mismatch(void)
{
    char err[256] = "";
    int rc = bind_mapping("{\"slave\":1,\"index\":\"0x6000\",\"subindex\":1,\"iec_location\":\"%QX0.0\"}",
                          err, sizeof(err));
    TEST_ASSERT_EQUAL_INT(-1, rc);
    TEST_ASSERT_NOT_NULL(strstr(err, "input data"));
}

void test_bind_fails_on_width_mismatch(void)
{
    char err[256] = "";
    int rc = bind_mapping("{\"slave\":1,\"index\":\"0x6010\",\"subindex\":1,\"iec_location\":\"%IB0\"}",
                          err, sizeof(err));
    TEST_ASSERT_EQUAL_INT(-1, rc);
    TEST_ASSERT_NOT_NULL(strstr(err, "16 bits"));
}

void test_bind_fails_when_master_not_running(void)
{
    char err[256] = "";
    FILE *fp = fopen(TMPFILE, "w");
    fputs("{\"version\":1,\"masters\":[{\"name\":\"other\",\"entries\":[]}]}", fp);
    fclose(fp);
    TEST_ASSERT_EQUAL_INT(0, ecat_iomap_load(TMPFILE, &map, err, sizeof(err)));
    cJSON *layout = cJSON_Parse(LAYOUT);
    TEST_ASSERT_EQUAL_INT(-1, ecat_iomap_bind(&map, layout, &args, &bound, err, sizeof(err)));
    cJSON_Delete(layout);
    TEST_ASSERT_NOT_NULL(strstr(err, "other"));
}

/* --- per-cycle copies --------------------------------------------------------------------- */

void test_collect_outputs_packs_bits_and_words(void)
{
    char err[256] = "";
    TEST_ASSERT_EQUAL_INT(0, bind_mapping(
        "{\"slave\":1,\"index\":\"0x7000\",\"subindex\":1,\"iec_location\":\"%QX0.1\"},"
        "{\"slave\":1,\"index\":\"0x7010\",\"subindex\":1,\"iec_location\":\"%QW2\"}",
        err, sizeof(err)));
    bool_out_vals[0][1] = 1;
    int_out_vals[2] = 0xBEEF;

    uint8_t payload[3];
    memset(payload, 0xFF, sizeof(payload));
    ecat_iomap_collect_outputs(&bound.masters[0], payload, sizeof(payload));
    TEST_ASSERT_EQUAL_HEX8(0x01, payload[0]); /* bit 0 set, the rest cleared */
    TEST_ASSERT_EQUAL_HEX8(0xEF, payload[1]);
    TEST_ASSERT_EQUAL_HEX8(0xBE, payload[2]);
}

void test_publish_inputs_writes_journal(void)
{
    char err[256] = "";
    TEST_ASSERT_EQUAL_INT(0, bind_mapping(
        "{\"slave\":1,\"index\":\"0x6000\",\"subindex\":1,\"iec_location\":\"%IX1.4\"},"
        "{\"slave\":1,\"index\":\"0x6010\",\"subindex\":1,\"iec_location\":\"%IW3\"}",
        err, sizeof(err)));
    uint8_t payload[3] = { 0x08, 0x34, 0x12 }; /* bit 3 set; word 0x1234 at byte 1 */
    ecat_iomap_publish_inputs(&bound.masters[0], payload, sizeof(payload), &args);
    TEST_ASSERT_EQUAL_INT(1, bool_writes);
    TEST_ASSERT_EQUAL_INT(1, last_bool_index);
    TEST_ASSERT_EQUAL_INT(4, last_bool_bit);
    TEST_ASSERT_EQUAL_INT(1, last_bool_value);
    TEST_ASSERT_EQUAL_INT(3, last_int_index);
    TEST_ASSERT_EQUAL_HEX16(0x1234, last_int_value);
}

void test_load_rejects_duplicate_master_names(void)
{
    FILE *fp = fopen(TMPFILE, "w");
    TEST_ASSERT_NOT_NULL(fp);
    fprintf(fp, "{\"version\":1,\"masters\":[{\"name\":\"m0\",\"entries\":[]},"
                "{\"name\":\"m0\",\"entries\":[]}]}");
    fclose(fp);
    char err[256];
    TEST_ASSERT_EQUAL_INT(-1, ecat_iomap_load(TMPFILE, &map, err, sizeof(err)));
    TEST_ASSERT_NOT_NULL(strstr(err, "used twice"));
}
