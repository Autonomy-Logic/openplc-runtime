#include <errno.h>
#include <pthread.h>
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
#include "plc_retain.h"
#include "plc_state_manager.h"
#include "plc_switch.h"
#include "scan_cycle_manager.h"
#include "unix_socket.h"
#include "utils/log.h"
#include "utils/utils.h"

extern volatile sig_atomic_t keep_running;

static plugin_driver_t *g_plugin_driver = NULL;

/* How long run_transition waits to observe the landing before reconciling with
 * the mode switch, and how often it looks. The bound comes from
 * plc_state_manager.h so that the watchdog's stuck-transition bound is derived
 * from the same number and can only fire strictly later. */
#define LANDING_WAIT_MS PLC_TRANSITION_LANDING_TIMEOUT_MS
#define LANDING_POLL_MS 20

void unix_socket_set_plugin_driver(void *driver)
{
    g_plugin_driver = (plugin_driver_t *)driver;
}

// Body of a claimed transition: perform it, wait for it to land, then reconcile
// with the mode switch. Normally runs on the detached worker spawned below, but
// is called directly when that worker cannot be spawned -- the transition has
// already been claimed by then, and it has to be completed by somebody.
static bool run_transition(PLCState target)
{
    bool result = plc_set_state(target);
    if (!result)
    {
        log_error("State transition to %s failed",
                  target == PLC_STATE_RUNNING ? "RUNNING" : "STOPPED");
    }

    // Wait for the landing before reconciling.
    //
    // plc_set_state(RUNNING) returns as soon as load_plc_program() has spawned
    // the PLC thread; that thread publishes RUNNING later, once the workers exist
    // (measured at ~4 s on an SLM-RP4, most of it plugin bring-up). Reconciling
    // straight after plc_set_state() therefore ran while the state was still
    // TRANSITIONING_TO_RUN and threw the switch movement away without acting on
    // it -- losing precisely the flip-during-a-start this exists to catch.
    //
    // Polling rather than a condvar handshake: the state IS the interlock, so
    // nothing else can begin a transition while we wait, and there is no lock
    // held here. The bound only exists so a transition that never lands cannot
    // strand this thread -- the watchdog is what turns that into a reported
    // fault.
    for (int waited_ms = 0; plc_state_is_transitioning() && waited_ms < LANDING_WAIT_MS;
         waited_ms += LANDING_POLL_MS)
    {
        struct timespec poll = { .tv_sec = 0, .tv_nsec = LANDING_POLL_MS * 1000000L };
        nanosleep(&poll, NULL);
    }

    // Reconcile with the mode switch.
    //
    // Requests are DROPPED while a transition is in flight, which on its own
    // loses the switch's intent: flip to STOP during a start and the stop
    // vanishes, leaving the PLC running with the switch in STOP and nobody
    // retrying. Rather than queueing requests, the runtime remembers only
    // whether the switch MOVED (plc_switch, so this works for every platform's
    // plugin) and compares the position it came to rest at against the state we
    // actually landed on. Several flips during one transition collapse to the
    // final position, which is the only one that matters.
    //
    // Gated on movement, deliberately: an editor stop with the switch untouched
    // records no movement, so nothing reconciles it away. Comparing position to
    // state unconditionally would make Stop impossible whenever the switch sits
    // in RUN. It also cannot ping-pong — each pass consumes the movement, and
    // only the switch physically moving sets it again.
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

            // The movement record was consumed above, so a refusal here would
            // throw the switch's intent away for good: the state is already
            // final, which leaves the socket thread free to claim a transition in
            // the gap, and the spawn paths below can fail too. Put the record
            // back so the next landing reconciles instead. This cannot ping-pong
            // -- the retry compares position against state again, and a landing
            // that agrees with the switch just consumes the record.
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

// Start a background thread that performs the (potentially slow) state
// transition. Returns false when the request was refused; otherwise the
// transition is under way (or, if the worker could not be spawned, has already
// been completed on this thread -- see below).
//
// The single authoritative entry point for every state change: socket
// START/STOP, plugin-initiated requests from a mode switch, and the boot
// auto-start in plc_main.c. All of them come here rather than calling
// plc_set_state() directly, so one arbiter decides what may begin.
//
// That arbiter is plc_claim_transition(), which under the state lock refuses a
// request while the state is TRANSITIONING_TO_RUN or TRANSITIONING_TO_STOP, and
// refuses a request for the state the runtime is already in. There is no second
// flag to keep in step with plc_state -- the state IS the interlock, which is
// what makes "you cannot change state while changing state" true by
// construction rather than by two variables agreeing.
//
// Requests refused here are dropped, not queued. The switch's intent survives
// via the movement reconciliation in transition_worker above.
bool plc_begin_transition(PLCState target)
{
    if (!plc_claim_transition(target))
    {
        return false;
    }

    // Claimed but the worker cannot be spawned: run the transition on this thread
    // rather than publishing a landing.
    //
    // Publishing STOPPED here used to look like the safe way out, and it is the
    // opposite. The claim has already published TRANSITIONING_TO_STOP, which is
    // what makes the dispatcher and workers leave their loops -- so on a stop from
    // RUNNING the scan really does end, but unload_plc_program never runs:
    // journal_cleanup, plugin_driver_stop, plugin_manager_destroy and the dlclose
    // are all skipped, plc_program stays non-NULL, plc_thread is never joined, and
    // STATUS reports a stop that tore nothing down. The next start then re-enters
    // plugin_driver_init on live plugin state and re-runs a program whose statics
    // were never reinitialised. (For a start from EMPTY it also reported "no
    // program" as "stopped".)
    //
    // Completing it here blocks this caller for the duration -- the socket is
    // single-client, so the editor waits -- which on a thread-or-memory exhaustion
    // path is the cheaper of the two costs by a wide margin.
    PLCState *arg = malloc(sizeof(PLCState));
    if (!arg)
    {
        log_error("Failed to allocate transition argument — completing the "
                  "transition on the calling thread");
        return run_transition(target);
    }
    *arg = target;

    pthread_t tid;
    if (pthread_create(&tid, NULL, transition_worker, arg) != 0)
    {
        log_error("Failed to create transition thread (%s) — completing the "
                  "transition on the calling thread", strerror(errno));
        free(arg);
        return run_transition(target);
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
    // While a state transition is in progress, only allow the reads: you cannot
    // change state while it is changing, and everything else gets COMMAND:BUSY.
    //
    // SWITCH belongs here with PING and STATUS. It is a plain atomic load of
    // plc_switch with no coupling to plc_state, so there is nothing mid-change for
    // it to expose -- and answering BUSY meant the webserver dropped
    // switchPosition from every status response for the whole duration of a start
    // or stop (parse_switch_position returns None) and GET /switch reported
    // "unknown". An editor that decides whether a start is allowed from that field
    // lost it precisely while polling through the transition it had just asked for.
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
    else if (strcmp(command, "RETAIN:CLEAR") == 0)
    {
        /* Discard stored retained values, so the next start uses the declared
         * initialisers. The webserver calls this on program upload — CODESYS
         * clears retained memory on download, and a new program's values have
         * no business surviving into it.
         *
         * Answers OK even with no retain plugin: "discard what is stored" is
         * satisfied by a device that stores nothing, and failing there would
         * make every upload to such a device look broken. */
        plc_retain_clear();
        strncpy(response, "RETAIN:OK\n", response_size);
    }
    else if (strcmp(command, "RETAIN:STATUS") == 0)
    {
        /* What is ACTUALLY holding the retained bytes right now, which is not
         * the same question as what the settings say. A VPP plugin overrides
         * the built-in file store, and the Persistent Storage screen has to be
         * able to say so — otherwise it reports the file store as enabled
         * while a plugin quietly does the work, and the operator finds out by
         * wondering why the file never grows. */
        snprintf(response, response_size, "RETAIN:STATUS %s %s %s\n",
                 plc_retain_active() ? "active" : "inactive",
                 plc_retain_backend(),
                 plc_retain_backend_detail());
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
