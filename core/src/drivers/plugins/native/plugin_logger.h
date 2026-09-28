// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file plugin_logger.h
 * @brief Centralized Plugin Logger for Native OpenPLC Plugins
 *
 * Provides a simple, consistent logging interface for native (C/C++) OpenPLC plugins
 * that routes log messages through the central logging system:
 *
 *     C runtime log functions -> Unix socket -> Python log server ->
 *     REST API -> OpenPLC Editor
 *
 * This ensures all plugin logs are visible in the Editor's log viewer.
 *
 * Usage:
 *     #include "plugin_logger.h"
 *
 *     // In plugin init():
 *     plugin_logger_t logger;
 *     plugin_logger_init(&logger, "MY_PLUGIN", runtime_args);
 *
 *     // Throughout plugin:
 *     plugin_logger_info(&logger, "Server started on port %d", port);
 *     plugin_logger_error(&logger, "Connection failed: %s", error_msg);
 *     plugin_logger_warn(&logger, "Retrying connection...");
 *     plugin_logger_debug(&logger, "Processing request %d", req_id);
 *
 * The plugin name is automatically prefixed to all messages, e.g.:
 *     "[MY_PLUGIN] Server started on port 502"
 */

#ifndef PLUGIN_LOGGER_H
#define PLUGIN_LOGGER_H

#include <stdbool.h>
#include <stdarg.h>

/**
 * @brief Logging function pointer types (matching plugin_driver.h)
 */
typedef void (*plugin_log_func_t)(const char *fmt, ...);

/**
 * @brief Plugin logger structure
 *
 * Stores the plugin name and logging function pointers for use throughout
 * the plugin lifecycle.
 */
typedef struct
{
    char plugin_name[64];           /**< Plugin name used as prefix in log messages */
    plugin_log_func_t log_info;     /**< Info level logging function */
    plugin_log_func_t log_debug;    /**< Debug level logging function */
    plugin_log_func_t log_warn;     /**< Warning level logging function */
    plugin_log_func_t log_error;    /**< Error level logging function */
    bool is_valid;                  /**< True if logger is properly initialized */
} plugin_logger_t;

/**
 * @brief Initialize the plugin logger
 *
 * @param logger Pointer to the logger structure to initialize
 * @param plugin_name Name of the plugin (will be used as prefix in messages)
 * @param runtime_args Pointer to plugin_runtime_args_t containing log function pointers
 * @return true if initialization succeeded, false otherwise
 *
 * Example:
 *     plugin_logger_t logger;
 *     if (!plugin_logger_init(&logger, "MODBUS_SLAVE", runtime_args)) {
 *         fprintf(stderr, "Failed to initialize logger\n");
 *         return -1;
 *     }
 */
bool plugin_logger_init(plugin_logger_t *logger, const char *plugin_name, void *runtime_args);

/**
 * @brief Log an informational message
 *
 * @param logger Pointer to initialized plugin logger
 * @param fmt Printf-style format string
 * @param ... Format arguments
 */
void plugin_logger_info(plugin_logger_t *logger, const char *fmt, ...);

/**
 * @brief Log a debug message
 *
 * @param logger Pointer to initialized plugin logger
 * @param fmt Printf-style format string
 * @param ... Format arguments
 */
void plugin_logger_debug(plugin_logger_t *logger, const char *fmt, ...);

/**
 * @brief Log a warning message
 *
 * @param logger Pointer to initialized plugin logger
 * @param fmt Printf-style format string
 * @param ... Format arguments
 */
void plugin_logger_warn(plugin_logger_t *logger, const char *fmt, ...);

/**
 * @brief Log an error message
 *
 * @param logger Pointer to initialized plugin logger
 * @param fmt Printf-style format string
 * @param ... Format arguments
 */
void plugin_logger_error(plugin_logger_t *logger, const char *fmt, ...);

#endif /* PLUGIN_LOGGER_H */
