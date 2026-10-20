// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef UNIX_SOCKET_H
#define UNIX_SOCKET_H

#include <stdbool.h>

#include "plc_state_manager.h"

#define SOCKET_PATH "/run/runtime/plc_runtime.socket"
#define COMMAND_BUFFER_SIZE 8192
#define MAX_RESPONSE_SIZE 65536
#define MAX_CLIENTS 1

int setup_unix_socket(void);
void close_unix_socket(int server_fd);
void *unix_socket_thread(void *arg);

// Setter for the plugin driver (called by plc_main after driver creation)
void unix_socket_set_plugin_driver(void *driver);

// Spawn a detached worker that transitions the PLC to `target`. Shared
// with plugin_driver so plugin-initiated requests take the same
// transition-guarded path as a socket STOP (plc_claim_transition
// refuses while TRANSITIONING).
bool plc_begin_transition(PLCState target);

/**
 * @brief Run an already claimed transition on a new detached worker thread.
 *
 * For callers that claimed with plc_claim_transition() and must not run the
 * transition themselves, e.g. the dispatcher, which the stop's teardown joins.
 *
 * @param target PLC_STATE_RUNNING or PLC_STATE_STOPPED
 * @return false when the worker could not be spawned; the claim is still held
 */
bool plc_complete_claimed_transition_async(PLCState target);

#endif // UNIX_SOCKET_H
