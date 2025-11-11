#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>

// Include IEC types
#include "../../../../lib/iec_types.h"

// Define plugin_runtime_args_t structure locally to avoid Python dependencies
typedef struct
{
    // Buffer pointers
    IEC_BOOL *(*bool_input)[8];
    IEC_BOOL *(*bool_output)[8];
    IEC_BYTE **byte_input;
    IEC_BYTE **byte_output;
    IEC_UINT **int_input;
    IEC_UINT **int_output;
    IEC_UDINT **dint_input;
    IEC_UDINT **dint_output;
    IEC_ULINT **lint_input;
    IEC_ULINT **lint_output;
    IEC_UINT **int_memory;
    IEC_UDINT **dint_memory;
    IEC_ULINT **lint_memory;

    // Mutex functions
    int (*mutex_take)(pthread_mutex_t *mutex);
    int (*mutex_give)(pthread_mutex_t *mutex);
    pthread_mutex_t *buffer_mutex;
    char plugin_specific_config_file_path[256];

    // Buffer size information
    int buffer_size;
    int bits_per_buffer;
} plugin_runtime_args_t;

// Global variable to track plugin state
static int plugin_initialized = 0;
static int plugin_running = 0;

// Required init function
// This function is called when the plugin is loaded
// args: pointer to plugin_runtime_args_t structure containing runtime buffers and mutex functions
int init(void *args)
{
    printf("[TEST_PLUGIN]: Initializing test plugin...\n");

    if (!args) {
        fprintf(stderr, "[TEST_PLUGIN]: Error - init args is NULL\n");
        return -1;
    }

    plugin_runtime_args_t *runtime_args = (plugin_runtime_args_t *)args;

    // Print some information about the runtime args
    printf("[TEST_PLUGIN]: Buffer size: %d\n", runtime_args->buffer_size);
    printf("[TEST_PLUGIN]: Bits per buffer: %d\n", runtime_args->bits_per_buffer);
    printf("[TEST_PLUGIN]: Plugin config path: %s\n", runtime_args->plugin_specific_config_file_path);

    // Test mutex functions if available
    if (runtime_args->mutex_take && runtime_args->mutex_give && runtime_args->buffer_mutex) {
        printf("[TEST_PLUGIN]: Testing mutex functions...\n");
        if (runtime_args->mutex_take(runtime_args->buffer_mutex) == 0) {
            printf("[TEST_PLUGIN]: Mutex acquired successfully\n");
            runtime_args->mutex_give(runtime_args->buffer_mutex);
            printf("[TEST_PLUGIN]: Mutex released successfully\n");
        } else {
            fprintf(stderr, "[TEST_PLUGIN]: Failed to acquire mutex\n");
        }
    }

    plugin_initialized = 1;
    printf("[TEST_PLUGIN]: Test plugin initialized successfully!\n");
    return 0;
}

// Optional start_loop function
// This function is called when the plugin should start its main loop
void start_loop()
{
    if (!plugin_initialized) {
        fprintf(stderr, "[TEST_PLUGIN]: Cannot start - plugin not initialized\n");
        return;
    }

    printf("[TEST_PLUGIN]: Starting test plugin loop...\n");
    plugin_running = 1;
    printf("[TEST_PLUGIN]: Test plugin loop started!\n");
}

// Optional stop_loop function
// This function is called when the plugin should stop its main loop
void stop_loop()
{
    if (!plugin_running) {
        printf("[TEST_PLUGIN]: Plugin loop already stopped\n");
        return;
    }

    printf("[TEST_PLUGIN]: Stopping test plugin loop...\n");
    plugin_running = 0;
    printf("[TEST_PLUGIN]: Test plugin loop stopped!\n");
}

// Optional cycle_start function
// This function is called at the start of each PLC cycle if the plugin needs to run synchronously
void cycle_start()
{
    if (!plugin_initialized || !plugin_running) {
        return; // Silent if not running
    }

    // Simple test - just print a message occasionally
    static int cycle_count = 0;
    cycle_count++;

    if (cycle_count % 1000 == 0) { // Print every 1000 cycles
        printf("[TEST_PLUGIN]: Starting cycle %d\n", cycle_count);
    }
}

// Optional cycle_end function
// This function is called at the end of each PLC cycle if the plugin needs to run synchronously
void cycle_end()
{
    if (!plugin_initialized || !plugin_running) {
        return; // Silent if not running
    }

    // Simple test - just print a message occasionally
    static int cycle_count = 0;
    cycle_count++;

    if (cycle_count % 1000 == 0) { // Print every 1000 cycles
        printf("[TEST_PLUGIN]: Ending cycle %d\n", cycle_count);
    }
}

// Optional cleanup function
// This function is called when the plugin is being unloaded
void cleanup()
{
    printf("[TEST_PLUGIN]: Cleaning up test plugin...\n");

    if (plugin_running) {
        stop_loop();
    }

    plugin_initialized = 0;
    printf("[TEST_PLUGIN]: Test plugin cleaned up successfully!\n");
}
