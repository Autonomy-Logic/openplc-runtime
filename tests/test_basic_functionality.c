#include "unity.h"

// Test fixture setup and teardown
void setUp(void)
{
    // Called before each test
}

void tearDown(void)
{
    // Called after each test
}

// Basic functionality tests
void test_basic_addition(void)
{
    TEST_ASSERT_EQUAL(4, 2 + 2);
}

void test_basic_subtraction(void)
{
    TEST_ASSERT_EQUAL(0, 5 - 5);
}

void test_string_comparison(void)
{
    TEST_ASSERT_EQUAL_STRING("hello", "hello");
}

void test_null_pointer(void)
{
    char *ptr = NULL;
    TEST_ASSERT_NULL(ptr);
}

void test_not_null_pointer(void)
{
    int value = 42;
    int *ptr  = &value;
    TEST_ASSERT_NOT_NULL(ptr);
    TEST_ASSERT_EQUAL(42, *ptr);
}