/**
 * @file test_ethercat_config_helpers.c
 * @brief Unit tests for the small public helpers in ethercat_config.c:
 *        ecat_config_init_defaults, ecat_data_type_size, ecat_state_to_string.
 */

#include "ethercat_config.h"
#include "unity.h"

#include <string.h>

TEST_SOURCE_FILE("core/src/drivers/plugins/native/ethercat/cjson/cJSON.c")

void setUp(void) {}
void tearDown(void) {}

/* =====================================================================
 *  ecat_config_init_defaults
 * ===================================================================== */

void test_init_defaults_NullConfig_ShouldNotCrash(void)
{
    ecat_config_init_defaults(NULL); /* contract: silent no-op */
}

void test_init_defaults_MasterFields_ShouldHaveExpectedDefaults(void)
{
    static ecat_config_t config;
    /* Pre-fill with non-zero garbage to confirm the init clears the struct. */
    memset(&config, 0xAA, sizeof(config));

    ecat_config_init_defaults(&config);

    TEST_ASSERT_EQUAL_STRING("eth0", config.master.interface);
    TEST_ASSERT_EQUAL_INT(1000, config.master.cycle_time_us);
    TEST_ASSERT_EQUAL_INT(2000, config.master.receive_timeout_us);
    TEST_ASSERT_EQUAL_INT(3, config.master.watchdog_timeout_cycles);
    TEST_ASSERT_EQUAL_STRING("info", config.master.log_level);
    TEST_ASSERT_EQUAL_INT(0, config.master.task_name[0]);
    TEST_ASSERT_EQUAL_INT(0, config.master.task_cycle_time_us);
}

void test_init_defaults_DiagnosticsFields_ShouldHaveExpectedDefaults(void)
{
    static ecat_config_t config;
    ecat_config_init_defaults(&config);

    TEST_ASSERT_TRUE(config.diagnostics.log_connections);
    TEST_ASSERT_FALSE(config.diagnostics.log_data_access);
    TEST_ASSERT_TRUE(config.diagnostics.log_errors);
    TEST_ASSERT_EQUAL_INT(10000, config.diagnostics.max_log_entries);
    TEST_ASSERT_EQUAL_INT(500, config.diagnostics.status_update_interval_ms);
}

void test_init_defaults_SlaveCount_ShouldBeZero(void)
{
    static ecat_config_t config;
    ecat_config_init_defaults(&config);
    TEST_ASSERT_EQUAL_INT(0, config.slave_count);
}

/* =====================================================================
 *  ecat_data_type_size
 * ===================================================================== */

void test_data_type_size_Bool_ShouldBeOne(void)
{
    TEST_ASSERT_EQUAL_INT(1, ecat_data_type_size(ECAT_DTYPE_BOOL));
}

void test_data_type_size_8bit_ShouldBeOne(void)
{
    TEST_ASSERT_EQUAL_INT(1, ecat_data_type_size(ECAT_DTYPE_INT8));
    TEST_ASSERT_EQUAL_INT(1, ecat_data_type_size(ECAT_DTYPE_UINT8));
}

void test_data_type_size_16bit_ShouldBeTwo(void)
{
    TEST_ASSERT_EQUAL_INT(2, ecat_data_type_size(ECAT_DTYPE_INT16));
    TEST_ASSERT_EQUAL_INT(2, ecat_data_type_size(ECAT_DTYPE_UINT16));
}

void test_data_type_size_32bit_ShouldBeFour(void)
{
    TEST_ASSERT_EQUAL_INT(4, ecat_data_type_size(ECAT_DTYPE_INT32));
    TEST_ASSERT_EQUAL_INT(4, ecat_data_type_size(ECAT_DTYPE_UINT32));
    TEST_ASSERT_EQUAL_INT(4, ecat_data_type_size(ECAT_DTYPE_REAL32));
}

void test_data_type_size_64bit_ShouldBeEight(void)
{
    TEST_ASSERT_EQUAL_INT(8, ecat_data_type_size(ECAT_DTYPE_INT64));
    TEST_ASSERT_EQUAL_INT(8, ecat_data_type_size(ECAT_DTYPE_UINT64));
    TEST_ASSERT_EQUAL_INT(8, ecat_data_type_size(ECAT_DTYPE_REAL64));
}

void test_data_type_size_UnknownAndPad_ShouldBeZero(void)
{
    TEST_ASSERT_EQUAL_INT(0, ecat_data_type_size(ECAT_DTYPE_UNKNOWN));
    TEST_ASSERT_EQUAL_INT(0, ecat_data_type_size(ECAT_DTYPE_PAD));
}

/* =====================================================================
 *  ecat_state_to_string
 * ===================================================================== */

void test_state_to_string_Idle_ShouldReturnIdle(void)
{
    TEST_ASSERT_EQUAL_STRING("IDLE", ecat_state_to_string(ECAT_STATE_IDLE));
}

void test_state_to_string_Scanning_ShouldReturnScanning(void)
{
    TEST_ASSERT_EQUAL_STRING("SCANNING", ecat_state_to_string(ECAT_STATE_SCANNING));
}

void test_state_to_string_Configuring_ShouldReturnConfiguring(void)
{
    TEST_ASSERT_EQUAL_STRING("CONFIGURING", ecat_state_to_string(ECAT_STATE_CONFIGURING));
}

void test_state_to_string_Transitioning_ShouldReturnTransitioning(void)
{
    TEST_ASSERT_EQUAL_STRING("TRANSITIONING", ecat_state_to_string(ECAT_STATE_TRANSITIONING));
}

void test_state_to_string_Operational_ShouldReturnOperational(void)
{
    TEST_ASSERT_EQUAL_STRING("OPERATIONAL", ecat_state_to_string(ECAT_STATE_OPERATIONAL));
}

void test_state_to_string_Recovering_ShouldReturnRecovering(void)
{
    TEST_ASSERT_EQUAL_STRING("RECOVERING", ecat_state_to_string(ECAT_STATE_RECOVERING));
}

void test_state_to_string_Error_ShouldReturnError(void)
{
    TEST_ASSERT_EQUAL_STRING("ERROR", ecat_state_to_string(ECAT_STATE_ERROR));
}

void test_state_to_string_Stopped_ShouldReturnStopped(void)
{
    TEST_ASSERT_EQUAL_STRING("STOPPED", ecat_state_to_string(ECAT_STATE_STOPPED));
}
