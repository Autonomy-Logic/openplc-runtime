/**
 * @file test_ethercat_iec_location.c
 * @brief Unit tests for ecat_io_parse_iec_location() — IEC 61131-3
 *        location string parser ("%IX0.3", "%QW3", etc.).
 */

#include "ethercat_io.h"
#include "unity.h"

TEST_SOURCE_FILE("core/src/drivers/plugins/native/ethercat/cjson/cJSON.c")

void setUp(void) {}
void tearDown(void) {}

/* ---- Valid: bit access (X) ---- */

void test_iec_location_InputBit_ShouldParseFully(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(0, ecat_io_parse_iec_location("%IX0.3", &loc));
    TEST_ASSERT_EQUAL_INT(IEC_DIR_INPUT, loc.direction);
    TEST_ASSERT_EQUAL_INT(IEC_SIZE_BIT, loc.size);
    TEST_ASSERT_EQUAL_INT(0, loc.byte_index);
    TEST_ASSERT_EQUAL_INT(3, loc.bit_index);
}

void test_iec_location_OutputBit_ShouldParseFully(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(0, ecat_io_parse_iec_location("%QX5.7", &loc));
    TEST_ASSERT_EQUAL_INT(IEC_DIR_OUTPUT, loc.direction);
    TEST_ASSERT_EQUAL_INT(IEC_SIZE_BIT, loc.size);
    TEST_ASSERT_EQUAL_INT(5, loc.byte_index);
    TEST_ASSERT_EQUAL_INT(7, loc.bit_index);
}

void test_iec_location_BitWithoutDot_ShouldDefaultBitZero(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(0, ecat_io_parse_iec_location("%IX42", &loc));
    TEST_ASSERT_EQUAL_INT(IEC_SIZE_BIT, loc.size);
    TEST_ASSERT_EQUAL_INT(42, loc.byte_index);
    TEST_ASSERT_EQUAL_INT(0, loc.bit_index);
}

/* ---- Valid: byte/word/dword/lword ---- */

void test_iec_location_Byte_ShouldParseAndSetBitIndexNegative(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(0, ecat_io_parse_iec_location("%IB10", &loc));
    TEST_ASSERT_EQUAL_INT(IEC_SIZE_BYTE, loc.size);
    TEST_ASSERT_EQUAL_INT(10, loc.byte_index);
    TEST_ASSERT_EQUAL_INT(-1, loc.bit_index);
}

void test_iec_location_Word_ShouldParse(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(0, ecat_io_parse_iec_location("%QW3", &loc));
    TEST_ASSERT_EQUAL_INT(IEC_DIR_OUTPUT, loc.direction);
    TEST_ASSERT_EQUAL_INT(IEC_SIZE_WORD, loc.size);
    TEST_ASSERT_EQUAL_INT(3, loc.byte_index);
    TEST_ASSERT_EQUAL_INT(-1, loc.bit_index);
}

void test_iec_location_DWord_ShouldParse(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(0, ecat_io_parse_iec_location("%ID7", &loc));
    TEST_ASSERT_EQUAL_INT(IEC_SIZE_DWORD, loc.size);
    TEST_ASSERT_EQUAL_INT(7, loc.byte_index);
}

void test_iec_location_LWord_ShouldParse(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(0, ecat_io_parse_iec_location("%QL16", &loc));
    TEST_ASSERT_EQUAL_INT(IEC_SIZE_LWORD, loc.size);
    TEST_ASSERT_EQUAL_INT(16, loc.byte_index);
}

/* ---- Case insensitivity ---- */

void test_iec_location_LowerCase_ShouldParse(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(0, ecat_io_parse_iec_location("%ix0.3", &loc));
    TEST_ASSERT_EQUAL_INT(IEC_DIR_INPUT, loc.direction);
    TEST_ASSERT_EQUAL_INT(IEC_SIZE_BIT, loc.size);
    TEST_ASSERT_EQUAL_INT(3, loc.bit_index);
}

/* ---- NULL guards ---- */

void test_iec_location_NullString_ShouldFail(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location(NULL, &loc));
}

void test_iec_location_NullOutput_ShouldFail(void)
{
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location("%IX0.0", NULL));
}

/* ---- Invalid input ---- */

void test_iec_location_NoLeadingPercent_ShouldFail(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location("IX0.0", &loc));
}

void test_iec_location_BadDirection_ShouldFail(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location("%MX0.0", &loc));
}

void test_iec_location_BadSize_ShouldFail(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location("%IZ0", &loc));
}

void test_iec_location_NonDigitByte_ShouldFail(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location("%IXfoo", &loc));
}

void test_iec_location_BitIndexOutOfRange_ShouldFail(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location("%IX0.8", &loc));
}

void test_iec_location_BitIndexOnNonBitSize_ShouldFail(void)
{
    /* '.bit' is only valid for X size; %IB0.3 must be rejected. */
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location("%IB0.3", &loc));
}

void test_iec_location_TrailingGarbage_ShouldFail(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location("%IX0.3xyz", &loc));
}

void test_iec_location_Empty_ShouldFail(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location("", &loc));
}

void test_iec_location_OnlyPercent_ShouldFail(void)
{
    iec_location_t loc;
    TEST_ASSERT_EQUAL_INT(-1, ecat_io_parse_iec_location("%", &loc));
}
