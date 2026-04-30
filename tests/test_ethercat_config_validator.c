/**
 * @file test_ethercat_config_validator.c
 * @brief Unit tests for ecat_config_validate() — boundary and shape checks
 *        on a parsed ecat_config_t before the master starts.
 */

#include "ethercat_config.h"
#include "unity.h"

#include <string.h>

TEST_SOURCE_FILE("core/src/drivers/plugins/native/ethercat/cjson/cJSON.c")

void setUp(void) {}
void tearDown(void) {}

/* Helper: a baseline configuration that should always validate as OK. */
static void baseline_config(ecat_config_t *config)
{
    ecat_config_init_defaults(config);

    config->slave_count = 1;
    ecat_slave_t *s = &config->slaves[0];
    memset(s, 0, sizeof(*s));
    s->position = 1;
    s->vendor_id = 0x00000002;
    s->product_code = 0x00000001;
    s->revision = 0x00000001;
    snprintf(s->name, sizeof(s->name), "TestSlave");
    s->channel_count = 0;
}

/* ---- NULL ---- */

void test_validate_NullConfig_ShouldReject(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(NULL));
}

/* ---- Master section ---- */

void test_validate_BaselineConfig_ShouldAccept(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, ecat_config_validate(&config));
}

void test_validate_EmptyInterface_ShouldReject(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.master.interface[0] = '\0';
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(&config));
}

void test_validate_ZeroCycleTime_ShouldReject(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.master.cycle_time_us = 0;
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(&config));
}

void test_validate_NegativeCycleTime_ShouldReject(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.master.cycle_time_us = -1;
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(&config));
}

void test_validate_ZeroReceiveTimeout_ShouldReject(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.master.receive_timeout_us = 0;
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(&config));
}

/* ---- Slave section ---- */

void test_validate_NoSlaves_ShouldAccept(void)
{
    /* Master with no slaves is a valid (if uncommon) config. */
    static ecat_config_t config;
    baseline_config(&config);
    config.slave_count = 0;
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, ecat_config_validate(&config));
}

void test_validate_SlavePositionZero_ShouldReject(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.slaves[0].position = 0;
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(&config));
}

void test_validate_SlavePositionNegative_ShouldReject(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.slaves[0].position = -1;
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(&config));
}

void test_validate_SlaveZeroVendorId_ShouldReject(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.slaves[0].vendor_id = 0;
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(&config));
}

void test_validate_SlaveZeroProductCode_ShouldReject(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.slaves[0].product_code = 0;
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(&config));
}

void test_validate_DuplicateSlavePositions_ShouldReject(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.slave_count = 2;
    /* slot 0 was set up by baseline_config */
    ecat_slave_t *s2 = &config.slaves[1];
    memset(s2, 0, sizeof(*s2));
    s2->position = config.slaves[0].position; /* same position - duplicate */
    s2->vendor_id = 0x00000003;
    s2->product_code = 0x00000004;
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(&config));
}

void test_validate_DistinctSlavePositions_ShouldAccept(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.slave_count = 2;
    ecat_slave_t *s2 = &config.slaves[1];
    memset(s2, 0, sizeof(*s2));
    s2->position = 2;
    s2->vendor_id = 0x00000003;
    s2->product_code = 0x00000004;
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, ecat_config_validate(&config));
}

/* ---- Channel section ---- */

void test_validate_ChannelWithoutPercentPrefix_ShouldReject(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.slaves[0].channel_count = 1;
    ecat_channel_t *ch = &config.slaves[0].channels[0];
    memset(ch, 0, sizeof(*ch));
    /* Non-empty location that is not a valid IEC string -- must start with '%'. */
    snprintf(ch->iec_location, sizeof(ch->iec_location), "IX0.0");
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_ERR_INVALID, ecat_config_validate(&config));
}

void test_validate_ChannelWithPercentPrefix_ShouldAccept(void)
{
    static ecat_config_t config;
    baseline_config(&config);
    config.slaves[0].channel_count = 1;
    ecat_channel_t *ch = &config.slaves[0].channels[0];
    memset(ch, 0, sizeof(*ch));
    snprintf(ch->iec_location, sizeof(ch->iec_location), "%%IX0.0");
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, ecat_config_validate(&config));
}

void test_validate_ChannelWithEmptyLocation_ShouldAccept(void)
{
    /* Empty iec_location is allowed (validator only flags non-empty without '%'). */
    static ecat_config_t config;
    baseline_config(&config);
    config.slaves[0].channel_count = 1;
    ecat_channel_t *ch = &config.slaves[0].channels[0];
    memset(ch, 0, sizeof(*ch));
    ch->iec_location[0] = '\0';
    TEST_ASSERT_EQUAL_INT(ECAT_CONFIG_OK, ecat_config_validate(&config));
}
