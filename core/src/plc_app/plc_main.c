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

#include "image_tables.h"
#include "utils/log.h"
#include "plcapp_manager.h"
#include "utils/utils.h"
#include "utils/watchdog.h"
#include "scan_cycle_manager.h"
#include "../drivers/plugin_driver.h"

extern atomic_long plc_heartbeat;
extern PLCState plc_state;
extern plc_timing_stats_t plc_timing_stats;
volatile sig_atomic_t keep_running = 1;
struct timespec timer_start;
pthread_t plc_thread;
PluginManager *plc_program = NULL;


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
        if (bool_output[0][0]) 
        {
            log_debug("bool_output[0][0]: %d", *bool_output[0][0]);
        } 
        else 
        {
            log_debug("bool_output[0][0] is NULL");
        }

        log_info("Scan Count: %lu", plc_timing_stats.scan_count);
        log_info("Scan Time - Min: %ld us, Max: %ld us, Avg: %ld us",
                 plc_timing_stats.scan_time_min,
                 plc_timing_stats.scan_time_max,
                 plc_timing_stats.scan_time_avg);
        log_info("Cycle Time - Min: %lu us, Max: %lu us, Avg: %ld us",
                 plc_timing_stats.cycle_time_min,
                 plc_timing_stats.cycle_time_max,
                 plc_timing_stats.cycle_time_avg);
        log_info("Cycle Latency - Min: %ld us, Max: %ld us, Avg: %ld us",
                 plc_timing_stats.cycle_latency_min,
                 plc_timing_stats.cycle_latency_max,
                 plc_timing_stats.cycle_latency_avg);
        log_info("Overruns: %lu", plc_timing_stats.overruns);

        // Print every 100ms
        usleep(100000);
    }
    return NULL;
}

void *plc_cycle_thread(void *arg) 
{
    PluginManager *pm = (PluginManager *)arg;

    // Initialize PLC
    set_realtime_priority();
    symbols_init(pm);
    ext_config_init__();
    ext_glueVars();

    log_info("Starting main loop");
    plc_state = PLC_STATE_RUNNING;
    log_info("PLC State: RUNNING");

    plc_timing_stats.scan_count = 0;

    // Get the start time for the running program
    clock_gettime(CLOCK_MONOTONIC, &timer_start);

    while (plc_state == PLC_STATE_RUNNING)
    {
        scan_cycle_time_start();

        // Execute the PLC cycle
        ext_config_run__(tick__++);
        ext_updateTime();

        // Update Watchdog Heartbeat
        atomic_store(&plc_heartbeat, time(NULL));

        scan_cycle_time_end();

        // Calculate next start time
        timer_start.tv_nsec += *ext_common_ticktime__;
        normalize_timespec(&timer_start);

        // Sleep until the next cycle should start
        sleep_until(&timer_start);
    }

    return NULL;
}

int load_plc_program(PluginManager *pm)
{
    if (plugin_manager_load(pm)) 
    {
        log_info("Loading PLC application");
        plc_state = PLC_STATE_INIT;
        log_info("PLC State: INIT");

        if (pthread_create(&plc_thread, NULL, plc_cycle_thread, pm) != 0) 
        {
            log_error("Failed to create PLC cycle thread");
            plc_state = PLC_STATE_ERROR;
            log_info("PLC State: ERROR");
            return -1;
        }
        return 0;
    } 
    else 
    {
        log_error("Failed to load PLC application");
        plc_state = PLC_STATE_ERROR;
        log_info("PLC State: ERROR");
        return -1;
    }
}


int main() 
{
    log_set_level(LOG_LEVEL_DEBUG);

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

    // Initialize plugin driver system
    plugin_driver_t *plugin_driver = plugin_driver_create();
    if (plugin_driver) {
        // Load plugin configuration
        if (plugin_driver_load_config(plugin_driver, "../plugins.conf") == 0) {
            // Start plugins
            plugin_driver_init(plugin_driver);
            log_info("[PLUGIN]: Plugin driver system initialized");
        } else {
            log_error("[PLUGIN]: Failed to load plugin configuration");
        }
    }

    // Load user application code
    plc_program = plugin_manager_create("./libplc.so");
    load_plc_program(plc_program);

    // Launch status printing thread
    pthread_t stats_thread;
    if (pthread_create(&stats_thread, NULL, print_stats_thread, NULL) != 0) 
    {
        log_error("Failed to create stats thread");
        return -1;
    }

    while (keep_running) 
    {
        // Handle UNIX socket here in the future
        sleep(1);
    }

    // Join threads and cleanup
    plc_state = PLC_STATE_STOPPED;
    log_info("PLC State: STOPPED");
    log_info("Shutting down...");
    pthread_join(stats_thread, NULL);
    pthread_join(plc_thread, NULL);
    plugin_manager_destroy(plc_program);
    
    // Cleanup plugin driver system
    if (plugin_driver) {
        plugin_driver_stop(plugin_driver);
        plugin_driver_destroy(plugin_driver);
    }
    
    return 0;
}
