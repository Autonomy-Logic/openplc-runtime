// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file test_ethercat_data_types.c
 * @brief Unit tests for ecat_parse_data_type() — data type string recognition
 *
 * Tests all recognized CoE/EtherCAT type names, common IEC 61131-3 aliases,
 * case-insensitivity, and the UNKNOWN fallback for unrecognized strings.
 */

#include "ethercat_config.h"
#include "unity.h"

TEST_SOURCE_FILE("core/src/drivers/plugins/native/ethercat/cjson/cJSON.c")

void setUp(void) {}
void tearDown(void) {}

/* ---- NULL and empty input ---- */

void test_parse_data_type_NullString_ShouldReturnUnknown(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UNKNOWN, ecat_parse_data_type(NULL));
}

void test_parse_data_type_EmptyString_ShouldReturnUnknown(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UNKNOWN, ecat_parse_data_type(""));
}

/* ---- Boolean ---- */

void test_parse_data_type_Bool_ShouldReturnBool(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_BOOL, ecat_parse_data_type("BOOL"));
}

void test_parse_data_type_BoolLowerCase_ShouldReturnBool(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_BOOL, ecat_parse_data_type("bool"));
}

void test_parse_data_type_BoolMixedCase_ShouldReturnBool(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_BOOL, ecat_parse_data_type("Bool"));
}

/* ---- 8-bit signed integer ---- */

void test_parse_data_type_Int8_ShouldReturnInt8(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_INT8, ecat_parse_data_type("INT8"));
}

void test_parse_data_type_Sint_ShouldReturnInt8(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_INT8, ecat_parse_data_type("SINT"));
}

/* ---- 8-bit unsigned integer ---- */

void test_parse_data_type_Uint8_ShouldReturnUint8(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT8, ecat_parse_data_type("UINT8"));
}

void test_parse_data_type_Usint_ShouldReturnUint8(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT8, ecat_parse_data_type("USINT"));
}

void test_parse_data_type_Byte_ShouldReturnUint8(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT8, ecat_parse_data_type("BYTE"));
}

/* ---- 16-bit signed integer ---- */

void test_parse_data_type_Int16_ShouldReturnInt16(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_INT16, ecat_parse_data_type("INT16"));
}

void test_parse_data_type_Int_ShouldReturnInt16(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_INT16, ecat_parse_data_type("INT"));
}

/* ---- 16-bit unsigned integer ---- */

void test_parse_data_type_Uint16_ShouldReturnUint16(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT16, ecat_parse_data_type("UINT16"));
}

void test_parse_data_type_Uint_ShouldReturnUint16(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT16, ecat_parse_data_type("UINT"));
}

void test_parse_data_type_Word_ShouldReturnUint16(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT16, ecat_parse_data_type("WORD"));
}

/* ---- 32-bit signed integer ---- */

void test_parse_data_type_Int32_ShouldReturnInt32(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_INT32, ecat_parse_data_type("INT32"));
}

void test_parse_data_type_Dint_ShouldReturnInt32(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_INT32, ecat_parse_data_type("DINT"));
}

/* ---- 32-bit unsigned integer ---- */

void test_parse_data_type_Uint32_ShouldReturnUint32(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT32, ecat_parse_data_type("UINT32"));
}

void test_parse_data_type_Udint_ShouldReturnUint32(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT32, ecat_parse_data_type("UDINT"));
}

void test_parse_data_type_Dword_ShouldReturnUint32(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT32, ecat_parse_data_type("DWORD"));
}

/* ---- 64-bit signed integer ---- */

void test_parse_data_type_Int64_ShouldReturnInt64(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_INT64, ecat_parse_data_type("INT64"));
}

void test_parse_data_type_Lint_ShouldReturnInt64(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_INT64, ecat_parse_data_type("LINT"));
}

/* ---- 64-bit unsigned integer ---- */

void test_parse_data_type_Uint64_ShouldReturnUint64(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT64, ecat_parse_data_type("UINT64"));
}

void test_parse_data_type_Ulint_ShouldReturnUint64(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT64, ecat_parse_data_type("ULINT"));
}

void test_parse_data_type_Lword_ShouldReturnUint64(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UINT64, ecat_parse_data_type("LWORD"));
}

/* ---- 32-bit float (REAL) ---- */

void test_parse_data_type_Real_ShouldReturnReal32(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL32, ecat_parse_data_type("REAL"));
}

void test_parse_data_type_Real32_ShouldReturnReal32(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL32, ecat_parse_data_type("REAL32"));
}

void test_parse_data_type_Float_ShouldReturnReal32(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL32, ecat_parse_data_type("FLOAT"));
}

void test_parse_data_type_RealLowerCase_ShouldReturnReal32(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL32, ecat_parse_data_type("real"));
}

void test_parse_data_type_FloatMixedCase_ShouldReturnReal32(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL32, ecat_parse_data_type("Float"));
}

/* ---- 64-bit float (LREAL) ---- */

void test_parse_data_type_Lreal_ShouldReturnReal64(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL64, ecat_parse_data_type("LREAL"));
}

void test_parse_data_type_Real64_ShouldReturnReal64(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL64, ecat_parse_data_type("REAL64"));
}

void test_parse_data_type_Double_ShouldReturnReal64(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL64, ecat_parse_data_type("DOUBLE"));
}

void test_parse_data_type_LrealLowerCase_ShouldReturnReal64(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL64, ecat_parse_data_type("lreal"));
}

void test_parse_data_type_DoubleMixedCase_ShouldReturnReal64(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_REAL64, ecat_parse_data_type("Double"));
}

/* ---- Padding ---- */

void test_parse_data_type_Pad_ShouldReturnPad(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_PAD, ecat_parse_data_type("PAD"));
}

void test_parse_data_type_PadLowerCase_ShouldReturnPad(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_PAD, ecat_parse_data_type("pad"));
}

/* ---- Unrecognized strings ---- */

void test_parse_data_type_Garbage_ShouldReturnUnknown(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UNKNOWN, ecat_parse_data_type("GARBAGE"));
}

void test_parse_data_type_Int128_ShouldReturnUnknown(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UNKNOWN, ecat_parse_data_type("INT128"));
}

void test_parse_data_type_WhitespaceString_ShouldReturnUnknown(void)
{
    TEST_ASSERT_EQUAL_INT(ECAT_DTYPE_UNKNOWN, ecat_parse_data_type(" BOOL"));
}
