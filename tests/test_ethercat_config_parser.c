/**
 * @file test_ethercat_config_parser.c
 * @brief Unit tests for ecat_config_parse_all() — JSON config ingestion.
 *
 * Builds temporary JSON files on disk and exercises the parser end-to-end
 * to cover: argument validation, malformed JSON, missing/invalid fields,
 * single-master, multi-master, mixed-protocol skip, and the bare-object
 * fall-back path.
 */

#include "ethercat_config.h"
#include "unity.h"

#include <stdio.h>
#include <string.h>

TEST_SOURCE_FILE("core/src/drivers/plugins/native/ethercat/cjson/cJSON.c")

static const char *TMPFILE = "test_ethercat_parser_tmp.json";

/* Static buffers because ecat_master_instance_t is multi-MB. */
static ecat_master_instance_t g_instances[ECAT_MAX_MASTERS];

void setUp(void)
{
    memset(g_instances, 0, sizeof(g_instances));
}

void tearDown(void)
{
    remove(TMPFILE);
}

static void write_tmpfile(const char *json)
{
    FILE *fp = fopen(TMPFILE, "w");
    TEST_ASSERT_NOT_NULL(fp);
    fputs(json, fp);
    fclose(fp);
}

/* =====================================================================
 *  Argument validation
 * ===================================================================== */

void test_parse_all_NullPath_ShouldReject(void)
{
    int count = 0;
    int rc = ecat_config_parse_all(NULL, g_instances, ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, rc);
}

void test_parse_all_NullInstances_ShouldReject(void)
{
    int count = 0;
    int rc = ecat_config_parse_all("anything.json", NULL, ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, rc);
}

void test_parse_all_NullCount_ShouldReject(void)
{
    int rc = ecat_config_parse_all("anything.json", g_instances,
                                   ECAT_MAX_MASTERS, NULL);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, rc);
}

void test_parse_all_ZeroMaxMasters_ShouldReject(void)
{
    int count = 0;
    int rc = ecat_config_parse_all("anything.json", g_instances, 0, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, rc);
}

/* =====================================================================
 *  File / parse errors
 * ===================================================================== */

void test_parse_all_NonExistentFile_ShouldReturnFileError(void)
{
    int count = 99;
    int rc = ecat_config_parse_all("/tmp/__definitely_missing_ecat_config.json",
                                   g_instances, ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_FILE, rc);
    TEST_ASSERT_EQUAL_INT(0, count);
}

void test_parse_all_MalformedJson_ShouldReturnParseError(void)
{
    write_tmpfile("{ this is not valid json ");
    int count = 0;
    int rc = ecat_config_parse_all(TMPFILE, g_instances,
                                   ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_PARSE, rc);
}

/* =====================================================================
 *  Single master, array form
 * ===================================================================== */

void test_parse_all_SingleMasterArray_ShouldParseAndValidate(void)
{
    write_tmpfile(
        "[{"
        "  \"name\": \"primary\","
        "  \"protocol\": \"ETHERCAT\","
        "  \"config\": {"
        "    \"master\": {"
        "      \"interface\": \"eth0\","
        "      \"cycle_time_us\": 500,"
        "      \"receive_timeout_us\": 1500"
        "    },"
        "    \"slaves\": [{"
        "      \"position\": 1,"
        "      \"name\": \"Coupler\","
        "      \"vendor_id\": \"0x00000002\","
        "      \"product_code\": \"0x00000001\","
        "      \"revision\": \"0x00000001\","
        "      \"channels\": [],"
        "      \"sdo_configurations\": [],"
        "      \"rx_pdos\": [], \"tx_pdos\": []"
        "    }],"
        "    \"diagnostics\": {}"
        "  }"
        "}]");

    int count = 0;
    int rc = ecat_config_parse_all(TMPFILE, g_instances,
                                   ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_EQUAL_INT(1, count);
    TEST_ASSERT_EQUAL_STRING("primary", g_instances[0].name);
    TEST_ASSERT_EQUAL_STRING("eth0", g_instances[0].config.master.interface);
    TEST_ASSERT_EQUAL_INT(500, g_instances[0].config.master.cycle_time_us);
    TEST_ASSERT_EQUAL_INT(1500, g_instances[0].config.master.receive_timeout_us);
    TEST_ASSERT_EQUAL_INT(1, g_instances[0].config.slave_count);
    TEST_ASSERT_EQUAL_INT(1, g_instances[0].config.slaves[0].position);
}

/* =====================================================================
 *  Multi-master
 * ===================================================================== */

void test_parse_all_TwoMasters_ShouldParseBoth(void)
{
    write_tmpfile(
        "["
        "  {"
        "    \"name\": \"m1\","
        "    \"protocol\": \"ETHERCAT\","
        "    \"config\": {"
        "      \"master\": {\"interface\": \"eth0\", \"cycle_time_us\": 1000, \"receive_timeout_us\": 2000},"
        "      \"slaves\": [{"
        "        \"position\": 1, \"name\": \"S1\","
        "        \"vendor_id\": \"0x00000002\", \"product_code\": \"0x00000001\","
        "        \"revision\": \"0x00000001\","
        "        \"channels\": [], \"sdo_configurations\": [],"
        "        \"rx_pdos\": [], \"tx_pdos\": []"
        "      }],"
        "      \"diagnostics\": {}"
        "    }"
        "  },"
        "  {"
        "    \"name\": \"m2\","
        "    \"protocol\": \"ETHERCAT\","
        "    \"config\": {"
        "      \"master\": {\"interface\": \"eth1\", \"cycle_time_us\": 2000, \"receive_timeout_us\": 3000},"
        "      \"slaves\": [{"
        "        \"position\": 1, \"name\": \"S2\","
        "        \"vendor_id\": \"0x00000003\", \"product_code\": \"0x00000004\","
        "        \"revision\": \"0x00000001\","
        "        \"channels\": [], \"sdo_configurations\": [],"
        "        \"rx_pdos\": [], \"tx_pdos\": []"
        "      }],"
        "      \"diagnostics\": {}"
        "    }"
        "  }"
        "]");

    int count = 0;
    int rc = ecat_config_parse_all(TMPFILE, g_instances,
                                   ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_EQUAL_INT(2, count);
    TEST_ASSERT_EQUAL_STRING("m1", g_instances[0].name);
    TEST_ASSERT_EQUAL_STRING("eth0", g_instances[0].config.master.interface);
    TEST_ASSERT_EQUAL_STRING("m2", g_instances[1].name);
    TEST_ASSERT_EQUAL_STRING("eth1", g_instances[1].config.master.interface);
    TEST_ASSERT_EQUAL_INT(2000, g_instances[1].config.master.cycle_time_us);
}

/* =====================================================================
 *  Protocol filtering
 * ===================================================================== */

void test_parse_all_NonEtherCATEntry_ShouldBeSkipped(void)
{
    write_tmpfile(
        "["
        "  {\"name\": \"modbus_skipped\", \"protocol\": \"MODBUS\","
        "   \"config\": {\"master\": {\"interface\": \"eth9\", \"cycle_time_us\": 100, \"receive_timeout_us\": 100}}},"
        "  {\"name\": \"ecat_kept\", \"protocol\": \"EtherCAT\","
        "   \"config\": {"
        "     \"master\": {\"interface\": \"eth0\", \"cycle_time_us\": 1000, \"receive_timeout_us\": 2000},"
        "     \"slaves\": [{"
        "       \"position\": 1, \"name\": \"S\","
        "       \"vendor_id\": \"0x00000002\", \"product_code\": \"0x00000001\","
        "       \"revision\": \"0x00000001\","
        "       \"channels\": [], \"sdo_configurations\": [], \"rx_pdos\": [], \"tx_pdos\": []"
        "     }],"
        "     \"diagnostics\": {}"
        "   }}"
        "]");

    int count = 0;
    int rc = ecat_config_parse_all(TMPFILE, g_instances,
                                   ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_EQUAL_INT(1, count);
    TEST_ASSERT_EQUAL_STRING("ecat_kept", g_instances[0].name);
}

/* =====================================================================
 *  Validation rejection inside the loop
 * ===================================================================== */

void test_parse_all_InvalidMasterEntry_ShouldBeSkipped(void)
{
    /* First entry has slave with vendor_id=0 -> validate fails -> skipped.
     * Second entry is valid -> kept. Overall result: OK with count=1. */
    write_tmpfile(
        "["
        "  {\"name\": \"bad\", \"protocol\": \"ETHERCAT\","
        "   \"config\": {"
        "     \"master\": {\"interface\": \"eth0\", \"cycle_time_us\": 1000, \"receive_timeout_us\": 2000},"
        "     \"slaves\": [{"
        "       \"position\": 1, \"name\": \"S\","
        "       \"vendor_id\": \"0x00000000\", \"product_code\": \"0x00000001\","
        "       \"revision\": \"0x00000001\","
        "       \"channels\": [], \"sdo_configurations\": [], \"rx_pdos\": [], \"tx_pdos\": []"
        "     }],"
        "     \"diagnostics\": {}"
        "   }},"
        "  {\"name\": \"good\", \"protocol\": \"ETHERCAT\","
        "   \"config\": {"
        "     \"master\": {\"interface\": \"eth1\", \"cycle_time_us\": 1000, \"receive_timeout_us\": 2000},"
        "     \"slaves\": [{"
        "       \"position\": 1, \"name\": \"S\","
        "       \"vendor_id\": \"0x00000002\", \"product_code\": \"0x00000001\","
        "       \"revision\": \"0x00000001\","
        "       \"channels\": [], \"sdo_configurations\": [], \"rx_pdos\": [], \"tx_pdos\": []"
        "     }],"
        "     \"diagnostics\": {}"
        "   }}"
        "]");

    int count = 0;
    int rc = ecat_config_parse_all(TMPFILE, g_instances,
                                   ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_EQUAL_INT(1, count);
    TEST_ASSERT_EQUAL_STRING("good", g_instances[0].name);
}

void test_parse_all_AllInvalid_ShouldReturnMissing(void)
{
    write_tmpfile(
        "[{"
        "  \"name\": \"bad\", \"protocol\": \"ETHERCAT\","
        "  \"config\": {"
        "    \"master\": {\"interface\": \"\", \"cycle_time_us\": 1000, \"receive_timeout_us\": 2000},"
        "    \"slaves\": [], \"diagnostics\": {}"
        "  }"
        "}]");

    int count = 99;
    int rc = ecat_config_parse_all(TMPFILE, g_instances,
                                   ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_MISSING, rc);
    TEST_ASSERT_EQUAL_INT(0, count);
}

/* =====================================================================
 *  Defaults applied for missing fields
 * ===================================================================== */

void test_parse_all_MissingMasterFields_ShouldUseDefaults(void)
{
    /* Master section omits cycle_time_us, receive_timeout_us, watchdog -- the
     * parser must fill in defaults from ecat_config_init_defaults(). */
    write_tmpfile(
        "[{"
        "  \"name\": \"m\", \"protocol\": \"ETHERCAT\","
        "  \"config\": {"
        "    \"master\": {\"interface\": \"eth0\"},"
        "    \"slaves\": [{"
        "      \"position\": 1, \"name\": \"S\","
        "      \"vendor_id\": \"0x00000002\", \"product_code\": \"0x00000001\","
        "      \"revision\": \"0x00000001\","
        "      \"channels\": [], \"sdo_configurations\": [], \"rx_pdos\": [], \"tx_pdos\": []"
        "    }],"
        "    \"diagnostics\": {}"
        "  }"
        "}]");

    int count = 0;
    int rc = ecat_config_parse_all(TMPFILE, g_instances,
                                   ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_EQUAL_INT(1, count);
    TEST_ASSERT_EQUAL_INT(1000, g_instances[0].config.master.cycle_time_us);
    TEST_ASSERT_EQUAL_INT(2000, g_instances[0].config.master.receive_timeout_us);
    TEST_ASSERT_EQUAL_INT(3, g_instances[0].config.master.watchdog_timeout_cycles);
}

/* =====================================================================
 *  Bare-object fall-back path
 * ===================================================================== */

void test_parse_all_BareObjectRoot_ShouldFallBackToSingleEntry(void)
{
    /* Root is an object, not an array -- the bare-object fall-back should
     * parse it as one master. */
    write_tmpfile(
        "{"
        "  \"name\": \"bare\","
        "  \"config\": {"
        "    \"master\": {\"interface\": \"eth0\", \"cycle_time_us\": 1000, \"receive_timeout_us\": 2000},"
        "    \"slaves\": [{"
        "      \"position\": 1, \"name\": \"S\","
        "      \"vendor_id\": \"0x00000002\", \"product_code\": \"0x00000001\","
        "      \"revision\": \"0x00000001\","
        "      \"channels\": [], \"sdo_configurations\": [], \"rx_pdos\": [], \"tx_pdos\": []"
        "    }],"
        "    \"diagnostics\": {}"
        "  }"
        "}");

    int count = 0;
    int rc = ecat_config_parse_all(TMPFILE, g_instances,
                                   ECAT_MAX_MASTERS, &count);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, rc);
    TEST_ASSERT_EQUAL_INT(1, count);
    TEST_ASSERT_EQUAL_STRING("bare", g_instances[0].name);
}
