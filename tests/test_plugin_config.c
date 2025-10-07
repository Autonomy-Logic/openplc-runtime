#include "plugin_config.h"
#include "plugin_driver.h"
#include "unity.h"
#include <string.h>

// Mock functions for standard library calls used in plugin_config.c
// Cmock will generate these automatically when we #include "mock_stdlib.h" or similar,
// but for direct functions like fopen, fgets, etc., we might need to create them manually
// or use a more generic mock approach if Cmock doesn't handle them out of the box.
// For simplicity, we'll assume Cmock can handle these or we'll create simple wrappers.
// Let's start by assuming Cmock handles them. If not, we'll adjust.

// Helper function to create a temporary config file for testing
static void create_test_config_file(const char *filename, const char *content)
{
    FILE *file = fopen(filename, "w");
    if (file)
    {
        fprintf(file, "%s", content);
        fclose(file);
    }
}

// Helper function to clean up the test config file
static void remove_test_config_file(const char *filename)
{
    remove(filename);
}

void setUp(void)
{
    // This function is called before each test
}

void tearDown(void)
{
    // This function is called after each test
}

// Note: External buffer variables and mock functions are now defined in
// tests/support/test_plugin_driver_stubs.c and will be linked automatically

// Test Case 1: Test parsing a valid configuration file
// This covers "Teste de leitura e parsing de configurações"
void test_parse_plugin_config_ValidFile_ShouldSucceed(void)
{
    const char *test_config_filename = "test_config_valid.conf";
    const char *config_content =
        "# This is a comment\n"
        "\n" // Empty line
        "plugin1,../path/to/plugin1.py,1,0,./config1.ini\n"
        "plugin2,./plugins/plugin2.so,0,1,./config2.conf\n"
        "plugin3,/another/path/plugin3.py,1,0,./config3.ini,/path/to/venv3\n";

    create_test_config_file(test_config_filename, config_content);

    plugin_config_t configs[MAX_PLUGINS];
    int expected_count = 3;

    int result = parse_plugin_config(test_config_filename, configs, MAX_PLUGINS);

    TEST_ASSERT_EQUAL_INT(expected_count, result);

    // Validate plugin1
    TEST_ASSERT_EQUAL_STRING("plugin1", configs[0].name);
    TEST_ASSERT_EQUAL_STRING("../path/to/plugin1.py", configs[0].path);
    TEST_ASSERT_EQUAL_INT(1, configs[0].enabled);
    TEST_ASSERT_EQUAL_INT(PLUGIN_TYPE_PYTHON, configs[0].type); // 0 for Python from plugin_config.h
    TEST_ASSERT_EQUAL_STRING("./config1.ini", configs[0].plugin_related_config_path);
    TEST_ASSERT_EQUAL_STRING("", configs[0].venv_path); // No venv_path specified

    // Validate plugin2
    TEST_ASSERT_EQUAL_STRING("plugin2", configs[1].name);
    TEST_ASSERT_EQUAL_STRING("./plugins/plugin2.so", configs[1].path);
    TEST_ASSERT_EQUAL_INT(0, configs[1].enabled);
    TEST_ASSERT_EQUAL_INT(PLUGIN_TYPE_NATIVE, configs[1].type); // 1 for Native from plugin_config.h
    TEST_ASSERT_EQUAL_STRING("./config2.conf", configs[1].plugin_related_config_path);
    TEST_ASSERT_EQUAL_STRING("", configs[1].venv_path); // No venv_path specified

    // Validate plugin3
    TEST_ASSERT_EQUAL_STRING("plugin3", configs[2].name);
    TEST_ASSERT_EQUAL_STRING("/another/path/plugin3.py", configs[2].path);
    TEST_ASSERT_EQUAL_INT(1, configs[2].enabled);
    TEST_ASSERT_EQUAL_INT(PLUGIN_TYPE_PYTHON, configs[2].type); // 0 for Python from plugin_config.h
    TEST_ASSERT_EQUAL_STRING("./config3.ini", configs[2].plugin_related_config_path);
    TEST_ASSERT_EQUAL_STRING("/path/to/venv3", configs[2].venv_path); // venv_path specified

    remove_test_config_file(test_config_filename);
}

// Test Case 2: Test parsing a file with more plugins than max_configs
void test_parse_plugin_config_TooManyPlugins_ShouldRespectMaxConfigs(void)
{
    const char *test_config_filename = "test_config_toomany.conf";
    char config_content[1024];
    int plugins_to_write = MAX_PLUGINS + 5; // Write more than MAX_PLUGINS
    int i;
    int pos = 0;

    for (i = 0; i < plugins_to_write; ++i)
    {
        pos += snprintf(config_content + pos, sizeof(config_content) - pos,
                        "plugin%d,/path/plugin%d.py,1,0,./config%d.ini\n", i, i, i);
    }

    create_test_config_file(test_config_filename, config_content);

    plugin_config_t configs[MAX_PLUGINS];
    int expected_count = MAX_PLUGINS;

    int result = parse_plugin_config(test_config_filename, configs, MAX_PLUGINS);

    TEST_ASSERT_EQUAL_INT(expected_count, result);
    // Optionally, check if the first MAX_PLUGINS entries are correctly parsed
    for (i = 0; i < expected_count; ++i)
    {
        char expected_name[20];
        snprintf(expected_name, sizeof(expected_name), "plugin%d", i);
        TEST_ASSERT_EQUAL_STRING(expected_name, configs[i].name);
    }

    remove_test_config_file(test_config_filename);
}

// Test Case 3: Test parsing a non-existent file
void test_parse_plugin_config_NonExistentFile_ShouldReturnNegative(void)
{
    const char *non_existent_filename = "non_existent_config.conf";
    plugin_config_t configs[MAX_PLUGINS];

    int result = parse_plugin_config(non_existent_filename, configs, MAX_PLUGINS);

    TEST_ASSERT_LESS_THAN(0, result);
}

// Test Case 4: Test parsing a file with a malformed line (e.g., missing fields)
void test_parse_plugin_config_MalformedLine_ShouldSkipLine(void)
{
    const char *test_config_filename = "test_config_malformed.conf";
    const char *config_content       = "plugin1,../path/to/plugin1.py,1,0,./config1.ini\n"
                                       "malformed_line\n" // This line should be skipped
                                 "plugin2,./plugins/plugin2.so,0,1,./config2.conf\n";

    create_test_config_file(test_config_filename, config_content);

    plugin_config_t configs[MAX_PLUGINS];
    int expected_count = 2; // Only the valid lines should be parsed

    int result = parse_plugin_config(test_config_filename, configs, MAX_PLUGINS);

    TEST_ASSERT_EQUAL_INT(expected_count, result);

    // Validate plugin1
    TEST_ASSERT_EQUAL_STRING("plugin1", configs[0].name);

    // Validate plugin2 (which should now be at index 1)
    TEST_ASSERT_EQUAL_STRING("plugin2", configs[1].name);

    remove_test_config_file(test_config_filename);
}

// Test Case 5: Test parsing a file with only comments and empty lines
void test_parse_plugin_config_CommentsAndEmptyOnly_ShouldReturnZero(void)
{
    const char *test_config_filename = "test_config_empty.conf";
    const char *config_content       = "# Comment line 1\n"
                                       "\n"
                                       "# Comment line 2\n"
                                       "\n";

    create_test_config_file(test_config_filename, config_content);

    plugin_config_t configs[MAX_PLUGINS];
    int expected_count = 0;

    int result = parse_plugin_config(test_config_filename, configs, MAX_PLUGINS);

    TEST_ASSERT_EQUAL_INT(expected_count, result);

    remove_test_config_file(test_config_filename);
}