#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
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
extern PLCState plc_state;

static plugin_driver_t *g_plugin_driver = NULL;

void unix_socket_set_plugin_driver(void *driver)
{
    g_plugin_driver = (plugin_driver_t *)driver;
}

// Flag to prevent overlapping state transitions (e.g. START while STOP is in progress).
// Set before spawning the transition thread, cleared when the transition completes.
static atomic_int is_transitioning = 0;

static void *transition_worker(void *arg)
{
    PLCState target = *(PLCState *)arg;
    free(arg);

    bool result = plc_set_state(target);
    if (!result)
    {
        log_error("State transition to %s failed",
                  target == PLC_STATE_RUNNING ? "RUNNING" : "STOPPED");
    }

    atomic_store(&is_transitioning, 0);

    // Converge on the mode switch.
    //
    // plc_begin_transition() returns false while another transition is in
    // flight, which means a stop request CAN be dropped: an editor START may
    // win the gate, then a plugin's set_switch_position(STOP) +
    // request_plc_stop() arrives and the stop is collapsed as "already
    // transitioning". That would leave the PLC running with the switch in
    // STOP, with nobody retrying.
    //
    // Re-checking here — after is_transitioning clears, so this call can win
    // the gate — makes every interleaving converge on STOPPED without asking
    // the requester to implement retry logic. The failure direction is the
    // safe one, which is the property to preserve in any change to either
    // caller. (The same drop was already possible for a plugin reporting a
    // hardware fault during a start; this fixes that too.)
    if (plc_get_state() == PLC_STATE_RUNNING && !plc_switch_allows_run())
    {
        log_warn("Mode switch reads STOP but the PLC ended up RUNNING — stopping");
        plc_begin_transition(PLC_STATE_STOPPED);
    }

    return NULL;
}

// Start a background thread that performs the (potentially slow) state
// transition. Returns true if the thread was spawned, false otherwise.
//
// This is the single authoritative entry point for all state transitions:
// socket START/STOP commands, plugin-initiated stops, AND the boot auto-start
// in plc_main.c (which calls this instead of plc_set_state() directly so that
// all paths share the same guard). That last point closes the race where the
// socket listener accepts a START while the auto-start is still in flight,
// which previously could spawn two concurrent load_plc_program() calls.
//
// Two safety guards on the entry path:
//
//   1. CAS on `is_transitioning` 0→1: collapses concurrent calls. A
//      misbehaving plugin spinning on plugin_request_plc_stop, or the boot
//      path racing with a socket START, would otherwise pile up detached
//      pthread workers. The CAS means only the first call fires the worker;
//      everything else is a cheap return.
//
//   2. Re-check current state AFTER the CAS wins: closes the
//      check-then-call race where the caller sees RUNNING, calls in,
//      and the state flips to STOPPED before we spawn the worker. We'd
//      otherwise dispatch a no-op transition, leaving STATUS reporting
//      TRANSITIONING for the worker's lifetime for nothing.
bool plc_begin_transition(PLCState target)
{
    int expected = 0;
    if (!atomic_compare_exchange_strong(&is_transitioning, &expected, 1))
    {
        // Another transition is already in flight. Don't pile on.
        return false;
    }

    if (plc_get_state() == target)
    {
        // State already at target — release the gate and bail. No
        // worker needed; reporting STATUS:TRANSITIONING for a no-op
        // would just confuse external pollers.
        atomic_store(&is_transitioning, 0);
        return false;
    }

    PLCState *arg = malloc(sizeof(PLCState));
    if (!arg)
    {
        log_error("Failed to allocate transition argument");
        atomic_store(&is_transitioning, 0);
        return false;
    }
    *arg = target;

    pthread_t tid;
    if (pthread_create(&tid, NULL, transition_worker, arg) != 0)
    {
        log_error("Failed to create transition thread: %s", strerror(errno));
        free(arg);
        atomic_store(&is_transitioning, 0);
        return false;
    }
    pthread_detach(tid);
    return true;
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
    // While a transition is in progress, plc_state has already flipped
    // to the target value (so the running task threads can exit their
    // `while (plc_get_state() == RUNNING)` loops) but the actual
    // load/unload work is still happening on the transition worker
    // thread. Reporting the bare state here would tell external
    // pollers STATUS:STOPPED while the runtime can't yet accept a
    // START — they'd race ahead and get COMMAND:BUSY.
    //
    // Surfacing TRANSITIONING for the duration of the worker keeps
    // _wait_for_plc_state(STOPPED) on the webserver side honest:
    // STATUS:STOPPED is reported only after the worker has completed
    // and is_transitioning has cleared.
    if (atomic_load(&is_transitioning))
    {
        strncpy(response, "STATUS:TRANSITIONING\n", response_size);
        return;
    }

    PLCState current_state = plc_get_state();

    if (current_state == PLC_STATE_INIT)
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

void handle_unix_socket_commands(const char *command, char *response, size_t response_size)
{
    // While a state transition is in progress, only allow PING and STATUS.
    // Everything else gets COMMAND:BUSY so commands cannot overlap.
    if (atomic_load(&is_transitioning))
    {
        if (strcmp(command, "PING") == 0)
        {
            strncpy(response, "PING:OK\n", response_size);
        }
        else if (strcmp(command, "STATUS") == 0)
        {
            format_status_response(response, response_size);
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
        // Report the mode-switch position a VPP plugin last stored. Devices
        // with no switch-aware plugin always answer RUN.
        if (plc_get_switch_position() == PLC_SWITCH_RUN)
            strncpy(response, "SWITCH:RUN\n", response_size);
        else
            strncpy(response, "SWITCH:STOP\n", response_size);
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
