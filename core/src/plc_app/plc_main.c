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
extern plc_timing_stats_t plc_timing_stats;
plugin_driver_t *plugin_driver = NULL;
extern bool print_logs;

void handle_sigint(int sig)
{
    (void)sig;
    keep_running = 0;
}

void *print_stats_thread(void *arg)
{
    (void)arg;
    while (keep_running)
    {
        /*
        if (bool_output[0][0])
        {
            log_debug("bool_output[0][0]: %d", *bool_output[0][0]);
        }
        else
        {
            log_debug("bool_output[0][0] is NULL");
        }
        */

        log_info("Scan Count: %lu", plc_timing_stats.scan_count);
        log_info("Scan Time - Min: %ld us, Max: %ld us, Avg: %ld us",
                 plc_timing_stats.scan_time_min, plc_timing_stats.scan_time_max,
                 plc_timing_stats.scan_time_avg);
        log_info("Cycle Time - Min: %lu us, Max: %lu us, Avg: %ld us",
                 plc_timing_stats.cycle_time_min, plc_timing_stats.cycle_time_max,
                 plc_timing_stats.cycle_time_avg);
        log_info("Cycle Latency - Min: %ld us, Max: %ld us, Avg: %ld us",
                 plc_timing_stats.cycle_latency_min, plc_timing_stats.cycle_latency_max,
                 plc_timing_stats.cycle_latency_avg);
        log_info("Overruns: %lu", plc_timing_stats.overruns);

        // Print every 5 seconds
        sleep(5);
    }
    return NULL;
}

int main(int argc, char *argv[])
{
    // Check for --print-logs argument
    for (int i = 1; i < argc; i++)
    {
        if (strcmp(argv[i], "--print-logs") == 0)
        {
            print_logs = true;
            break;
        }
    }

    // Initialize logging system
    log_set_level(LOG_LEVEL_DEBUG);

    if (log_init(LOG_SOCKET_PATH) < 0)
    {
        fprintf(stderr, "Failed to initialize logging system\n");
        return -1;
    }

    // Handle SIGINT for graceful shutdown
    struct sigaction sa;
    sa.sa_handler = handle_sigint;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT, &sa, NULL);

    // Initialize watchdog
    if (watchdog_init() != 0)
    {
        log_error("Failed to initialize watchdog");
        return -1;
    }

    // Start PLC state manager
    if (plc_state_manager_init() != 0)
    {
        log_error("Failed to initialize PLC state manager");
        return -1;
    }

    // Start UNIX socket server
    if (setup_unix_socket() != 0)
    {
        log_error("Failed to set up UNIX socket");
        return -1;
    }

    // Launch status printing thread
    pthread_t stats_thread;
    if (pthread_create(&stats_thread, NULL, print_stats_thread, NULL) != 0)
    {
        log_error("Failed to create stats thread");
        return -1;
    }

    // Start PLC
    if (plc_set_state(PLC_STATE_RUNNING) != true)
    {
        log_error("Failed to set PLC state to RUNNING");
    }

    // Initialize plugin driver system
    plugin_driver = plugin_driver_create();
    if (plugin_driver)
    {
        log_info("[PLUGIN]: Plugin driver system created");
        // Load plugin configuration
        if (plugin_driver_load_config(plugin_driver, "./plugins.conf") == 0)
        {
            // Start plugins
            plugin_driver_init(plugin_driver);
            plugin_driver_start(plugin_driver);
            log_info("[PLUGIN]: Plugin driver system initialized");
        }
        else
        {
            log_error("[PLUGIN]: Failed to load plugin configuration");
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
    pthread_join(stats_thread, NULL);
    return 0;
}
