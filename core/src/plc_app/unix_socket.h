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

// Spawn a detached worker thread that transitions the PLC to `target`.
// Shared with plugin_driver so a plugin's request_plc_stop callback goes
// through the same transition-guarded path as an external STOP command
// (same overlap protection: plc_claim_transition refuses while TRANSITIONING).
bool plc_begin_transition(PLCState target);

#endif // UNIX_SOCKET_H
