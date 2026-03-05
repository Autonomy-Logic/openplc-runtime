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
#include "plcapp_manager.h"
#include "scan_cycle_manager.h"
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

    // Make sure PLC starts in STOP state
    plc_set_state(PLC_STATE_STOPPED);

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

    // Start PLC (skip in safe mode to allow program upload without loading the
    // faulty program that may have caused repeated crashes)
    if (safe_mode)
    {
        log_info("Runtime started in SAFE MODE - PLC program will not be loaded");
        log_info("Upload a corrected program to recover");
    }
    else if (plc_set_state(PLC_STATE_RUNNING) != true)
    {
        log_error("Failed to set PLC state to RUNNING");
    }

    // Initialize plugin driver system.
    // Only load config and symbols here — plugins are started later when
    // a PLC program is loaded (in load_plc_program). Starting plugins at
    // boot would cause errors because buffer pointers are NULL until
    // a PLC program populates them.
    plugin_driver = plugin_driver_create();
    if (plugin_driver)
    {
        log_info("[PLUGIN]: Plugin driver system created");
        if (plugin_driver_load_config(plugin_driver, "./plugins.conf") == 0)
        {
            log_info("[PLUGIN]: Plugin driver configuration loaded");
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
            log_info("[PLUGIN]: Released Python GIL after config load");
        }
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
