#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <dlfcn.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "../drivers/plugin_driver.h"
#include "image_tables.h"
#include "plc_state_manager.h"
#include "plc_switch.h"
#include "plcapp_manager.h"
#include "unix_socket.h"
#include "utils/log.h"
#include "utils/utils.h"
#include "utils/watchdog.h"

extern PLCState plc_state;
volatile sig_atomic_t keep_running = 1;
plugin_driver_t *plugin_driver     = NULL;
extern bool print_logs;

void handle_sigint(int sig)
{
    (void)sig;
    keep_running = 0;
}

/* Process-wide no-op SIGUSR1 handler. The wake mechanism is EINTR
 * delivery to a specific thread via pthread_kill(target, SIGUSR1) — the
 * handler body itself does nothing. Installed exactly once at startup
 * (instead of being re-installed by every thread that wants to be
 * woken) so that handlers can never clobber each other. */
static void handle_sigusr1(int sig)
{
    (void)sig;
}

int main(int argc, char *argv[])
{
    bool print_debug = false;
    bool safe_mode   = false;

    // Check for command line arguments
    for (int i = 1; i < argc; i++)
    {
        if (strcmp(argv[i], "--print-logs") == 0)
        {
            print_logs = true;
        }
        else if (strcmp(argv[i], "--print-debug") == 0)
        {
            print_debug = true;
        }
        else if (strcmp(argv[i], "--safe-mode") == 0)
        {
            safe_mode = true;
        }
    }

    // Initialize logging system
    // Only enable debug level logging if --print-debug flag is passed
    if (print_debug)
    {
        log_set_level(LOG_LEVEL_DEBUG);
    }

    if (log_init(LOG_SOCKET_PATH) < 0)
    {
        time_t now = time(NULL);
        struct tm t;
        gmtime_r(&now, &t);
        char time_buf[20];
        strftime(time_buf, sizeof(time_buf), "%Y-%m-%d %H:%M:%S", &t);
        fprintf(stderr, "[%s] [ERROR] Failed to initialize logging system\n", time_buf);
        return -1;
    }

    // Handle SIGINT for graceful shutdown
    struct sigaction sa;
    sa.sa_handler = handle_sigint;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT, &sa, NULL);

    // Install the process-wide SIGUSR1 wake handler exactly once. Task
    // threads (plc_state_manager.cpp) and EtherCAT bus threads
    // (ethercat_plugin.c) both rely on EINTR-from-pthread_kill to break
    // out of clock_nanosleep on stop. Previously each of those callers
    // re-installed sigaction(SIGUSR1, …) on its own — last writer wins,
    // and any future divergence between handlers (e.g. one logs, the
    // other resets state) would silently lose half the time depending
    // on thread spawn order. One install, here, eliminates the race.
    struct sigaction wake_sa;
    wake_sa.sa_handler = handle_sigusr1;
    sigemptyset(&wake_sa.sa_mask);
    wake_sa.sa_flags = 0;
    sigaction(SIGUSR1, &wake_sa, NULL);

    // No need to force STOPPED here: plc_state is statically initialised to it,
    // and plc_set_state() is now the body of a claimed transition rather than a
    // setter -- calling it with nothing loaded would just log a failed unload.

    // Initialize watchdog
    if (watchdog_init() != 0)
    {
        log_error("Failed to initialize watchdog");
        return -1;
    }

    // Start UNIX socket server
    if (setup_unix_socket() != 0)
    {
        log_error("Failed to set up UNIX socket");
        return -1;
    }

    // Initialize plugin driver system BEFORE loading the PLC program.
    // plc_set_state(RUNNING) triggers load_plc_program() which uses the plugin
    // driver to update config and re-init plugins, and plc_cycle_thread() calls
    // plugin_driver_start() after image tables are populated.
    plugin_driver = plugin_driver_create();
    if (plugin_driver)
    {
        // Make plugin driver available to unix socket for PLUGIN_CMD routing
        unix_socket_set_plugin_driver(plugin_driver);
        log_info("[PLUGIN]: Plugin driver system created");
        if (plugin_driver_load_config(plugin_driver, "./plugins.conf") == 0)
        {
            plugin_driver_init(plugin_driver);
            log_info("[PLUGIN]: All plugins initialized (not started)");
        }
        else
        {
            log_error("[PLUGIN]: Failed to load plugin configuration");
        }

        // Release the Python GIL if Python was initialized during plugin loading.
        // This prevents a deadlock where the main thread holds the GIL forever
        // while sleeping, blocking other threads (like the unix socket thread)
        // from using Python when handling commands like START.
        if (Py_IsInitialized())
        {
            PyEval_SaveThread();
            log_info("[PLUGIN]: Released Python GIL");
        }
    }

    // Start PLC (skip in safe mode to allow program upload without loading the
    // faulty program that may have caused repeated crashes).
    //
    // Use plc_begin_transition() rather than plc_set_state() directly so the
    // auto-start is arbitrated by plc_claim_transition() like any
    // socket-originated START command. This prevents a race where the socket
    // listener (already running above) accepts a START before the auto-start
    // finishes, causing two concurrent load_plc_program() calls — and two
    // dispatcher threads. plc_begin_transition() also makes the start
    // asynchronous, which is fine: the main thread just sleeps below.
    if (safe_mode)
    {
        log_info("Runtime started in SAFE MODE - PLC program will not be loaded");
        log_info("Upload a corrected program to recover");
    }
    // Same gate as any other start, but note what it can and cannot see. A VPP
    // plugin that owns a physical mode switch is initialised as part of loading
    // the program — inside the start transition below — so at this point the
    // switch has usually NOT been reported yet and the gate reads the default
    // (RUN). A device powered up with the switch in STOP therefore does start,
    // and is then corrected: the plugin reports STOP during init, that request is
    // dropped because a transition is in flight, and the switch-movement
    // reconciliation stops the PLC as soon as the start lands. Safe, but the gate
    // only bites here for a switch position already known at this point (e.g. one
    // reported by a plugin the runtime loaded independently of the program).
    else if (!plc_switch_allows_run())
    {
        log_info("Hardware mode switch is in STOP - PLC left stopped");
        log_info("Move the switch to RUN to start the PLC");
    }
    else if (!plc_begin_transition(PLC_STATE_RUNNING))
    {
        log_error("Failed to initiate PLC start");
    }

    while (keep_running)
    {
        // Sleep forever in the main thread
        sleep(1);
    }

    // Cleanup plugin driver system
    if (plugin_driver)
    {
        plugin_driver_destroy(plugin_driver);
    }

    // Cleanup
    log_info("Shutting down...");
    plc_state_manager_cleanup();
    return 0;
}
