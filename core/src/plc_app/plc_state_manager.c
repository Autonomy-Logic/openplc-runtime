#include <pthread.h>
#include <setjmp.h>
#include <signal.h>
#include <stdatomic.h>
#include <string.h>

#include "../drivers/plugin_driver.h"
#include "image_tables.h"
#include "journal_buffer.h"
#include "plc_state_manager.h"
#include "plcapp_manager.h"
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

// Signal recovery for PLC cycle thread crashes (SIGFPE, SIGSEGV)
static sigjmp_buf plc_crash_jmp;
static pthread_t plc_thread_id;
static volatile sig_atomic_t plc_crash_signal = 0;
static volatile sig_atomic_t holding_buffer_mutex = 0;

// NOTE on siglongjmp safety: siglongjmp from a hardware-raised signal is
// well-defined when process state (heap/stack) is intact at the time of the
// signal. For generated PLC code this is the expected case because:
//   - SIGFPE: the faulting instruction is trapped before completing, no
//     memory is modified, so recovery is always safe.
//   - SIGSEGV: typically caused by out-of-bounds access into an unmapped
//     page. The write/read is trapped by the MMU before completing, so
//     process state is clean.
// The theoretical risk is a SIGSEGV caused by a prior silent corruption
// (write to a valid but wrong address) leaving heap/stack inconsistent.
// This is extremely unlikely with generated PLC code (no heap allocation,
// no recursion). If it does happen, the recovered process will crash again
// shortly and Layer 3 (webserver safe mode) will catch the rapid crash
// pattern and restart without loading the faulty program.
static void plc_crash_handler(int sig)
{
    // Only handle if the crash came from the PLC cycle thread
    if (!pthread_equal(pthread_self(), plc_thread_id))
    {
        // Not our thread - restore default handler and re-raise
        signal(sig, SIG_DFL);
        raise(sig);
        return;
    }

    plc_crash_signal = sig;
    siglongjmp(plc_crash_jmp, sig);
}

void *plc_cycle_thread(void *arg)
{
    PluginManager *pm = (PluginManager *)arg;

    // Record this thread's ID for the crash handler
    plc_thread_id = pthread_self();
    plc_crash_signal = 0;

    // Initialize scan cycle manager (priority-inheriting stats mutex)
    if (scan_cycle_manager_init() != 0)
    {
        log_error("Failed to initialize scan cycle manager");
    }

    // mlockall is process-wide; safe to call before plugin init.
    lock_memory();

    symbols_init(pm);
    ext_config_init__();
    ext_glueVars();

    // Fill NULL pointers in image tables with temporary buffers
    // This ensures plugins can access addresses not used by the PLC program
    plugin_mutex_take(&plugin_driver->buffer_mutex);
    image_tables_fill_null_pointers();
    plugin_mutex_give(&plugin_driver->buffer_mutex);

    // Initialize journal buffer for race-condition-free plugin writes
    journal_buffer_ptrs_t journal_ptrs = {
        .bool_input = bool_input,
        .bool_output = bool_output,
        .bool_memory = bool_memory,
        .byte_input = byte_input,
        .byte_output = byte_output,
        .int_input = int_input,
        .int_output = int_output,
        .int_memory = int_memory,
        .dint_input = dint_input,
        .dint_output = dint_output,
        .dint_memory = dint_memory,
        .lint_input = lint_input,
        .lint_output = lint_output,
        .lint_memory = lint_memory,
        .buffer_size = BUFFER_SIZE,
        .image_mutex = &plugin_driver->buffer_mutex
    };
    if (journal_init(&journal_ptrs) != 0) {
        log_error("Failed to initialize journal buffer");
    } else {
        log_info("Journal buffer initialized");
    }

    // Start enabled plugins now that image tables are populated.
    // This is the earliest safe point: ext_glueVars() + image_tables_fill_null_pointers()
    // have run, so plugins will not encounter NULL buffer pointers.
    if (plugin_driver)
    {
        plugin_driver_start(plugin_driver);
        log_info("[PLUGIN]: Enabled plugins started");
    }

    // Elevate AFTER plugins start: pthread default is PTHREAD_INHERIT_SCHED,
    // so any thread spawned during plugin_driver_start would otherwise
    // inherit FIFO and contend with the scan on its core.
    set_realtime_priority();

    // Install signal handlers for crash recovery BEFORE entering the main loop.
    // This allows SIGFPE (e.g. division by zero) and SIGSEGV (e.g. bad array
    // access) in the user's PLC program to be caught and recovered from,
    // instead of killing the entire runtime process.
    struct sigaction crash_sa;
    memset(&crash_sa, 0, sizeof(crash_sa));
    crash_sa.sa_handler = plc_crash_handler;
    sigemptyset(&crash_sa.sa_mask);
    // SA_NODEFER: allow the handler to catch the same signal again after
    // siglongjmp returns (needed because the signal is still blocked otherwise)
    crash_sa.sa_flags = SA_NODEFER;
    sigaction(SIGFPE, &crash_sa, NULL);
    sigaction(SIGSEGV, &crash_sa, NULL);

    log_info("Starting main loop");

    pthread_mutex_lock(&state_mutex);
    plc_state = PLC_STATE_RUNNING;
    pthread_mutex_unlock(&state_mutex);
    log_info("PLC State: RUNNING");

    plc_timing_stats.scan_count = 0;

    // Get the start time for the running program
    clock_gettime(CLOCK_MONOTONIC, &timer_start);

    // Set up the crash recovery point. sigsetjmp returns 0 on initial call,
    // and returns the signal number when siglongjmp jumps back here after
    // a crash in the PLC program.
    int crash_sig = sigsetjmp(plc_crash_jmp, 1);
    if (crash_sig != 0)
    {
        // We got here via siglongjmp from the crash handler.
        // Only release the buffer mutex if we held it when we crashed.
        if (holding_buffer_mutex)
        {
            holding_buffer_mutex = 0;
            plugin_mutex_give(&plugin_driver->buffer_mutex);
        }

        const char *sig_name = (crash_sig == SIGFPE) ? "SIGFPE (arithmetic error, e.g. division by zero)"
                                                      : "SIGSEGV (memory access violation)";
        log_error("PLC program crashed with signal %d: %s", crash_sig, sig_name);
        log_error("The loaded PLC program contains a fatal error. "
                  "Upload a corrected program to recover.");

        // Restore default handlers so crashes outside the PLC thread
        // still terminate the process as expected
        signal(SIGFPE, SIG_DFL);
        signal(SIGSEGV, SIG_DFL);

        pthread_mutex_lock(&state_mutex);
        plc_state = PLC_STATE_ERROR;
        pthread_mutex_unlock(&state_mutex);
        log_info("PLC State: ERROR");

        return NULL;
    }

    while (plc_state == PLC_STATE_RUNNING)
    {
        scan_cycle_time_start();
        holding_buffer_mutex = 1;
        plugin_mutex_take(&plugin_driver->buffer_mutex);

        // Apply pending journal entries before plugin hooks run
        // This ensures all plugin writes from the previous cycle are visible
        journal_apply_and_clear();

        // Call cycle_start for all active native plugins that registered the hook
        plugin_driver_cycle_start(plugin_driver);

        // Execute the PLC cycle
        ext_config_run__(tick__++);
        ext_updateTime();

        // Call cycle_end for all active native plugins that registered the hook
        plugin_driver_cycle_end(plugin_driver);

        // Update Watchdog Heartbeat
        atomic_store(&plc_heartbeat, time(NULL));

        plugin_mutex_give(&plugin_driver->buffer_mutex);
        holding_buffer_mutex = 0;
        scan_cycle_time_end();

        // Calculate next start time
        timer_start.tv_nsec += *ext_common_ticktime__;
        normalize_timespec(&timer_start);

        // Sleep until the next cycle should start
        sleep_until(&timer_start);
    }

    // Restore default signal handlers when exiting normally
    signal(SIGFPE, SIG_DFL);
    signal(SIGSEGV, SIG_DFL);

    scan_cycle_manager_cleanup();

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

        // Re-initialize plugins with updated config (e.g. after program re-upload).
        // Do NOT start plugins here -- they are started later in plc_cycle_thread()
        // after image tables are populated, ensuring plugins never see NULL buffers.
        if (plugin_driver)
        {
            if (plugin_driver_update_config(plugin_driver, "./plugins.conf") == 0)
            {
                plugin_driver_init(plugin_driver);
                log_info("[PLUGIN]: Plugins re-initialized with updated config");
            }
            else
            {
                log_error("[PLUGIN]: Failed to load plugin configuration");
            }
        }

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
        // Check if we are coming from ERROR state (crash recovery).
        // In that case, the PLC thread has already exited via the signal
        // handler, so we only need to join it without changing state first.
        PLCState prev_state = plc_get_state();

        if (prev_state != PLC_STATE_ERROR)
        {
            // Normal shutdown: signal the PLC thread to stop
            pthread_mutex_lock(&state_mutex);
            plc_state = PLC_STATE_STOPPED;
            pthread_mutex_unlock(&state_mutex);
        }

        // Wait for the PLC thread to finish
        pthread_join(plc_thread, NULL);

        // Cleanup journal buffer before clearing image tables
        journal_cleanup();
        log_info("Journal buffer cleaned up");

        // Stop plugins FIRST (before acquiring mutex) to prevent deadlock
        // The S7Comm plugin's RWArea callback acquires buffer_mutex during
        // client read operations. If we try to acquire the mutex before
        // stopping the plugin, we can deadlock if a client is connected.
        plugin_driver_stop(plugin_driver);

        // Clear temporary pointers from image tables before unloading
        // This ensures clean state for the next program load
        plugin_mutex_take(&plugin_driver->buffer_mutex);
        image_tables_clear_null_pointers();
        plugin_mutex_give(&plugin_driver->buffer_mutex);

        // Cleanup Python function blocks BEFORE unloading the shared library
        // This terminates Python subprocesses and joins runner threads to prevent
        // crash when dlclose() unmaps the code while threads are still running
        void (*python_cleanup)(void);
        *(void **)(&python_cleanup) =
            plugin_manager_get_symbol(pm, "python_blocks_cleanup");
        if (python_cleanup)
        {
            python_cleanup();
        }

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
            char *libplc_path = find_libplc_file(libplc_build_dir);
            if (libplc_path == NULL)
            {
                log_error("Failed to find libplc file");
                pthread_mutex_lock(&state_mutex);
                plc_state = PLC_STATE_EMPTY;
                pthread_mutex_unlock(&state_mutex);
                return false;
            }

            plc_program = plugin_manager_create(libplc_path);
            free(libplc_path);

            if (plc_program == NULL)
            {
                log_error("Failed to create PluginManager");
                pthread_mutex_lock(&state_mutex);
                plc_state = PLC_STATE_EMPTY;
                pthread_mutex_unlock(&state_mutex);
                return false;
            }
        }
        if (load_plc_program(plc_program) < 0)
        {
            pthread_mutex_lock(&state_mutex);
            plc_state = PLC_STATE_ERROR;
            pthread_mutex_unlock(&state_mutex);
            return false;
        }
    }

    // Handle transition to stopped
    else if (new_state == PLC_STATE_STOPPED)
    {
        if (plc_program)
        {
            if (unload_plc_program(plc_program) < 0)
            {
                return false;
            }
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

void plc_force_error_state(void)
{
    pthread_mutex_lock(&state_mutex);
    plc_state = PLC_STATE_ERROR;
    pthread_mutex_unlock(&state_mutex);
    log_info("PLC State: ERROR");
}

int plc_get_crash_signal(void)
{
    return (int)plc_crash_signal;
}
