// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file test_ethercat_sdo_config.c
 * @brief Unit tests for SDO value parsing in ecat_config_parse()
 *
 * Verifies that the parser correctly handles SDO values as both
 * JSON numbers and JSON strings (decimal, hex, float, negative).
 * Exercises the get_numeric_value() helper via ecat_config_parse().
 */

#include "ethercat_config.h"
#include "unity.h"

#include <stdio.h>
#include <string.h>

TEST_SOURCE_FILE("core/src/drivers/plugins/native/ethercat/cjson/cJSON.c")

void setUp(void) {}
void tearDown(void) {}

/**
 * Helper: write a minimal JSON config with a single slave that has one SDO
 * entry whose "value" field is set to the provided raw JSON token.
 * Returns the path to the temp file.
 */
static const char *TEMP_FILE = "test_sdo_config_tmp.json";

static int write_sdo_json(const char *value_token, const char *data_type)
{
    FILE *fp = fopen(TEMP_FILE, "w");
    if (!fp)
        return -1;

    fprintf(fp,
        "[{\n"
        "  \"name\": \"test\",\n"
        "  \"protocol\": \"ETHERCAT\",\n"
        "  \"config\": {\n"
        "    \"master\": { \"interface\": \"eth0\", \"cycle_time_us\": 1000, "
        "\"receive_timeout_us\": 2000 },\n"
        "    \"slaves\": [{\n"
        "      \"position\": 1,\n"
        "      \"name\": \"TestSlave\",\n"
        "      \"type\": \"coupler\",\n"
        "      \"vendor_id\": \"0x00000002\",\n"
        "      \"product_code\": \"0x00000001\",\n"
        "      \"revision\": \"0x00000001\",\n"
        "      \"channels\": [],\n"
        "      \"sdo_configurations\": [{\n"
        "        \"index\": \"0x8000\",\n"
        "        \"subindex\": 1,\n"
        "        \"value\": %s,\n"
        "        \"data_type\": \"%s\",\n"
        "        \"name\": \"TestSDO\"\n"
        "      }],\n"
        "      \"rx_pdos\": [],\n"
        "      \"tx_pdos\": []\n"
        "    }],\n"
        "    \"diagnostics\": {}\n"
        "  }\n"
        "}]\n",
        value_token, data_type);

    fclose(fp);
    return 0;
}

static void cleanup_temp(void)
{
    remove(TEMP_FILE);
}

/* ---- JSON number values ---- */

void test_sdo_parse_NumberValue_ShouldStoreCorrectly(void)
{
    write_sdo_json("100", "UINT16");

    /* ecat_config_t is too large for the stack; use static */
    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_EQUAL_INT(1, config.slave_count);
    TEST_ASSERT_EQUAL_INT(1, config.slaves[0].sdo_count);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, 100.0, config.slaves[0].sdo_configs[0].value);
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT16, config.slaves[0].sdo_configs[0].parsed_type);

    cleanup_temp();
}

void test_sdo_parse_NegativeNumber_ShouldStoreCorrectly(void)
{
    write_sdo_json("-50", "INT16");

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, -50.0, config.slaves[0].sdo_configs[0].value);
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_INT16, config.slaves[0].sdo_configs[0].parsed_type);

    cleanup_temp();
}

void test_sdo_parse_FloatNumber_ShouldStoreCorrectly(void)
{
    write_sdo_json("3.14", "REAL");

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, 3.14, config.slaves[0].sdo_configs[0].value);
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL32, config.slaves[0].sdo_configs[0].parsed_type);

    cleanup_temp();
}

/* ---- JSON string values (backward compatibility with old editor) ---- */

void test_sdo_parse_StringDecimal_ShouldParseAsNumber(void)
{
    write_sdo_json("\"100\"", "UINT16");

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, 100.0, config.slaves[0].sdo_configs[0].value);

    cleanup_temp();
}

void test_sdo_parse_StringHex_ShouldParseAsNumber(void)
{
    write_sdo_json("\"0xFF\"", "UINT8");

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, 255.0, config.slaves[0].sdo_configs[0].value);
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT8, config.slaves[0].sdo_configs[0].parsed_type);

    cleanup_temp();
}

void test_sdo_parse_StringFloat_ShouldParseAsNumber(void)
{
    write_sdo_json("\"3.14\"", "REAL32");

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, 3.14, config.slaves[0].sdo_configs[0].value);
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL32, config.slaves[0].sdo_configs[0].parsed_type);

    cleanup_temp();
}

void test_sdo_parse_StringNegative_ShouldParseAsNumber(void)
{
    write_sdo_json("\"-50\"", "INT16");

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, -50.0, config.slaves[0].sdo_configs[0].value);

    cleanup_temp();
}

void test_sdo_parse_StringEmpty_ShouldDefaultToZero(void)
{
    write_sdo_json("\"\"", "UINT16");

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, 0.0, config.slaves[0].sdo_configs[0].value);

    cleanup_temp();
}

/* ---- Missing value field ---- */

void test_sdo_parse_MissingValue_ShouldDefaultToZero(void)
{
    /* Write JSON without a "value" field */
    FILE *fp = fopen(TEMP_FILE, "w");
    TEST_ASSERT_NOT_NULL(fp);

    fprintf(fp,
        "[{\n"
        "  \"name\": \"test\",\n"
        "  \"protocol\": \"ETHERCAT\",\n"
        "  \"config\": {\n"
        "    \"master\": { \"interface\": \"eth0\", \"cycle_time_us\": 1000, "
        "\"receive_timeout_us\": 2000 },\n"
        "    \"slaves\": [{\n"
        "      \"position\": 1,\n"
        "      \"name\": \"TestSlave\",\n"
        "      \"type\": \"coupler\",\n"
        "      \"vendor_id\": \"0x00000002\",\n"
        "      \"product_code\": \"0x00000001\",\n"
        "      \"revision\": \"0x00000001\",\n"
        "      \"channels\": [],\n"
        "      \"sdo_configurations\": [{\n"
        "        \"index\": \"0x8000\",\n"
        "        \"subindex\": 1,\n"
        "        \"data_type\": \"UINT16\",\n"
        "        \"name\": \"TestSDO\"\n"
        "      }],\n"
        "      \"rx_pdos\": [],\n"
        "      \"tx_pdos\": []\n"
        "    }],\n"
        "    \"diagnostics\": {}\n"
        "  }\n"
        "}]\n");
    fclose(fp);

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, 0.0, config.slaves[0].sdo_configs[0].value);

    cleanup_temp();
}

/* ---- Data type resolution in SDO ---- */

void test_sdo_parse_DataType_ShouldResolveParsedType(void)
{
    write_sdo_json("42", "DINT");

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_INT32, config.slaves[0].sdo_configs[0].parsed_type);

    cleanup_temp();
}

void test_sdo_parse_LrealDataType_ShouldResolveReal64(void)
{
    write_sdo_json("1.5", "LREAL");

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL64, config.slaves[0].sdo_configs[0].parsed_type);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, 1.5, config.slaves[0].sdo_configs[0].value);

    cleanup_temp();
}

/* ---- Large hex value ---- */

void test_sdo_parse_StringLargeHex_ShouldParseCorrectly(void)
{
    write_sdo_json("\"0x1A2B\"", "UINT16");

    static ecat_config_t config;
    int rc = ecat_config_parse(TEMP_FILE, &config);

    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_DOUBLE_WITHIN(0.001, 6699.0, config.slaves[0].sdo_configs[0].value);

    cleanup_temp();
}
