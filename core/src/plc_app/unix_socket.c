// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "../drivers/plugin_driver.h"
#include "debug_handler.h"
#include "plc_state_manager.h"
#include "plc_switch.h"
#include "scan_cycle_manager.h"
#include "unix_socket.h"
#include "utils/log.h"
#include "utils/utils.h"

extern volatile sig_atomic_t keep_running;

static plugin_driver_t *g_plugin_driver = NULL;

/* How long run_transition waits for the landing before reconciling with
 * the mode switch, and how often it polls. Bound comes from
 * plc_state_manager.h so the watchdog's stuck-transition timeout fires
 * strictly later than this one. */
#define LANDING_WAIT_MS PLC_TRANSITION_LANDING_TIMEOUT_MS
#define LANDING_POLL_MS 20

void unix_socket_set_plugin_driver(void *driver)
{
    g_plugin_driver = (plugin_driver_t *)driver;
}

// Body of a claimed transition: perform it, wait for it to land, then
// reconcile with the mode switch. Normally on the detached worker;
// called directly when that worker cannot be spawned.
static bool run_transition(PLCState target)
{
    bool result = plc_set_state(target);
    if (!result)
    {
        log_error("State transition to %s failed",
                  target == PLC_STATE_RUNNING ? "RUNNING" : "STOPPED");
    }

    // Wait for the landing. plc_set_state(RUNNING) returns before
    // RUNNING is published (workers come up ~4 s later on SLM-RP4), so
    // reconciling earlier throws the switch movement away. The state
    // IS the interlock; bound just prevents stranding.
    for (int waited_ms = 0; plc_state_is_transitioning() && waited_ms < LANDING_WAIT_MS;
         waited_ms += LANDING_POLL_MS)
    {
        struct timespec poll = { .tv_sec = 0, .tv_nsec = LANDING_POLL_MS * 1000000L };
        nanosleep(&poll, NULL);
    }

    // Reconcile with the mode switch: compare its final resting
    // position against the state landed on. Gated on movement so a
    // request dropped during a transition is not lost. Cannot ping-pong.
    if (plc_switch_take_movement())
    {
        const PLCState landed = plc_get_state();
        const PLCState wanted = plc_switch_allows_run() ? PLC_STATE_RUNNING : PLC_STATE_STOPPED;

        // Only reconcile from a clean landing. ERROR and EMPTY are not states to
        // "correct" — restarting a faulted or programless PLC because a switch
        // moved would fight the fault rather than report it.
        if ((landed == PLC_STATE_RUNNING || landed == PLC_STATE_STOPPED) && landed != wanted)
        {
            log_warn("Mode switch came to rest in %s but the PLC landed on %s — correcting",
                     wanted == PLC_STATE_RUNNING ? "RUN" : "STOP",
                     landed == PLC_STATE_RUNNING ? "RUNNING" : "STOPPED");

            // The movement record was consumed above, so a refusal here
            // would discard the switch's intent. Put the record back so
            // the next landing reconciles. Cannot ping-pong.
            if (!plc_begin_transition(wanted))
            {
                plc_switch_note_movement();
                log_warn("Correction to %s did not go through — re-armed for the "
                         "next landing",
                         wanted == PLC_STATE_RUNNING ? "RUN" : "STOP");
            }
        }
    }

    return result;
}

static void *transition_worker(void *arg)
{
    PLCState target = *(PLCState *)arg;
    free(arg);

    run_transition(target);
    return NULL;
}

static bool spawn_transition_worker(PLCState target)
{
    PLCState *arg = malloc(sizeof(PLCState));
    if (!arg)
    {
        log_error("Failed to allocate transition argument");
        return false;
    }
    *arg = target;

    /* Explicit SCHED_OTHER: the dispatcher (FIFO 98) also spawns this worker for a fault stop. */
    pthread_attr_t attr;
    int rc = pthread_attr_init(&attr);
    if (rc != 0)
    {
        log_error("Failed to init transition thread attributes (%s)", strerror(rc));
        free(arg);
        return false;
    }
    struct sched_param sp = {.sched_priority = 0};
    if (pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED) != 0 ||
        pthread_attr_setschedpolicy(&attr, SCHED_OTHER) != 0 ||
        pthread_attr_setschedparam(&attr, &sp) != 0)
    {
        log_warn("Transition thread inherits the caller's scheduling");
    }

    pthread_t tid;
    rc = pthread_create(&tid, &attr, transition_worker, arg);
    pthread_attr_destroy(&attr);
    if (rc != 0)
    {
        log_error("Failed to create transition thread (%s)", strerror(rc));
        free(arg);
        return false;
    }
    pthread_detach(tid);
    return true;
}

// Start a state transition on a background worker. Returns false when
// refused. Single entry point for every state change;
// plc_claim_transition arbitrates. A refused switch intent survives
// via movement reconciliation.
bool plc_begin_transition(PLCState target)
{
    if (!plc_claim_transition(target))
    {
        return false;
    }

    // Worker could not spawn: run on this thread. Publishing STOPPED
    // here would skip unload_plc_program entirely and leave plc_program
    // non-NULL. Blocking the caller is cheaper.
    if (!spawn_transition_worker(target))
    {
        log_error("Completing the transition on the calling thread");
        return run_transition(target);
    }
    return true;
}

bool plc_complete_claimed_transition_async(PLCState target)
{
    return spawn_transition_worker(target);
}

// helper: read one line terminated by '\n' from a socket
static ssize_t read_line(int fd, char *buffer, size_t max_length)
{
    size_t total_read = 0;
    char ch;
    while (total_read < max_length - 1)
    {
        ssize_t bytes_read = read(fd, &ch, 1);
        if (bytes_read <= 0)
        {
            return bytes_read; // error or connection closed
        }
        if (ch == '\n')
        {
            break; // end of line
        }
        buffer[total_read++] = ch;
    }
    buffer[total_read] = '\0'; // null-terminate the string
    return total_read;
}

static void format_status_response(char *response, size_t response_size)
{
    PLCState current_state = plc_get_state();

    // Both directions report as the one TRANSITIONING string that external
    // callers already know. The distinction is internal (intent), and the
    // webserver's _wait_for_plc_idle plus the editor both key off this wire
    // value, so it stays exactly as it was.
    if (current_state == PLC_STATE_TRANSITIONING_TO_RUN ||
        current_state == PLC_STATE_TRANSITIONING_TO_STOP)
        strncpy(response, "STATUS:TRANSITIONING\n", response_size);
    else if (current_state == PLC_STATE_INIT)
        strncpy(response, "STATUS:INIT\n", response_size);
    else if (current_state == PLC_STATE_RUNNING)
        strncpy(response, "STATUS:RUNNING\n", response_size);
    else if (current_state == PLC_STATE_STOPPED)
        strncpy(response, "STATUS:STOPPED\n", response_size);
    else if (current_state == PLC_STATE_ERROR)
        strncpy(response, "STATUS:ERROR\n", response_size);
    else if (current_state == PLC_STATE_EMPTY)
        strncpy(response, "STATUS:EMPTY\n", response_size);
    else
        strncpy(response, "STATUS:UNKNOWN\n", response_size);
}

static void format_switch_response(char *response, size_t response_size)
{
    // Report the mode-switch position a VPP plugin last stored. Devices with no
    // switch-aware plugin always answer RUN.
    if (plc_get_switch_position() == PLC_SWITCH_RUN)
        strncpy(response, "SWITCH:RUN\n", response_size);
    else
        strncpy(response, "SWITCH:STOP\n", response_size);
}

void handle_unix_socket_commands(const char *command, char *response, size_t response_size)
{
    // During a transition only reads are allowed; else COMMAND:BUSY.
    // SWITCH is a read: a plain atomic load with no coupling to
    // plc_state, so BUSY would needlessly hide switchPosition during
    // every start/stop.
    if (plc_state_is_transitioning())
    {
        if (strcmp(command, "PING") == 0)
        {
            strncpy(response, "PING:OK\n", response_size);
        }
        else if (strcmp(command, "STATUS") == 0)
        {
            format_status_response(response, response_size);
        }
        else if (strcmp(command, "SWITCH") == 0)
        {
            format_switch_response(response, response_size);
        }
        else
        {
            strncpy(response, "COMMAND:BUSY\n", response_size);
        }
        response[response_size - 1] = '\0';
        return;
    }

    if (strcmp(command, "PING") == 0)
    {
        strncpy(response, "PING:OK\n", response_size);
    }
    else if (strcmp(command, "STATUS") == 0)
    {
        format_status_response(response, response_size);
    }
    else if (strcmp(command, "STOP") == 0)
    {
        PLCState current_state = plc_get_state();
        if (current_state == PLC_STATE_RUNNING)
        {
            if (plc_begin_transition(PLC_STATE_STOPPED))
                strncpy(response, "STOP:OK\n", response_size);
            else
                strncpy(response, "STOP:ERROR\n", response_size);
        }
        else
        {
            strncpy(response, "STOP:ERROR\n", response_size);
        }
    }
    else if (strcmp(command, "SWITCH") == 0)
    {
        format_switch_response(response, response_size);
    }
    else if (strcmp(command, "START") == 0)
    {
        PLCState current_state = plc_get_state();
        // Hardware is authoritative: refuse rather than queue, so the editor
        // can tell the user to flip the switch instead of leaving a start
        // pending. Checked before the transition is ever begun.
        if (!plc_switch_allows_run())
        {
            strncpy(response, "START:ERROR_SWITCH_STOP\n", response_size);
            log_warn("Received START command but the mode switch is in STOP");
        }
        else if (current_state != PLC_STATE_RUNNING)
        {
            if (plc_begin_transition(PLC_STATE_RUNNING))
                strncpy(response, "START:OK\n", response_size);
            else
                strncpy(response, "START:ERROR\n", response_size);
        }
        else
        {
            strncpy(response, "START:ERROR_ALREADY_RUNNING\n", response_size);
            log_error("Received START command but PLC is already RUNNING");
        }
    }
    else if (strcmp(command, "STATS") == 0)
    {
        format_timing_stats_response(response, response_size);
        // Splice in any plugin-contributed statistics. Safe no-op when no
        // plugin exports get_stats.
        if (g_plugin_driver)
            plugin_driver_append_stats_json(g_plugin_driver, response, response_size);
    }
    else if (strncmp(command, "DEBUG:", 6) == 0)
    {
        uint8_t debug_data[4096] = {0};
        size_t data_length       = parse_hex_string(&command[6], debug_data);
        if (data_length > 0)
        {
            data_length = process_debug_data(debug_data, data_length);
            if (data_length > 0)
            {
                bytes_to_hex_string(debug_data, data_length, response, response_size, "DEBUG:");
                size_t len = strlen(response);
                if (len < response_size - 1)
                {
                    response[len]     = '\n';
                    response[len + 1] = '\0';
                }
            }
            else
            {
                strncpy(response, "DEBUG:ERROR_PROCESSING\n", response_size);
            }
        }
        else
        {
            strncpy(response, "DEBUG:ERROR_PARSING\n", response_size);
        }
    }
    else if (strncmp(command, "PLUGIN_CMD:", 11) == 0)
    {
        // Format: PLUGIN_CMD:<plugin_name>:<json_payload>
        // NOTE: This handler is BLOCKING -- plugin commands like EtherCAT scan
        // may take several seconds. The unix socket thread is single-client,
        // so the caller must wait for the response.
        const char *rest = &command[11];
        const char *colon = strchr(rest, ':');
        if (!colon || !g_plugin_driver)
        {
            snprintf(response, response_size,
                     "PLUGIN_CMD:ERROR:{\"error\":\"invalid format or driver not set\"}\n");
        }
        else
        {
            // Extract plugin name
            size_t name_len = colon - rest;
            char plugin_name[64] = {0};
            if (name_len >= sizeof(plugin_name))
                name_len = sizeof(plugin_name) - 1;
            strncpy(plugin_name, rest, name_len);

            const char *json_payload = colon + 1;

            // Stack-allocated buffer for plugin output.
            // MAX_RESPONSE_SIZE is 64KB; this leaves 256 bytes for the
            // "PLUGIN_CMD:OK:" prefix. Fits comfortably in the default
            // 8MB thread stack.
            char plugin_response[MAX_RESPONSE_SIZE - 256];
            memset(plugin_response, 0, sizeof(plugin_response));

            int result = plugin_driver_execute_command(g_plugin_driver, plugin_name, json_payload,
                                                       plugin_response, sizeof(plugin_response));

            if (result == 0)
            {
                snprintf(response, response_size, "PLUGIN_CMD:OK:%s\n", plugin_response);
            }
            else
            {
                snprintf(response, response_size, "PLUGIN_CMD:ERROR:%s\n", plugin_response);
            }
        }
    }
    else
    {
        log_error("Unknown command received: %s", command);
        strncpy(response, "COMMAND:ERROR\n", response_size);
    }

    // Always ensure null termination
    response[response_size - 1] = '\0';
}

void *unix_socket_thread(void *arg)
{
    (void)arg;
    int *server_fd_pt = (int *)arg;
    int client_fd;
    char command_buffer[COMMAND_BUFFER_SIZE];

    if (server_fd_pt == NULL)
    {
        log_error("Server file descriptor is NULL");
        return NULL;
    }

    int server_fd = *server_fd_pt;
    if (server_fd < 0)
    {
        log_error("Failed to set up UNIX socket");
        return NULL;
    }

    while (keep_running)
    {
        client_fd = accept(server_fd, NULL, NULL);
        if (client_fd < 0)
        {
            if (errno == EINTR)
            {
                continue; // Interrupted by signal, retry
            }
            log_error("Unix socket accept failed: %s", strerror(errno));

            // Retry after a short delay
            sleep(1);
            continue;
        }

        log_info("Unix socket client connected");

        while (keep_running)
        {
            ssize_t bytes_read = read_line(client_fd, command_buffer, COMMAND_BUFFER_SIZE);
            if (bytes_read > 0)
            {
                // Handle the command
                char response[MAX_RESPONSE_SIZE] = {0};
                handle_unix_socket_commands(command_buffer, response, MAX_RESPONSE_SIZE);
                if (strlen(response) > 0)
                {
                    ssize_t bytes_written = write(client_fd, response, strlen(response));
                    if (bytes_written <= 0)
                    {
                        log_error("Error writing on unix socket: %s", strerror(errno));
                    }
                }
            }
            else if (bytes_read == 0)
            {
                log_info("Unix socket client disconnected");
                break;
            }
            else
            {
                log_error("Unix socket read failed: %s", strerror(errno));
                break;
            }
        }
        close(client_fd);
    }

    close_unix_socket(server_fd);
    return NULL;
}

void close_unix_socket(int server_fd)
{
    if (server_fd >= 0)
    {
        close(server_fd);
        unlink(SOCKET_PATH);
        log_info("UNIX socket server closed");
    }
}

int setup_unix_socket(void)
{
    int server_fd;
    struct sockaddr_un address;

    // Remove any existing socket file
    unlink(SOCKET_PATH);

    // Create socket
    if ((server_fd = socket(AF_UNIX, SOCK_STREAM, 0)) < 0)
    {
        log_error("Socket creation failed: %s", strerror(errno));
        return -1;
    }

    // Configure socket address structure
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    strncpy(address.sun_path, SOCKET_PATH, sizeof(address.sun_path) - 1);

    // Bind socket to the address
    if (bind(server_fd, (struct sockaddr *)&address, sizeof(address)) < 0)
    {
        log_error("Socket bind failed: %s", strerror(errno));
        close(server_fd);
        return -1;
    }

    // Listen for incoming connections
    if (listen(server_fd, MAX_CLIENTS) < 0)
    {
        log_error("Socket listen failed: %s", strerror(errno));
        close(server_fd);
        return -1;
    }

    log_info("UNIX socket server setup at %s", SOCKET_PATH);

    // Create a thread to handle socket commands
    pthread_t socket_thread;
    int *fd_ptr = malloc(sizeof(int));
    *fd_ptr     = server_fd;
    if (pthread_create(&socket_thread, NULL, unix_socket_thread, fd_ptr) != 0)
    {
        log_error("Failed to create UNIX socket thread: %s", strerror(errno));
        close(server_fd);
        free(fd_ptr);
        return -1;
    }

    return 0;
}
