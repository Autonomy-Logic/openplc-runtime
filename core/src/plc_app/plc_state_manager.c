#include <pthread.h>
#include <stdatomic.h>

#include "../drivers/plugin_driver.h"
#include "image_tables.h"
#include "plc_state_manager.h"
#include "scan_cycle_manager.h"
#include "utils/log.h"
#include "utils/utils.h"

static PLCState plc_state          = PLC_STATE_STOPPED;
static pthread_mutex_t state_mutex = PTHREAD_MUTEX_INITIALIZER;

struct timespec timer_start;
pthread_t plc_thread;
PluginManager *plc_program = NULL;

extern plc_timing_stats_t plc_timing_stats;
extern atomic_long plc_heartbeat;
extern plugin_driver_t *plugin_driver;

void *plc_cycle_thread(void *arg)
{
    PluginManager *pm = (PluginManager *)arg;

    // Initialize PLC
    set_realtime_priority();
    symbols_init(pm);
    ext_config_init__();
    ext_glueVars();

    log_info("Starting main loop");

    pthread_mutex_lock(&state_mutex);
    plc_state = PLC_STATE_RUNNING;
    pthread_mutex_unlock(&state_mutex);
    log_info("PLC State: RUNNING");

    plc_timing_stats.scan_count = 0;

    // Get the start time for the running program
    clock_gettime(CLOCK_MONOTONIC, &timer_start);

    while (plc_state == PLC_STATE_RUNNING)
    {
        scan_cycle_time_start();
        plugin_mutex_take(&plugin_driver->buffer_mutex);

        // Execute the PLC cycle
        ext_config_run__(tick__++);
        ext_updateTime();

        // Update Watchdog Heartbeat
        atomic_store(&plc_heartbeat, time(NULL));

        plugin_mutex_give(&plugin_driver->buffer_mutex);
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
    if (pm == NULL)
    {
        log_error("Failed to load PLC Program: PluginManager is NULL");

        pthread_mutex_lock(&state_mutex);
        plc_state = PLC_STATE_ERROR;
        pthread_mutex_unlock(&state_mutex);
        log_info("PLC State: ERROR");

        return -1;
    }

    if (plugin_manager_load(pm))
    {
        log_info("Loading PLC application");

        pthread_mutex_lock(&state_mutex);
        plc_state = PLC_STATE_INIT;
        pthread_mutex_unlock(&state_mutex);
        log_info("PLC State: INIT");

        if (pthread_create(&plc_thread, NULL, plc_cycle_thread, pm) != 0)
        {
            log_error("Failed to create PLC cycle thread");

            pthread_mutex_lock(&state_mutex);
            plc_state = PLC_STATE_ERROR;
            pthread_mutex_unlock(&state_mutex);
            log_info("PLC State: ERROR");

            return -1;
        }
        return 0;
    }
    else
    {
        log_error("Failed to load PLC application");

        pthread_mutex_lock(&state_mutex);
        plc_state = PLC_STATE_EMPTY;
        pthread_mutex_unlock(&state_mutex);
        log_info("PLC State: EMPTY");

        return -1;
    }
}

int unload_plc_program(PluginManager *pm)
{
    if (pm && pm == plc_program)
    {
        // Signal the PLC thread to stop
        pthread_mutex_lock(&state_mutex);
        plc_state = PLC_STATE_STOPPED;
        pthread_mutex_unlock(&state_mutex);

        // Wait for the PLC thread to finish
        pthread_join(plc_thread, NULL);

        // Destroy the plugin manager
        plugin_manager_destroy(pm);
        plc_program = NULL;

        log_info("PLC program unloaded successfully");

        log_info("PLC State: STOPPED");
        return 0;
    }
    else
    {
        log_error("No PLC program loaded or mismatched plugin manager");
        return -1;
    }
}

int plc_state_manager_init(void)
{
    pthread_mutex_lock(&state_mutex);
    plc_state = PLC_STATE_STOPPED;

    plc_program = plugin_manager_create(libplc_file);
    if (plc_program == NULL)
    {
        log_error("Failed to create PluginManager");
        pthread_mutex_unlock(&state_mutex);
        return -1;
    }
    pthread_mutex_unlock(&state_mutex);

    return 0;
}

PLCState plc_get_state(void)
{
    PLCState state;
    pthread_mutex_lock(&state_mutex);
    state = plc_state;
    pthread_mutex_unlock(&state_mutex);
    return state;
}

bool plc_set_state(PLCState new_state)
{
    pthread_mutex_lock(&state_mutex);
    if (plc_state == new_state)
    {
        pthread_mutex_unlock(&state_mutex);
        return false;
    }
    plc_state = new_state;
    pthread_mutex_unlock(&state_mutex);

    // Handle transition to running
    if (new_state == PLC_STATE_RUNNING)
    {
        if (plc_program == NULL)
        {
            plc_program = plugin_manager_create(libplc_file);
            if (plc_program == NULL)
            {
                log_error("Failed to create PluginManager");
                return false;
            }
        }
        if (load_plc_program(plc_program) < 0)
        {
            return false;
        }
    }

    // Handle transition to stopped
    else if (new_state == PLC_STATE_STOPPED)
    {
        if (unload_plc_program(plc_program) < 0)
        {
            return false;
        }
    }

    return true;
}

void plc_state_manager_cleanup(void)
{
    if (plc_program)
    {
        unload_plc_program(plc_program);
    }
}