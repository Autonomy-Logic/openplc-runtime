// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <dlfcn.h>
#include <errno.h>
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
#include "task_policy.h"
#include "unix_socket.h"
#include "utils/log.h"
#include "utils/utils.h"
#include "utils/watchdog.h"

extern PLCState plc_state;
volatile sig_atomic_t keep_running = 1;
plugin_driver_t *plugin_driver     = NULL;
extern bool print_logs;

/* Graceful shutdown for both signals that mean "stop": SIGINT from an
 * interactive Ctrl-C, and SIGTERM from a supervisor. Drops out of the main loop
 * so the program is stopped and the plugin driver torn down on the way out. */
void handle_shutdown_signal(int sig)
{
    (void)sig;
    keep_running = 0;
}

/* No-op SIGUSR1 handler. Wake mechanism is pthread_kill delivering
 * EINTR to a target thread; the body does nothing. Installed once so
 * concurrent callers cannot overwrite each other's handler. */
static void handle_sigusr1(int sig)
{
    (void)sig;
}

int main(int argc, char *argv[])
{
    bool print_debug = false;
    bool safe_mode   = false;
    bool after_fault = false;

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
        else if (strcmp(argv[i], "--fault") == 0)
        {
            after_fault = true;
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

    // Handle SIGINT and SIGTERM: RuntimeManager and systemd both send
    // SIGTERM for a graceful stop; without a handler the default
    // disposition kills the process and skips program unload.
    struct sigaction sa;
    sa.sa_handler = handle_shutdown_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    // Install the process-wide SIGUSR1 wake handler exactly once; task
    // and bus threads use pthread_kill to EINTR out of clock_nanosleep.
    struct sigaction wake_sa;
    wake_sa.sa_handler = handle_sigusr1;
    sigemptyset(&wake_sa.sa_mask);
    wake_sa.sa_flags = 0;
    sigaction(SIGUSR1, &wake_sa, NULL);

    // No need to force STOPPED here: plc_state is statically initialised to it,
    // and plc_set_state() is now the body of a claimed transition rather than a
    // setter -- calling it with nothing loaded would just log a failed unload.

    bool skip_outputs_off = false;
    if (access(PLC_WATCHDOG_FAULT_MARKER, F_OK) == 0)
    {
        char reason[512] = {0};
        FILE *marker     = fopen(PLC_WATCHDOG_FAULT_MARKER, "r");
        if (marker)
        {
            size_t n  = fread(reason, 1, sizeof(reason) - 1, marker);
            reason[n] = '\0';
            fclose(marker);
        }
        skip_outputs_off = strstr(reason, PLC_FAULT_CONTEXT_BOOT_OUTPUTS_OFF) != NULL;
        if (unlink(PLC_WATCHDOG_FAULT_MARKER) != 0)
            log_warn("Could not remove %s: %s", PLC_WATCHDOG_FAULT_MARKER, strerror(errno));
        safe_mode   = true;
        after_fault = true;
    }
    if (after_fault && !safe_mode)
    {
        log_warn("--fault is only honoured together with --safe-mode; ignoring it");
        after_fault = false;
    }

    // Initialize watchdog
    if (watchdog_init() != 0)
    {
        log_error("Failed to initialize watchdog");
        return -1;
    }

    // Initialize the plugin driver BEFORE loading the PLC program.
    // Must also run before the command socket exists: a concurrent START
    // would race load_config()/init() and could dlclose a .so a plugin
    // is still sleeping in.
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

        // Release the GIL through the driver (not PyEval_SaveThread
        // directly) so plugin_driver_destroy can restore this exact
        // thread state before Py_FinalizeEx at shutdown.
        if (Py_IsInitialized())
        {
            plugin_driver_release_gil();
            log_info("[PLUGIN]: Released Python GIL");
        }
    }

    // Before the socket exists, so no command can claim a transition underneath.
    if (safe_mode)
    {
        log_info("Runtime started in SAFE MODE - PLC program will not be loaded");
        log_info("Upload a corrected program to recover");
        if (after_fault)
        {
            log_error("Previous run ended in an unrecoverable watchdog fault");
            plc_force_error_state();
            if (skip_outputs_off)
            {
                log_error("Outputs not driven off: the previous attempt did not complete");
            }
            else if (plc_claim_transition(PLC_STATE_STOPPED))
            {
                // Bounded by the watchdog's stop budget; the context breaks a restart loop.
                watchdog_set_fault_context(PLC_FAULT_CONTEXT_BOOT_OUTPUTS_OFF);
                if (!plc_outputs_off_without_program())
                    log_error("Outputs could not be driven off after the watchdog fault");
                watchdog_set_fault_context(NULL);
                plc_publish_final_state(PLC_STATE_ERROR);
            }
        }
    }

    // Start the socket only now that the driver is fully built: every
    // socket command reaches into the driver.
    if (setup_unix_socket() != 0)
    {
        log_error("Failed to set up UNIX socket");
        return -1;
    }

    // Auto-start (skipped in safe mode). The switch gate below reads
    // the default (RUN) when no plugin has reported yet — a VPP that
    // owns the switch is initialised DURING the start transition, so
    // a device in STOP will start and movement-reconciliation stops it.
    if (!safe_mode && !plc_switch_allows_run())
    {
        log_info("Hardware mode switch is in STOP - PLC left stopped");
        log_info("Move the switch to RUN to start the PLC");
    }
    else if (!safe_mode && !plc_begin_transition(PLC_STATE_RUNNING))
    {
        log_error("Failed to initiate PLC start");
    }

    while (keep_running)
    {
        // Sleep forever in the main thread
        sleep(1);
    }

    log_info("Shutting down...");

    // Order matters: program (which calls plugin_driver_stop) must
    // tear down BEFORE the driver is destroyed.
    plc_state_manager_cleanup();

    if (plugin_driver)
    {
        plugin_driver_destroy(plugin_driver);
        plugin_driver = NULL;
    }
    return 0;
}
