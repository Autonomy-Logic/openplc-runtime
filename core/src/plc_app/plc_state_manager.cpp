// plc_state_manager.cpp
//
// Walks the loaded program's ConfigurationInstance via virtual dispatch
// (Phase 5), spawns one SCHED_FIFO pthread per IEC TASK (Phase 6), and
// anchors the per-cycle housekeeping window on the fastest task's
// thread (Phase 7).
//
// Linux-only (the runtime targets Linux).

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <atomic>
#include <cerrno>
#include <csetjmp>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <exception>

#include <pthread.h>
#include <sched.h>

extern "C" {
#include "../drivers/plugin_driver.h"
}

// Runtime-side strucpp ABI mirror — see core/src/lib/strucpp_abi.hpp
#include "../lib/strucpp_abi.hpp"

#include "image_tables.h"
#include "journal_buffer.h"
#include "plc_io_cycle.h"
#include "plc_state_manager.h"
#include "plcapp_manager.h"
#include "scan_cycle_manager.h"
#include "utils/log.h"
#include "utils/utils.h"

static PLCState         plc_state    = PLC_STATE_STOPPED;
static pthread_mutex_t  state_mutex  = PTHREAD_MUTEX_INITIALIZER;

struct timespec  timer_start;
pthread_t        plc_thread;
PluginManager   *plc_program = NULL;

extern std::atomic<long>  plc_heartbeat;
extern plugin_driver_t   *plugin_driver;

/* -----------------------------------------------------------------------
 * Per-task storage. Allocated when a program loads, freed on stop.
 *
 * plc_tasks_lock serialises lifecycle (alloc/publish/free) against
 * readers (STATS handler in scan_cycle_manager). See plc_state_manager.h
 * for the contract. Read-only once published until the next STOP, so the
 * lock is held only briefly on the writer side and for one iteration on
 * the reader side. Not a recursive lock — callers must not nest.
 * --------------------------------------------------------------------- */
PlcTaskCtx *plc_tasks      = nullptr;
size_t      plc_task_count = 0;

static pthread_mutex_t plc_tasks_lock = PTHREAD_MUTEX_INITIALIZER;

extern "C" void plc_tasks_reader_lock(void)
{
    pthread_mutex_lock(&plc_tasks_lock);
}

extern "C" void plc_tasks_reader_unlock(void)
{
    pthread_mutex_unlock(&plc_tasks_lock);
}

/* The bootstrap thread doesn't run any IEC task body — it does setup,
 * spawns task threads, waits, and joins. We still want crash recovery
 * on it via a separate jmp pair. The active task's ctx is in __thread
 * storage so the signal handler knows which siglongjmp target to use. */
static __thread PlcTaskCtx  *current_task_ctx        = nullptr;
static sigjmp_buf            bootstrap_crash_jmp;
static volatile sig_atomic_t bootstrap_crash_sig     = 0;
static volatile sig_atomic_t bootstrap_holding_mutex = 0;
static volatile sig_atomic_t plc_crash_signal        = 0;

/* The SIGUSR1 wake handler is installed once at process init in
 * plc_main.c (handle_sigusr1). Every task thread relies on EINTR from
 * pthread_kill(target, SIGUSR1) to break out of clock_nanosleep on
 * stop — the handler body itself is a no-op. */

static void plc_crash_handler(int sig)
{
    if (current_task_ctx)
    {
        current_task_ctx->crash_sig = sig;
        plc_crash_signal            = sig;
        siglongjmp(current_task_ctx->crash_jmp, sig);
    }
    if (pthread_equal(pthread_self(), plc_thread))
    {
        bootstrap_crash_sig = sig;
        plc_crash_signal    = sig;
        siglongjmp(bootstrap_crash_jmp, sig);
    }
    /* Unknown thread — restore default and re-raise so we don't
     * silently eat fatal signals from webserver / plugin threads. */
    signal(sig, SIG_DFL);
    raise(sig);
}

/* Drop whichever runtime lock this task thread currently holds. Mirrors the
 * signal-handler recovery (the sigsetjmp block below) so a C++ exception
 * thrown mid-scan can't leave the image or global mutex locked when the thread
 * unwinds and exits. holding_global/holding_mutex are set only inside their
 * respective locked windows, so at most the matching lock is released. */
static void plc_task_release_locks(PlcTaskCtx *ctx)
{
    if (ctx->holding_global)
    {
        ctx->holding_global = 0;
        pthread_mutex_unlock(global_mutex());
    }
    if (ctx->holding_mutex)
    {
        ctx->holding_mutex = 0;
        pthread_mutex_unlock(image_tables_mutex());
    }
}

/* -----------------------------------------------------------------------
 * Per-task thread function.
 *
 * Phase 6 keeps this minimal: SCHED_FIFO priority elevation, optional
 * CPU affinity, per-thread crash recovery, then a clock_nanosleep loop
 * that runs task->programs[]->run() under the process-image protocol.
 * Phase 7 specializes the fastest task by adding housekeeping pre/post.
 * --------------------------------------------------------------------- */
static void *plc_task_thread(void *arg)
{
    PlcTaskCtx *ctx = static_cast<PlcTaskCtx *>(arg);
    current_task_ctx = ctx;

    pthread_setname_np(pthread_self(), ctx->name);

    int rt = ctx->priority;
    if (rt < 1)  rt = 1;
    if (rt > 99) rt = 99;
    sched_param sp{};
    sp.sched_priority = rt;
    if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) != 0)
    {
        log_warn("[task %s] SCHED_FIFO(%d) failed: %s — running default scheduling",
                 ctx->name, rt, strerror(errno));
    }
    else
    {
        log_info("[task %s] SCHED_FIFO priority %d", ctx->name, rt);
    }

    if (ctx->cpu_affinity_mask != 0)
    {
        // CPU affinity uses `cpu_set_t` / `CPU_ZERO` / `CPU_SETSIZE` /
        // `pthread_setaffinity_np` — all Linux extensions to `<sched.h>`,
        // not present on MSYS2/Cygwin/Windows.  Skip silently when
        // building for a non-Linux host: Windows doesn't expose
        // SCHED_FIFO either, so any "pin to CPU" guarantee would already
        // be unachievable there.
#if !defined(__CYGWIN__) && !defined(__MSYS__) && defined(__linux__)
        cpu_set_t cs;
        CPU_ZERO(&cs);
        for (int cpu = 0; cpu < 64 && cpu < CPU_SETSIZE; ++cpu)
        {
            if (ctx->cpu_affinity_mask & (1ULL << cpu)) CPU_SET(cpu, &cs);
        }
        if (pthread_setaffinity_np(pthread_self(), sizeof cs, &cs) != 0)
        {
            log_warn("[task %s] pthread_setaffinity_np failed: %s",
                     ctx->name, strerror(errno));
        }
#else
        log_info("[task %s] CPU affinity requested but unsupported on this platform",
                 ctx->name);
#endif
    }

    if (sigsetjmp(ctx->crash_jmp, 1) != 0)
    {
        if (ctx->holding_mutex)
        {
            ctx->holding_mutex = 0;
            pthread_mutex_unlock(image_tables_mutex());
        }
        if (ctx->holding_global)
        {
            ctx->holding_global = 0;
            pthread_mutex_unlock(global_mutex());
        }
        log_error("[task %s] crashed (signal %d) — entering ERROR state",
                  ctx->name, ctx->crash_sig);
        plc_force_error_state();
        return nullptr;
    }

    auto *task = static_cast<strucpp::TaskInstance *>(ctx->task_handle);

    timespec next_wakeup;
    clock_gettime(CLOCK_MONOTONIC, &next_wakeup);

    while (plc_get_state() == PLC_STATE_RUNNING)
    {
        scan_cycle_tracker_start(&ctx->tracker);

        /* Process-image model: short locked windows for I/O and global sync;
         * the body runs lock-free on the program's private storage, so task
         * bodies execute in parallel. holding_mutex/holding_global gate the
         * crash-handler unlock of whichever lock is held.
         *
         * The whole scan body runs under try/catch. On this hosted,
         * exceptions-enabled build the STruC++ runtime THROWS on an
         * unrecoverable fault (null IEC reference, array index out of bounds,
         * bad located address). If that throw escaped this pthread entry
         * function it would hit std::terminate -> abort the whole process ->
         * the daemon would bounce every task. Instead we catch it here: this
         * one task logs the fault and terminates; the other task threads keep
         * scanning. The PLC stays RUNNING (minus the faulted task) rather than
         * dying wholesale. */
        try
        {
            /* 1. Copy-in: snapshot this task's located inputs from the image
             *    into program storage. image_lock() takes the image mutex and
             *    drains the journal (flush-on-lock) so we read fresh committed
             *    values -- the same API plugins use for coherent reads. Set
             *    holding_mutex after the lock so a fatal signal between flag and
             *    lock can't unlock a mutex we don't own. */
            image_lock();
            ctx->holding_mutex = 1;
            for (size_t p = 0; p < task->program_count; ++p)
            {
                uint32_t off = 0, cnt = 0;
                task->programs[p]->located_range(&off, &cnt);
                if (cnt) image_tables_threaded_copy_in(off, cnt);
            }
            ctx->holding_mutex = 0;
            image_unlock();

            /* 2. Global sync-in: canonical globals -> private working copies. */
            pthread_mutex_lock(global_mutex());
            ctx->holding_global = 1;
            for (size_t p = 0; p < task->program_count; ++p)
                task->programs[p]->sync_in();
            ctx->holding_global = 0;
            pthread_mutex_unlock(global_mutex());

            if (ctx->is_fastest_task) plc_run_io_cycle_threaded_pre();

            /* 3. Run the bodies on private storage -- NO lock held. */
            for (size_t p = 0; p < task->program_count; ++p)
                task->programs[p]->run();

            /* 4. Global sync-out: commit changed externals back. */
            pthread_mutex_lock(global_mutex());
            ctx->holding_global = 1;
            for (size_t p = 0; p < task->program_count; ++p)
                task->programs[p]->sync_out();
            ctx->holding_global = 0;
            pthread_mutex_unlock(global_mutex());

            /* 5. Copy-out: journal this task's changed located outputs
             *    (lock-free; applied to the image on the next drain). */
            for (size_t p = 0; p < task->program_count; ++p)
            {
                uint32_t off = 0, cnt = 0;
                task->programs[p]->located_range(&off, &cnt);
                if (cnt) image_tables_threaded_copy_out(off, cnt);
            }

            if (ctx->is_fastest_task) plc_run_io_cycle_threaded_post();
        }
        catch (const std::exception &e)
        {
            plc_task_release_locks(ctx);
            log_error("[task %s] terminated by unhandled exception: %s — "
                      "other tasks keep running", ctx->name, e.what());
            return nullptr;
        }
        catch (...)
        {
            plc_task_release_locks(ctx);
            log_error("[task %s] terminated by unknown exception — "
                      "other tasks keep running", ctx->name);
            return nullptr;
        }

        scan_cycle_tracker_end(&ctx->tracker);

        ctx->heartbeat.store((long)time(nullptr), std::memory_order_relaxed);
        ctx->local_tick.fetch_add(1, std::memory_order_relaxed);

        next_wakeup.tv_nsec += (long)(ctx->interval_ns % 1000000000LL);
        next_wakeup.tv_sec  += (time_t)(ctx->interval_ns / 1000000000LL);
        if (next_wakeup.tv_nsec >= 1000000000L)
        {
            next_wakeup.tv_nsec -= 1000000000L;
            next_wakeup.tv_sec  += 1;
        }
        int rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next_wakeup, nullptr);
        if (rc == EINTR) continue;
    }

    log_info("[task %s] stopped after %llu ticks", ctx->name,
             (unsigned long long)ctx->local_tick.load());
    return nullptr;
}

void *plc_cycle_thread(void *arg)
{
    PluginManager *pm = (PluginManager *)arg;

    plc_crash_signal        = 0;
    bootstrap_crash_sig     = 0;
    bootstrap_holding_mutex = 0;

    /* Per-task trackers are initialised below, once we know the task list
     * and each task's interval. */

    lock_memory();

    if (symbols_init(pm) != 0)
    {
        pthread_mutex_lock(&state_mutex);
        plc_state = PLC_STATE_ERROR;
        pthread_mutex_unlock(&state_mutex);
        log_error("PLC State: ERROR (failed to resolve .so symbols)");
        return NULL;
    }

    /* Bind located variables to image-table slots, then fill any
     * unbound slots with private backing buffers. */
    pthread_mutex_t *itm = image_tables_mutex();
    pthread_mutex_lock(itm);
    image_tables_bind_located_vars();
    image_tables_fill_null_pointers();
    pthread_mutex_unlock(itm);

    journal_buffer_ptrs_t journal_ptrs = {
        .bool_input   = bool_input,
        .bool_output  = bool_output,
        .bool_memory  = bool_memory,
        .byte_input   = byte_input,
        .byte_output  = byte_output,
        .int_input    = int_input,
        .int_output   = int_output,
        .int_memory   = int_memory,
        .dint_input   = dint_input,
        .dint_output  = dint_output,
        .dint_memory  = dint_memory,
        .lint_input   = lint_input,
        .lint_output  = lint_output,
        .lint_memory  = lint_memory,
        .buffer_size  = BUFFER_SIZE,
        .image_mutex  = itm,
    };
    if (journal_init(&journal_ptrs) != 0)
    {
        log_error("Failed to initialize journal buffer");
    }
    else
    {
        log_info("Journal buffer initialized");
    }

    if (plugin_driver)
    {
        plugin_driver_start(plugin_driver);
        log_info("[PLUGIN]: Enabled plugins started");
    }

    set_realtime_priority();

    struct sigaction crash_sa;
    std::memset(&crash_sa, 0, sizeof(crash_sa));
    crash_sa.sa_handler = plc_crash_handler;
    sigemptyset(&crash_sa.sa_mask);
    crash_sa.sa_flags = SA_NODEFER;
    sigaction(SIGFPE,  &crash_sa, NULL);
    sigaction(SIGSEGV, &crash_sa, NULL);

    /* SIGUSR1 wake handler is installed once at process init (plc_main.c).
     * No per-thread re-installation here — the bootstrap thread inherits
     * the handler from the process. */

    log_info("Starting main loop");

    pthread_mutex_lock(&state_mutex);
    plc_state = PLC_STATE_RUNNING;
    pthread_mutex_unlock(&state_mutex);
    log_info("PLC State: RUNNING");

    clock_gettime(CLOCK_MONOTONIC, &timer_start);

    int crash_sig = sigsetjmp(bootstrap_crash_jmp, 1);
    if (crash_sig != 0)
    {
        if (bootstrap_holding_mutex)
        {
            bootstrap_holding_mutex = 0;
            pthread_mutex_unlock(itm);
        }
        const char *sig_name = (crash_sig == SIGFPE)
                                   ? "SIGFPE (arithmetic error, e.g. division by zero)"
                                   : "SIGSEGV (memory access violation)";
        log_error("PLC bootstrap thread crashed with signal %d: %s", crash_sig, sig_name);

        signal(SIGFPE,  SIG_DFL);
        signal(SIGSEGV, SIG_DFL);

        pthread_mutex_lock(&state_mutex);
        plc_state = PLC_STATE_ERROR;
        pthread_mutex_unlock(&state_mutex);
        log_info("PLC State: ERROR");
        return NULL;
    }

    /* Walk the configuration via virtual dispatch and discover the GCD
     * base tick + flat task list. Phase 5 keeps a single-thread cycle
     * that runs every task in round-robin (each task runs every
     * interval/base ticks). Phase 6 will replace this with one thread per
     * task on SCHED_FIFO. */
    auto *cfg = static_cast<strucpp::ConfigurationInstance *>(strucpp_config_handle());
    if (!cfg)
    {
        log_error("PLC: configuration handle is NULL");
        pthread_mutex_lock(&state_mutex);
        plc_state = PLC_STATE_ERROR;
        pthread_mutex_unlock(&state_mutex);
        return NULL;
    }

    /* Compute GCD across all task intervals. */
    unsigned long long base_ns = 0;
    auto *resources = cfg->get_resources();
    size_t total_tasks = 0;
    for (size_t r = 0; r < cfg->get_resource_count(); ++r)
    {
        for (size_t t = 0; t < resources[r].task_count; ++t)
        {
            ++total_tasks;
            unsigned long long ivl =
                (unsigned long long)resources[r].tasks[t].interval_ns;
            if (ivl == 0) ivl = 20000000ULL;
            if (base_ns == 0) base_ns = ivl;
            else
            {
                unsigned long long a = base_ns, b = ivl;
                while (b) { unsigned long long tmp = b; b = a % b; a = tmp; }
                base_ns = a;
            }
        }
    }
    if (base_ns == 0) base_ns = 20000000ULL;
    if (total_tasks == 0)
    {
        log_error("PLC program declares zero tasks — refusing to run");
        pthread_mutex_lock(&state_mutex);
        plc_state = PLC_STATE_ERROR;
        pthread_mutex_unlock(&state_mutex);
        return NULL;
    }
    log_info("PLC base tick: %llu ns across %zu task(s)",
             (unsigned long long)base_ns, total_tasks);

    /* Allocate per-task contexts and spawn one thread per IEC task.
     * Hold plc_tasks_lock across the alloc + count publish so a STATS
     * reader doesn't observe a non-NULL pointer with the old (zero)
     * count, or vice versa. */
    pthread_mutex_lock(&plc_tasks_lock);
    plc_tasks = static_cast<PlcTaskCtx *>(std::calloc(total_tasks, sizeof(PlcTaskCtx)));
    if (!plc_tasks)
    {
        pthread_mutex_unlock(&plc_tasks_lock);
        log_error("Failed to allocate plc_tasks array");
        pthread_mutex_lock(&state_mutex);
        plc_state = PLC_STATE_ERROR;
        pthread_mutex_unlock(&state_mutex);
        return NULL;
    }
    plc_task_count = total_tasks;
    pthread_mutex_unlock(&plc_tasks_lock);

    {
        size_t flat_idx = 0;
        long   now_t    = (long)time(nullptr);
        for (size_t r = 0; r < cfg->get_resource_count(); ++r)
        {
            for (size_t t = 0; t < resources[r].task_count; ++t)
            {
                PlcTaskCtx *ctx = &plc_tasks[flat_idx];
                auto       &tk  = resources[r].tasks[t];
                ctx->idx               = flat_idx;
                ctx->interval_ns       = tk.interval_ns > 0 ? tk.interval_ns : (int64_t)base_ns;
                ctx->priority          = tk.priority;
                ctx->cpu_affinity_mask = 0;       /* Phase 8 will plumb this from CPU_AFFINITY */
                ctx->is_fastest_task   = false;   /* set below */
                ctx->task_handle       = &tk;
                if (tk.name && tk.name[0] != '\0')
                {
                    std::snprintf(ctx->name, sizeof ctx->name, "%s", tk.name);
                }
                else
                {
                    std::snprintf(ctx->name, sizeof ctx->name, "plc-task-%zu", flat_idx);
                }
                ctx->heartbeat.store(now_t, std::memory_order_relaxed);
                ctx->local_tick.store(0,    std::memory_order_relaxed);
                if (scan_cycle_tracker_init(&ctx->tracker, ctx->interval_ns) != 0)
                {
                    log_error("Failed to init scan-cycle tracker for task %s", ctx->name);
                }
                ++flat_idx;
            }
        }
    }

    /* Pick the fastest task: smallest interval, tie-break by priority,
     * then by declaration order (which is the iteration order above). */
    {
        size_t fastest_idx = 0;
        for (size_t i = 1; i < plc_task_count; ++i)
        {
            PlcTaskCtx *c = &plc_tasks[i];
            PlcTaskCtx *f = &plc_tasks[fastest_idx];
            if (c->interval_ns < f->interval_ns ||
                (c->interval_ns == f->interval_ns && c->priority > f->priority))
            {
                fastest_idx = i;
            }
        }
        plc_tasks[fastest_idx].is_fastest_task = true;
        log_info("PLC: anchoring housekeeping on task %s "
                 "(interval=%lld ns, priority=%d)",
                 plc_tasks[fastest_idx].name,
                 (long long)plc_tasks[fastest_idx].interval_ns,
                 plc_tasks[fastest_idx].priority);
    }

    /* Spawn task threads.
     *
     * Failure mode: if pthread_create succeeds for tasks 0..i-1 and then
     * fails for task i, the previously-spawned threads are running at
     * SCHED_FIFO 99 holding image_tables_mutex and reading from
     * plc_tasks[].  Returning here without cleanup leaves them orphaned
     * — the next load_plc_program reallocates plc_tasks and the old
     * threads dereference freed memory. We must:
     *
     *   1) flip plc_state to ERROR so the surviving task threads exit
     *      their `while (state == RUNNING)` loop on their next iteration;
     *   2) SIGUSR1 each surviving thread to break it out of its
     *      clock_nanosleep without waiting up to interval_ns;
     *   3) join all spawned threads before freeing the array.
     *
     * After this rollback, plc_tasks is nullptr and plc_task_count is 0,
     * so STATS / next-cycle teardown can run without UAF. */
    size_t spawned = 0;
    for (; spawned < plc_task_count; ++spawned)
    {
        if (pthread_create(&plc_tasks[spawned].thread, NULL,
                           plc_task_thread, &plc_tasks[spawned]) != 0)
        {
            log_error("Failed to spawn task %zu thread: %s",
                      spawned, strerror(errno));

            /* Flip to ERROR FIRST so the running tasks exit. */
            pthread_mutex_lock(&state_mutex);
            plc_state = PLC_STATE_ERROR;
            pthread_mutex_unlock(&state_mutex);

            /* Wake any task currently blocked in clock_nanosleep so it
             * notices the state change without waiting a full period. */
            for (size_t k = 0; k < spawned; ++k)
            {
                pthread_kill(plc_tasks[k].thread, SIGUSR1);
            }
            for (size_t k = 0; k < spawned; ++k)
            {
                pthread_join(plc_tasks[k].thread, nullptr);
            }
            /* Take plc_tasks_lock around the destruction so any STATS
             * reader currently iterating exits before we free. */
            pthread_mutex_lock(&plc_tasks_lock);
            for (size_t k = 0; k < plc_task_count; ++k)
            {
                scan_cycle_tracker_cleanup(&plc_tasks[k].tracker);
            }
            std::free(plc_tasks);
            plc_tasks      = nullptr;
            plc_task_count = 0;
            pthread_mutex_unlock(&plc_tasks_lock);
            return NULL;
        }
    }
    log_info("Spawned %zu PLC task thread(s)", plc_task_count);

    /* Bootstrap thread: wait for state change, then signal+join the
     * task threads. Phase 7 may add the housekeeping window onto the
     * fastest task's thread; Phase 6 keeps the bootstrap quiet. */
    while (plc_get_state() == PLC_STATE_RUNNING)
    {
        timespec poll_sleep;
        poll_sleep.tv_sec  = 1;
        poll_sleep.tv_nsec = 0;
        nanosleep(&poll_sleep, nullptr);
    }

    log_info("Stopping %zu PLC task thread(s)", plc_task_count);
    for (size_t i = 0; i < plc_task_count; ++i)
    {
        pthread_kill(plc_tasks[i].thread, SIGUSR1);
    }
    for (size_t i = 0; i < plc_task_count; ++i)
    {
        pthread_join(plc_tasks[i].thread, nullptr);
    }

    /* Take plc_tasks_lock for the tracker-cleanup + free. A STATS reader
     * that started iterating before STOP arrived will block briefly
     * waiting for this critical section, then exit because plc_task_count
     * is observed as 0. Without the lock, the reader could be midway
     * through scan_cycle_tracker_snapshot when we pthread_mutex_destroy
     * the tracker's own mutex below — undefined behaviour. */
    pthread_mutex_lock(&plc_tasks_lock);
    for (size_t i = 0; i < plc_task_count; ++i)
    {
        scan_cycle_tracker_cleanup(&plc_tasks[i].tracker);
    }
    std::free(plc_tasks);
    plc_tasks      = nullptr;
    plc_task_count = 0;
    pthread_mutex_unlock(&plc_tasks_lock);

    signal(SIGFPE,  SIG_DFL);
    signal(SIGSEGV, SIG_DFL);

    return NULL;
}

extern "C" int load_plc_program(PluginManager *pm)
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

        if (plugin_driver)
        {
            if (plugin_driver_update_config(plugin_driver, "./plugins.conf") != 0)
            {
                log_error("[PLUGIN]: Failed to load plugin configuration");
                pthread_mutex_lock(&state_mutex);
                plc_state = PLC_STATE_ERROR;
                pthread_mutex_unlock(&state_mutex);
                log_info("PLC State: ERROR");
                if (pm == plc_program) plc_program = NULL;
                plugin_manager_destroy(pm);
                return -1;
            }
            /* Load VPP plugins from the editor-generated vpp_plugins.conf.
             * The file is absent when the upload had no VPP target, so an
             * absent file is silently ignored (not an error). */
            if (plugin_driver_append_config(plugin_driver, "./vpp_plugins.conf") != 0)
            {
                log_error("[PLUGIN]: VPP plugin failed to load — check vpp_plugins.conf and build/vpp/");
                pthread_mutex_lock(&state_mutex);
                plc_state = PLC_STATE_ERROR;
                pthread_mutex_unlock(&state_mutex);
                log_info("PLC State: ERROR");
                if (pm == plc_program) plc_program = NULL;
                plugin_manager_destroy(pm);
                return -1;
            }
            if (plugin_driver_init(plugin_driver) != 0)
            {
                /* Roll back any plugins that did initialise before the
                 * failure. Without this, the next INIT cycle's call to
                 * plugin_driver_init would invoke init() on top of
                 * half-allocated state and (e.g.) double-spawn EtherCAT
                 * masters or OPC-UA sockets. */
                log_error("[PLUGIN]: Plugin init failed — rolling back");
                plugin_driver_cleanup_init(plugin_driver);
                pthread_mutex_lock(&state_mutex);
                plc_state = PLC_STATE_ERROR;
                pthread_mutex_unlock(&state_mutex);
                log_info("PLC State: ERROR");
                if (pm == plc_program) plc_program = NULL;
                plugin_manager_destroy(pm);
                return -1;
            }
            log_info("[PLUGIN]: Plugins re-initialized with updated config");
        }

        if (pthread_create(&plc_thread, NULL, plc_cycle_thread, pm) != 0)
        {
            log_error("Failed to create PLC cycle thread");
            /* plugin_driver_init succeeded above, so plugins hold init
             * state (allocated buffers, opened devices). Roll those back
             * before bailing — otherwise the next start retries init()
             * on a half-initialised driver. */
            if (plugin_driver) plugin_driver_cleanup_init(plugin_driver);
            pthread_mutex_lock(&state_mutex);
            plc_state = PLC_STATE_ERROR;
            pthread_mutex_unlock(&state_mutex);
            log_info("PLC State: ERROR");
            // Drop the manager so the next RUNNING transition re-runs
            // find_libplc_file. See the comment on the dlopen-failure
            // branch below for the full reasoning.
            if (pm == plc_program) plc_program = NULL;
            plugin_manager_destroy(pm);
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
        // Without this, plc_program survives the failed dlopen with a
        // stale so_path. The build script rotates the libplc filename
        // (libplc_<ns_timestamp>.so) on every successful build, so the
        // next "Start PLC" would reuse the deleted path and fail with
        // `cannot open shared object file`. Drop the manager and let
        // plc_set_state(RUNNING) re-run find_libplc_file next time.
        if (pm == plc_program) plc_program = NULL;
        plugin_manager_destroy(pm);
        return -1;
    }
}

extern "C" int unload_plc_program(PluginManager *pm)
{
    if (pm && pm == plc_program)
    {
        PLCState prev_state = plc_get_state();

        if (prev_state != PLC_STATE_ERROR)
        {
            pthread_mutex_lock(&state_mutex);
            plc_state = PLC_STATE_STOPPED;
            pthread_mutex_unlock(&state_mutex);
        }

        pthread_join(plc_thread, NULL);

        journal_cleanup();
        log_info("Journal buffer cleaned up");

        plugin_driver_stop(plugin_driver);

        pthread_mutex_t *itm = image_tables_mutex();
        pthread_mutex_lock(itm);
        image_tables_clear_null_pointers();
        pthread_mutex_unlock(itm);

        void (*python_cleanup)(void);
        *(void **)&python_cleanup = plugin_manager_get_symbol(pm, "python_blocks_cleanup");
        if (python_cleanup) python_cleanup();

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

extern "C" PLCState plc_get_state(void)
{
    pthread_mutex_lock(&state_mutex);
    PLCState s = plc_state;
    pthread_mutex_unlock(&state_mutex);
    return s;
}

extern "C" bool plc_set_state(PLCState new_state)
{
    pthread_mutex_lock(&state_mutex);
    if (plc_state == new_state)
    {
        pthread_mutex_unlock(&state_mutex);
        return false;
    }
    plc_state = new_state;
    pthread_mutex_unlock(&state_mutex);

    // Note: plc_state must flip BEFORE load/unload runs. Task threads
    // exit their scan loops via `while (plc_get_state() == RUNNING)`,
    // and unload_plc_program() depends on that signal to join them.
    // The "STATUS reports STOPPED while teardown is still in flight"
    // window this opens is gated externally via is_transitioning in
    // unix_socket.c — STATUS returns STATUS:TRANSITIONING for the
    // duration of the worker, so external pollers don't see the
    // stale STOPPED.

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
    else if (new_state == PLC_STATE_STOPPED)
    {
        if (plc_program)
        {
            if (unload_plc_program(plc_program) < 0) return false;
        }
    }

    return true;
}

extern "C" void plc_state_manager_cleanup(void)
{
    if (plc_program) unload_plc_program(plc_program);
}

extern "C" void plc_force_error_state(void)
{
    pthread_mutex_lock(&state_mutex);
    plc_state = PLC_STATE_ERROR;
    pthread_mutex_unlock(&state_mutex);
    log_info("PLC State: ERROR");
}

extern "C" int plc_get_crash_signal(void)
{
    return (int)plc_crash_signal;
}
