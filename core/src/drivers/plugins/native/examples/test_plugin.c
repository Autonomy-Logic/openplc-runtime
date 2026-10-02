// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file test_plugin.c
 * @brief Example native plugin demonstrating proper logging usage
 *
 * This plugin demonstrates how to use the centralized plugin logger
 * to ensure all log messages are visible in the OpenPLC Editor.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>

/* Include the plugin logger */
#include "../plugin_logger.h"

/* Include IEC types */
#include "../../../../lib/plc_image_types.h"

/* Include the plugin types (runtime args, logging function types) */
#include "../../../plugin_types.h"

/* Global logger instance */
static plugin_logger_t g_logger;

/* Global variable to track plugin state */
static int plugin_initialized = 0;
static int plugin_running = 0;

/* Store runtime args for mutex access */
static plugin_runtime_args_t *g_runtime_args = NULL;

/**
 * @brief Initialize the plugin
 *
 * This function is called when the plugin is loaded.
 * @param args Pointer to plugin_runtime_args_t containing runtime buffers,
 *             mutex functions, and logging function pointers
 * @return 0 on success, -1 on failure
 */
int init(void *args)
{
    /* Initialize logger first (before we have runtime_args) */
    plugin_logger_init(&g_logger, "TEST_PLUGIN", NULL);
    plugin_logger_info(&g_logger, "Initializing test plugin...");

    if (!args)
    {
        plugin_logger_error(&g_logger, "init args is NULL");
        return -1;
    }

    g_runtime_args = (plugin_runtime_args_t *)args;

    /* Re-initialize logger with runtime_args for central logging */
    plugin_logger_init(&g_logger, "TEST_PLUGIN", args);

    /* Print some information about the runtime args */
    plugin_logger_info(&g_logger, "Buffer size: %d", g_runtime_args->buffer_size);
    plugin_logger_info(&g_logger, "Bits per buffer: %d", g_runtime_args->bits_per_buffer);
    plugin_logger_debug(&g_logger, "Plugin config path: %s",
                        g_runtime_args->plugin_specific_config_file_path);

    /* Test the flush-on-lock image read API if available */
    if (g_runtime_args->image_lock && g_runtime_args->image_unlock)
    {
        plugin_logger_debug(&g_logger, "Testing image lock...");
        g_runtime_args->image_lock();
        plugin_logger_debug(&g_logger, "Image lock acquired (journal drained)");
        g_runtime_args->image_unlock();
        plugin_logger_debug(&g_logger, "Image lock released");
    }

    plugin_initialized = 1;
    plugin_logger_info(&g_logger, "Test plugin initialized successfully!");
    return 0;
}

/**
 * @brief Start the plugin main loop
 *
 * This function is called when the plugin should start its main loop.
 */
void start_loop(void)
{
    if (!plugin_initialized)
    {
        plugin_logger_error(&g_logger, "Cannot start - plugin not initialized");
        return;
    }

    plugin_logger_info(&g_logger, "Starting test plugin loop...");
    plugin_running = 1;
    plugin_logger_info(&g_logger, "Test plugin loop started!");
}

/**
 * @brief Stop the plugin main loop
 *
 * This function is called when the plugin should stop its main loop.
 */
void stop_loop(void)
{
    if (!plugin_running)
    {
        plugin_logger_info(&g_logger, "Plugin loop already stopped");
        return;
    }

    plugin_logger_info(&g_logger, "Stopping test plugin loop...");
    plugin_running = 0;
    plugin_logger_info(&g_logger, "Test plugin loop stopped!");
}

/**
 * @brief Called at the start of each PLC scan cycle
 *
 * This function is called at the start of each PLC cycle if the plugin
 * needs to run synchronously with the scan cycle.
 */
void cycle_start(void)
{
    if (!plugin_initialized || !plugin_running)
    {
        return; /* Silent if not running */
    }

    /* Simple test - log a message occasionally */
    static int cycle_count = 0;
    cycle_count++;

    if (cycle_count % 1000 == 0)
    { /* Log every 1000 cycles */
        plugin_logger_debug(&g_logger, "Starting cycle %d", cycle_count);
    }
}

/**
 * @brief Called at the end of each PLC scan cycle
 *
 * This function is called at the end of each PLC cycle if the plugin
 * needs to run synchronously with the scan cycle.
 */
void cycle_end(void)
{
    if (!plugin_initialized || !plugin_running)
    {
        return; /* Silent if not running */
    }

    /* Simple test - log a message occasionally */
    static int cycle_count = 0;
    cycle_count++;

    if (cycle_count % 1000 == 0)
    { /* Log every 1000 cycles */
        plugin_logger_debug(&g_logger, "Ending cycle %d", cycle_count);
    }
}

/**
 * @brief Cleanup plugin resources
 *
 * This function is called when the plugin is being unloaded.
 */
void cleanup(void)
{
    plugin_logger_info(&g_logger, "Cleaning up test plugin...");

    if (plugin_running)
    {
        stop_loop();
    }

    plugin_initialized = 0;
    g_runtime_args = NULL;
    plugin_logger_info(&g_logger, "Test plugin cleaned up successfully!");
}
