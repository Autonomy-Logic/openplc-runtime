// SPDX-License-Identifier: LGPL-3.0-or-later
// Copyright (c) 2026 Autonomy®

/**
 * @file s7comm_plugin.h
 * @brief S7Comm Plugin for OpenPLC Runtime v4
 *
 * This plugin implements a Siemens S7 communication server using the Snap7 library,
 * allowing S7-compatible HMIs and SCADA systems to communicate with OpenPLC.
 *
 * The plugin is self-contained with Snap7 sources compiled directly into the
 * shared library, requiring no external dependencies.
 */

#ifndef S7COMM_PLUGIN_H
#define S7COMM_PLUGIN_H

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Initialize the S7Comm plugin
 *
 * Called when the plugin is loaded. Initializes the Snap7 server and
 * registers data areas for S7 client access.
 *
 * @param args Pointer to plugin_runtime_args_t containing runtime buffers,
 *             mutex functions, and logging function pointers
 * @return 0 on success, -1 on failure
 */
int init(void *args);

/**
 * @brief Start the S7 server
 *
 * Starts the Snap7 server and begins accepting client connections
 * on the configured port (default: 102).
 */
int start_loop(void);

/**
 * @brief Stop the S7 server
 *
 * Stops the Snap7 server and disconnects all clients gracefully.
 */
void stop_loop(void);

/**
 * @brief Cleanup plugin resources
 *
 * Releases all resources allocated by the plugin including
 * the Snap7 server instance and data buffers.
 */
void cleanup(void);

/**
 * @brief Called at the start of each PLC scan cycle
 *
 * Synchronizes OpenPLC input buffers to S7 data areas.
 * Called with buffer mutex already held.
 */
void cycle_start(void);

/**
 * @brief Called at the end of each PLC scan cycle
 *
 * Synchronizes S7 data areas to OpenPLC output buffers.
 * Called with buffer mutex already held.
 */
void cycle_end(void);

#ifdef __cplusplus
}
#endif

#endif /* S7COMM_PLUGIN_H */
