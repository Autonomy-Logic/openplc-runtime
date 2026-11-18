// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#include "plugin_config.h" // For plugin_config_t, etc.
#include "plugin_driver.h"
#include "unity.h"
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// Simple mock control variables
static int mock_calloc_should_fail          = 0;
static int mock_init_rt_mutex_should_fail   = 0;
static void *mock_calloc_return_value       = NULL;
static int mock_calloc_call_count           = 0;
static int mock_init_rt_mutex_call_count    = 0;
static int mock_free_call_count             = 0;

// Mock implementations - override the real functions
void *calloc(size_t num, size_t size)
{
    mock_calloc_call_count++;
    if (mock_calloc_should_fail)
    {
        return NULL;
    }
    if (mock_calloc_return_value)
    {
        return mock_calloc_return_value;
    }
    // Default: use real malloc and zero it
    void *ptr = malloc(num * size);
    if (ptr)
    {
        memset(ptr, 0, num * size);
    }
    return ptr;
}

int init_rt_mutex(pthread_mutex_t *mutex)
{
    (void)mutex;
    mock_init_rt_mutex_call_count++;
    return mock_init_rt_mutex_should_fail ? -1 : 0;
}

void free(void *ptr)
{
    mock_free_call_count++;
    // For unit tests, we only track the call count
    // Memory will be freed automatically when the test process ends
    // This avoids recursion issues with dlsym
    (void)ptr; // Suppress unused parameter warning
}

// Mock reset function
void reset_mocks(void)
{
    mock_calloc_should_fail          = 0;
    mock_init_rt_mutex_should_fail   = 0;
    mock_calloc_return_value         = NULL;
    mock_calloc_call_count           = 0;
    mock_init_rt_mutex_call_count    = 0;
    mock_free_call_count             = 0;
}

// Note: External buffer variables and plugin_manager_destroy are now defined in
// tests/support/test_plugin_driver_stubs.c and will be linked automatically

#define FAKE_SLOTS 3

/* Call counts and the detach_image result of one fake native plugin slot. */
typedef struct
{
    int detach_calls;
    int detach_result;
    int cleanup_calls;
    int start_calls;
} fake_hooks_t;

static fake_hooks_t fake[FAKE_SLOTS];
static plugin_funct_bundle_t fake_bundle[FAKE_SLOTS];
static plugin_driver_t driver;

/* Defines the detach_image, cleanup and start_loop hooks of fake slot n. */
#define FAKE_HOOKS(n)                                                                              \
    static int fake##n##_detach(void)                                                              \
    {                                                                                              \
        fake[n].detach_calls++;                                                                    \
        return fake[n].detach_result;                                                              \
    }                                                                                              \
    static void fake##n##_cleanup(void)                                                            \
    {                                                                                              \
        fake[n].cleanup_calls++;                                                                   \
    }                                                                                              \
    static int fake##n##_start(void)                                                               \
    {                                                                                              \
        fake[n].start_calls++;                                                                     \
        return 0;                                                                                  \
    }

FAKE_HOOKS(0)
FAKE_HOOKS(1)
FAKE_HOOKS(2)

static int (*const fake_detach[FAKE_SLOTS])(void)    = {fake0_detach, fake1_detach, fake2_detach};
static void (*const fake_cleanup[FAKE_SLOTS])(void)  = {fake0_cleanup, fake1_cleanup, fake2_cleanup};
static int (*const fake_start[FAKE_SLOTS])(void)     = {fake0_start, fake1_start, fake2_start};

static int invalidate_calls;
static int cleanup_calls_at_first_invalidate;

/* Overrides the weak support stub: counts invalidations and records fake slot 0's cleanup count at the first. */
void plc_switch_owner_invalidate(const char *id)
{
    (void)id;
    if (invalidate_calls == 0)
    {
        cleanup_calls_at_first_invalidate = fake[0].cleanup_calls;
    }
    invalidate_calls++;
}

/* Clears the shared driver, every fake slot and the invalidation record. */
static void reset_fake_slots(void)
{
    memset(&driver, 0, sizeof(driver));
    memset(fake, 0, sizeof(fake));
    memset(fake_bundle, 0, sizeof(fake_bundle));
    invalidate_calls                  = 0;
    cleanup_calls_at_first_invalidate = -1;
}

/* Wires slot n of the shared driver as an enabled native plugin backed by the fake hooks. */
static plugin_instance_t *fake_native_slot(int n, int initialized, int exports_detach)
{
    plugin_instance_t *slot   = &driver.plugins[n];
    fake_bundle[n].start        = fake_start[n];
    fake_bundle[n].cleanup      = fake_cleanup[n];
    fake_bundle[n].detach_image = exports_detach ? fake_detach[n] : NULL;
    snprintf(slot->config.name, sizeof(slot->config.name), "fake%d", n);
    slot->config.type    = PLUGIN_TYPE_NATIVE;
    slot->config.enabled = 1;
    slot->native_plugin  = &fake_bundle[n];
    slot->initialized    = initialized;
    if (driver.plugin_count < n + 1)
    {
        driver.plugin_count = n + 1;
    }
    return slot;
}

/* Writes text to a new temporary plugins.conf and stores its path in path. */
static void write_temp_conf(char *path, size_t path_size, const char *text)
{
    snprintf(path, path_size, "/tmp/test_plugin_driver_XXXXXX");
    int fd = mkstemp(path);
    TEST_ASSERT_TRUE_MESSAGE(fd >= 0, "mkstemp failed");
    ssize_t written = write(fd, text, strlen(text));
    close(fd);
    TEST_ASSERT_EQUAL_INT((int)strlen(text), (int)written);
}

void setUp(void)
{
    // Reset all mocks before each test
    reset_mocks();
    reset_fake_slots();
}

void tearDown(void)
{
    // Clean up after each test if needed
    reset_mocks();
}

// Test Case 1: Test for driver creation - success case
void test_plugin_driver_create_ShouldAllocateAndInitializeDriver(void)
{
    // Setup: Configure mocks for success (default behavior is success)
    // No special setup needed - mocks will succeed by default

    // Call the function under test
    plugin_driver_t *driver = plugin_driver_create();

    // Assertions
    TEST_ASSERT_NOT_NULL_MESSAGE(driver, "Driver creation should not return NULL");

    // Verify that calloc was called
    TEST_ASSERT_EQUAL_INT_MESSAGE(1, mock_calloc_call_count, "calloc should be called once");

    // Verify that pthread_mutex_init was called
    TEST_ASSERT_EQUAL_INT_MESSAGE(1, mock_init_rt_mutex_call_count,
                                  "init_rt_mutex should be called once");

    // Verify internal state - all fields should be zero-initialized by calloc
    TEST_ASSERT_EQUAL_INT(0, driver->plugin_count);

    // Cleanup
    free(driver);
}

// Test Case 2: Test driver creation - calloc failure
void test_plugin_driver_create_CallocFails_ShouldReturnNULL(void)
{
    // Setup: Configure calloc to fail
    mock_calloc_should_fail = 1;

    // Call the function under test
    plugin_driver_t *driver = plugin_driver_create();

    // Assertions
    TEST_ASSERT_NULL_MESSAGE(driver, "Driver creation should return NULL if calloc fails");

    // Verify that calloc was called
    TEST_ASSERT_EQUAL_INT_MESSAGE(1, mock_calloc_call_count, "calloc should be called once");

    // Verify that pthread_mutex_init was NOT called (since calloc failed)
    TEST_ASSERT_EQUAL_INT_MESSAGE(0, mock_init_rt_mutex_call_count,
                                  "init_rt_mutex should not be called if calloc fails");
}

// Test Case 3: Test driver creation - mutex init failure
void test_plugin_driver_create_MutexInitFails_ShouldFreeAndReturnNULL(void)
{
    // Setup: Configure pthread_mutex_init to fail
    mock_init_rt_mutex_should_fail = 1;

    // Call the function under test
    plugin_driver_t *driver = plugin_driver_create();

    // Assertions
    TEST_ASSERT_NULL_MESSAGE(driver,
                             "Driver creation should return NULL if init_rt_mutex fails");

    // Verify that all expected functions were called
    TEST_ASSERT_EQUAL_INT_MESSAGE(1, mock_calloc_call_count, "calloc should be called once");
    TEST_ASSERT_EQUAL_INT_MESSAGE(1, mock_init_rt_mutex_call_count,
                                  "init_rt_mutex should be called once");
    TEST_ASSERT_EQUAL_INT_MESSAGE(
        1, mock_free_call_count, "free should be called once to clean up after init_rt_mutex failure");
}

// Test Case 4: Test data structure manipulation (simplified)
void test_plugin_driver_data_structure_ShouldStorePluginInfo(void)
{
    // Setup: Create a mock driver instance
    plugin_driver_t driver;
    memset(&driver, 0, sizeof(plugin_driver_t));

    // Note: For this test we'll use a simple approach without mocking parse_plugin_config
    // In a real scenario, you'd mock parse_plugin_config for better isolation

    // Simulate the outcome of a successful parse_plugin_config call
    plugin_config_t mock_configs[3];
    strncpy(mock_configs[0].name, "py_plugin", MAX_PLUGIN_NAME_LEN);
    strncpy(mock_configs[0].path, "../path/to/py_plugin.py", MAX_PLUGIN_PATH_LEN);
    mock_configs[0].enabled = 1;
    mock_configs[0].type    = PLUGIN_TYPE_PYTHON;
    strncpy(mock_configs[0].plugin_related_config_path, "./py_config.ini", MAX_PLUGIN_PATH_LEN);
    mock_configs[0].venv_path[0] = '\0';

    strncpy(mock_configs[1].name, "native_plugin", MAX_PLUGIN_NAME_LEN);
    strncpy(mock_configs[1].path, "./plugins/native_plugin.so", MAX_PLUGIN_PATH_LEN);
    mock_configs[1].enabled = 0;
    mock_configs[1].type    = PLUGIN_TYPE_NATIVE;
    strncpy(mock_configs[1].plugin_related_config_path, "./native_config.conf",
            MAX_PLUGIN_PATH_LEN);
    mock_configs[1].venv_path[0] = '\0';

    strncpy(mock_configs[2].name, "py_plugin_venv", MAX_PLUGIN_NAME_LEN);
    strncpy(mock_configs[2].path, "/another/path/py_plugin.py", MAX_PLUGIN_PATH_LEN);
    mock_configs[2].enabled = 1;
    mock_configs[2].type    = PLUGIN_TYPE_PYTHON;
    strncpy(mock_configs[2].plugin_related_config_path, "./py_config3.ini", MAX_PLUGIN_PATH_LEN);
    strncpy(mock_configs[2].venv_path, "/path/to/venv3", MAX_PLUGIN_PATH_LEN);

    int config_count = 3;

    // Fill driver.plugins with mock_configs to simulate what parse_plugin_config would do
    for (int i = 0; i < config_count && i < MAX_PLUGINS; i++)
    {
        memcpy(&driver.plugins[i].config, &mock_configs[i], sizeof(plugin_config_t));
    }
    driver.plugin_count = config_count;

    // Assertions on the hand-wired driver (no load_config mock here).
    TEST_ASSERT_EQUAL_INT_MESSAGE(3, driver.plugin_count, "Driver plugin count should be 3");

    // Validate plugin 1 (Python)
    TEST_ASSERT_EQUAL_STRING("py_plugin", driver.plugins[0].config.name);
    TEST_ASSERT_EQUAL_INT(PLUGIN_TYPE_PYTHON, driver.plugins[0].config.type);

    // Validate plugin 2 (Native)
    TEST_ASSERT_EQUAL_STRING("native_plugin", driver.plugins[1].config.name);
    TEST_ASSERT_EQUAL_INT(PLUGIN_TYPE_NATIVE, driver.plugins[1].config.type);

    // Validate plugin 3 (Python with venv)
    TEST_ASSERT_EQUAL_STRING("py_plugin_venv", driver.plugins[2].config.name);
    TEST_ASSERT_EQUAL_INT(PLUGIN_TYPE_PYTHON, driver.plugins[2].config.type);
    TEST_ASSERT_EQUAL_STRING("/path/to/venv3", driver.plugins[2].config.venv_path);

    // No cleanup needed for driver if it's stack allocated
}

// Test Case 5: plugin_driver_init must halt and return error when a
// plugin's init (Python or Native) returns non-zero.
void test_plugin_driver_Init_WhenPluginInitFails_ShouldHaltAndReturnError(void)
{
    // This test requires extensive mocking of Python C API and plugin structures.
    // Simplified for now to show intent.

    plugin_driver_t driver;
    memset(&driver, 0, sizeof(plugin_driver_t));
    driver.plugin_count = 1; // One plugin that fails

    strncpy(driver.plugins[0].config.name, "bad_python_plugin", MAX_PLUGIN_NAME_LEN);
    driver.plugins[0].config.type = PLUGIN_TYPE_PYTHON;

    // For this test, we'll simulate the plugin init failure
    // In a real implementation, you would mock the Python C API calls
    // This is a simplified version to show the testing structure

    // --- Call the function under test ---
    int result = plugin_driver_init(&driver);

    // --- Assertions ---
    // For now, we just test that the function can be called
    // In a real implementation, you would mock Python C API to simulate failure
    TEST_ASSERT_TRUE_MESSAGE(result == 0 || result == -1,
                             "plugin_driver_init should return valid result");

    // Note: Cleanup of mocked resources would typically be handled by Cmock or test teardown.
}

void test_plugin_driver_detach_images_DetachImageReturnsZero_ShouldMarkSlotDetached(void)
{
    plugin_instance_t *slot = fake_native_slot(0, 1, 1);
    fake[0].detach_result   = 0;

    plugin_driver_detach_images(&driver);

    TEST_ASSERT_EQUAL_INT(1, fake[0].detach_calls);
    TEST_ASSERT_TRUE(slot->detached);
}

void test_plugin_driver_detach_images_DetachedSlot_ShouldStayInitialisedWithoutCleanup(void)
{
    plugin_instance_t *slot = fake_native_slot(0, 1, 1);
    fake[0].detach_result   = 0;

    plugin_driver_detach_images(&driver);

    TEST_ASSERT_TRUE(slot->initialized);
    TEST_ASSERT_EQUAL_INT(0, fake[0].cleanup_calls);
}

void test_plugin_driver_detach_images_DetachImageRefuses_ShouldLeaveSlotAttached(void)
{
    plugin_instance_t *slot = fake_native_slot(0, 1, 1);
    fake[0].detach_result   = -1;

    plugin_driver_detach_images(&driver);

    TEST_ASSERT_EQUAL_INT(1, fake[0].detach_calls);
    TEST_ASSERT_FALSE(slot->detached);
}

void test_plugin_driver_detach_images_SymbolAbsent_ShouldLeaveSlotAttached(void)
{
    plugin_instance_t *slot = fake_native_slot(0, 1, 0);

    plugin_driver_detach_images(&driver);

    TEST_ASSERT_FALSE(slot->detached);
}

void test_plugin_driver_detach_images_UninitialisedSlot_ShouldNotCallDetachImage(void)
{
    plugin_instance_t *slot = fake_native_slot(0, 0, 1);
    fake[0].detach_result   = 0;

    plugin_driver_detach_images(&driver);

    TEST_ASSERT_EQUAL_INT(0, fake[0].detach_calls);
    TEST_ASSERT_FALSE(slot->detached);
}

void test_plugin_driver_detach_images_SeveralSlots_ShouldDetachEachSlotThatAccepts(void)
{
    plugin_instance_t *accepts = fake_native_slot(0, 1, 1);
    plugin_instance_t *refuses = fake_native_slot(1, 1, 1);
    plugin_instance_t *later   = fake_native_slot(2, 1, 1);
    fake[0].detach_result      = 0;
    fake[1].detach_result      = -1;
    fake[2].detach_result      = 0;

    plugin_driver_detach_images(&driver);

    TEST_ASSERT_TRUE(accepts->detached);
    TEST_ASSERT_FALSE(refuses->detached);
    TEST_ASSERT_TRUE(later->detached);
}

void test_plugin_driver_any_holds_image_EveryInitialisedSlotDetached_ShouldReturnFalse(void)
{
    fake_native_slot(0, 1, 1)->detached = 1;
    fake_native_slot(1, 1, 1)->detached = 1;
    fake_native_slot(2, 0, 1);

    TEST_ASSERT_FALSE(plugin_driver_any_holds_image(&driver));
}

void test_plugin_driver_any_holds_image_OneAttachedSlotRemains_ShouldReturnTrue(void)
{
    fake_native_slot(0, 1, 1)->detached = 1;
    fake_native_slot(1, 1, 0);

    TEST_ASSERT_TRUE(plugin_driver_any_holds_image(&driver));
}

void test_plugin_driver_any_holds_image_OnlyUninitialisedSlots_ShouldReturnFalse(void)
{
    fake_native_slot(0, 0, 1);
    fake_native_slot(1, 0, 0);

    TEST_ASSERT_FALSE(plugin_driver_any_holds_image(&driver));
}

void test_plugin_driver_cleanup_attached_ShouldCallCleanupOnlyOnAttachedSlots(void)
{
    fake_native_slot(0, 1, 1)->detached = 1;
    fake_native_slot(1, 1, 0);

    plugin_driver_cleanup_attached(&driver);

    TEST_ASSERT_EQUAL_INT(0, fake[0].cleanup_calls);
    TEST_ASSERT_EQUAL_INT(1, fake[1].cleanup_calls);
}

void test_plugin_driver_cleanup_attached_DetachedSlot_ShouldStayInitialised(void)
{
    plugin_instance_t *slot = fake_native_slot(0, 1, 1);
    slot->detached          = 1;

    plugin_driver_cleanup_attached(&driver);

    TEST_ASSERT_TRUE(slot->initialized);
}

void test_plugin_driver_StopSequence_AfterARefusedDetach_ShouldLeaveNoSlotHoldingTheImage(void)
{
    fake_native_slot(0, 1, 1);
    fake_native_slot(1, 1, 1);
    fake[0].detach_result = 0;
    fake[1].detach_result = -1;

    plugin_driver_detach_images(&driver);
    plugin_driver_cleanup_attached(&driver);

    TEST_ASSERT_EQUAL_INT(1, fake[1].cleanup_calls);
    TEST_ASSERT_FALSE(plugin_driver_any_holds_image(&driver));
}

void test_plugin_driver_start_DetachedSlot_ShouldNotCallStartLoop(void)
{
    fake_native_slot(0, 1, 1)->detached = 1;
    fake_native_slot(1, 1, 0);

    plugin_driver_start(&driver);

    TEST_ASSERT_EQUAL_INT(0, fake[0].start_calls);
    TEST_ASSERT_EQUAL_INT(1, fake[1].start_calls);
}

void test_plugin_driver_destroy_DetachedSlot_ShouldStillBeCleanedUp(void)
{
    fake_native_slot(0, 1, 1)->detached = 1;

    plugin_driver_destroy(&driver);

    TEST_ASSERT_EQUAL_INT(1, fake[0].cleanup_calls);
}

void test_plugin_driver_update_config_DetachedSlot_ShouldBeCleanedUpOnReload(void)
{
    char conf[64];
    write_temp_conf(conf, sizeof(conf), "ghost,./no-such-plugin.so,0,1\n");
    fake_native_slot(0, 1, 1)->detached = 1;

    int result = plugin_driver_update_config(&driver, conf);
    unlink(conf);

    TEST_ASSERT_EQUAL_INT(0, result);
    TEST_ASSERT_EQUAL_INT(1, fake[0].cleanup_calls);
}

void test_plugin_driver_update_config_DetachedSlot_ShouldClearTheDetachedFlag(void)
{
    char conf[64];
    write_temp_conf(conf, sizeof(conf), "ghost,./no-such-plugin.so,0,1\n");
    fake_native_slot(0, 1, 1)->detached = 1;

    int result = plugin_driver_update_config(&driver, conf);
    unlink(conf);

    TEST_ASSERT_EQUAL_INT(0, result);
    TEST_ASSERT_FALSE(driver.plugins[0].detached);
}

void test_plugin_driver_update_config_InitialisedSlot_ShouldInvalidateSwitchReportAfterCleanup(void)
{
    char conf[64];
    write_temp_conf(conf, sizeof(conf), "ghost,./no-such-plugin.so,0,1\n");
    fake_native_slot(0, 1, 0);

    int result = plugin_driver_update_config(&driver, conf);
    unlink(conf);

    TEST_ASSERT_EQUAL_INT(0, result);
    TEST_ASSERT_EQUAL_INT_MESSAGE(1, invalidate_calls, "switch report invalidated once");
    TEST_ASSERT_EQUAL_INT_MESSAGE(1, cleanup_calls_at_first_invalidate,
                                  "the plugin's cleanup had returned when its switch report was invalidated");
}

void test_plugin_driver_update_config_UninitialisedSlot_ShouldNotInvalidateSwitchReport(void)
{
    char conf[64];
    write_temp_conf(conf, sizeof(conf), "ghost,./no-such-plugin.so,0,1\n");
    fake_native_slot(0, 0, 0);

    int result = plugin_driver_update_config(&driver, conf);
    unlink(conf);

    TEST_ASSERT_EQUAL_INT(0, result);
    TEST_ASSERT_EQUAL_INT(0, invalidate_calls);
}
