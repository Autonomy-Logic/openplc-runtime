// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Walks the loaded program's ConfigurationInstance via virtual dispatch,
// spawns one SCHED_FIFO pthread per IEC task, and anchors per-cycle
// housekeeping on the fastest task's thread. Linux-only.

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
#include "unix_socket.h"
}

// Runtime-side strucpp ABI mirror — see core/src/lib/strucpp_abi.hpp
#include "../lib/strucpp_abi.hpp"

#include "debug_write_journal.h"
#include "image_tables.h"
#include "journal_buffer.h"
#include "plc_retain.h"
#include "plc_state_manager.h"
#include "plcapp_manager.h"
#include "scan_cycle_manager.h"
#include "task_policy.h"
#include "utils/log.h"
#include "utils/rt_mutex.h"
#include "utils/utils.h"
#include "utils/watchdog.h"

/* Writers serialise on state_mutex; readers load the atomic without locking. */
static std::atomic<PLCState> plc_state{PLC_STATE_STOPPED};
static_assert(std::atomic<PLCState>::is_always_lock_free, "state reads must not lock");
static pthread_mutex_t       state_mutex = PTHREAD_MUTEX_INITIALIZER;

struct timespec  timer_start;
pthread_t        plc_thread;
PluginManager   *plc_program = NULL;

extern plugin_driver_t   *plugin_driver;

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

static std::atomic<int> g_tasks_running{0};
static pthread_mutex_t  done_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t   done_cond;   /* initialised once, CLOCK_MONOTONIC */
static pthread_once_t   done_cond_once = PTHREAD_ONCE_INIT;

__attribute__((constructor)) static void state_manager_locks_init_pi(void)
{
    rt_mutex_upgrade_static(&state_mutex, "state_mutex");
    rt_mutex_upgrade_static(&plc_tasks_lock, "plc_tasks_lock");
    rt_mutex_upgrade_static(&done_mutex, "done_mutex");
}

/* One-time init of done_cond on CLOCK_MONOTONIC (not REALTIME) so it
 * matches the dispatcher deadline and does not jump under NTP. */
static void init_done_cond(void)
{
    pthread_condattr_t cattr;
    pthread_condattr_init(&cattr);
    pthread_condattr_setclock(&cattr, CLOCK_MONOTONIC);
    pthread_cond_init(&done_cond, &cattr);
    pthread_condattr_destroy(&cattr);
}

/* Called by a worker when it leaves a scan by any path. Decrements the
 * in-flight count and, if it was the last, wakes the dispatcher so it can
 * retire cycle_end. Cheap: one atomic; the mutex+signal only on the 1->0
 * edge. */
static void worker_scan_done(void)
{
    if (g_tasks_running.fetch_sub(1, std::memory_order_acq_rel) == 1)
    {
        pthread_mutex_lock(&done_mutex);
        pthread_cond_signal(&done_cond);
        pthread_mutex_unlock(&done_mutex);
    }
}

/* Bootstrap thread runs no IEC task body, but needs its own crash
 * recovery jmp pair. Active task ctx is __thread so the signal handler
 * picks the right siglongjmp target. */
static __thread PlcTaskCtx  *current_task_ctx        = nullptr;
static sigjmp_buf            bootstrap_crash_jmp;
static volatile sig_atomic_t bootstrap_crash_sig     = 0;
static volatile sig_atomic_t bootstrap_holding_mutex = 0;
static volatile sig_atomic_t plc_crash_signal        = 0;

/* Interval between abort signals while waiting for an aborted task to exit. */
#define PLC_TASK_ABORT_RESEND_NS 100000000LL

/* Poll interval while waiting for a task thread to return. */
#define PLC_TASK_EXIT_POLL_NS 1000000L

/* Set when a task had to be aborted; the stop then lands ERROR, not STOPPED. */
static std::atomic<bool> g_task_fault{false};

/* Longest task interval of the loaded program, for the stop budget. */
static std::atomic<int64_t> g_longest_interval_ns{0};

/* Oldest in-flight first scan (release ns, 0 = none), and the watchdog's trip request for it. */
static std::atomic<int64_t> g_first_scan_since_ns{0};
static std::atomic<bool>    g_first_scan_trip{false};

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

/* Jumps out of the scan body to the task's recovery point. No-op outside the scan window. */
static void plc_abort_handler(int sig)
{
    PlcTaskCtx *ctx = current_task_ctx;
    if (ctx && ctx->in_body)
    {
        ctx->crash_sig = sig;
        siglongjmp(ctx->crash_jmp, sig);
    }
}

/* Drop the image mutex if this task is in the locked window. Mirrors
 * the signal-handler recovery below so a C++ exception mid-scan does
 * not leave it held. Per-global strucpp mutexes self-release. */
static void plc_task_release_locks(PlcTaskCtx *ctx)
{
    if (ctx->holding_mutex)
    {
        ctx->holding_mutex = 0;
        pthread_mutex_unlock(image_tables_mutex());
    }
}

/* Per-task thread: FIFO priority from the IEC priority, optional affinity, crash and
 * watchdog-abort recovery, then one scan per release posted by the dispatcher. */
static void plc_task_body(PlcTaskCtx *ctx)
{
    current_task_ctx = ctx;

    pthread_setname_np(pthread_self(), ctx->name);

    /* IEC 0 (highest) -> FIFO 49; always below the dispatcher and watchdog. */
    bool clamped = false;
    int  rt      = plc_task_fifo_priority(ctx->priority, &clamped);
    if (clamped)
    {
        log_warn("[task %s] IEC priority %d outside %d..%d, clamped", ctx->name, ctx->priority,
                 PLC_IEC_PRIORITY_MIN, PLC_IEC_PRIORITY_MAX);
    }
    sched_param sp{};
    sp.sched_priority = rt;
    int sp_rc = pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp);
    if (sp_rc != 0)
    {
        log_warn("[task %s] SCHED_FIFO(%d) failed: %s — running default scheduling",
                 ctx->name, rt, strerror(sp_rc));
    }
    else
    {
        log_info("[task %s] SCHED_FIFO priority %d (IEC priority %d)", ctx->name, rt,
                 ctx->priority);
    }

    if (ctx->cpu_affinity_mask != 0)
    {
        // CPU affinity is Linux-only (cpu_set_t family). SCHED_FIFO is
        // also unavailable on Windows, so skip silently off-Linux.
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

    /* Recovery point for SIGSEGV/SIGFPE from IEC code and for the watchdog abort
     * (PLC_TASK_ABORT_SIGNAL): release held locks, mark the task dead, exit this thread. */
    if (sigsetjmp(ctx->crash_jmp, 1) != 0)
    {
        ctx->in_body = 0;
        plc_task_release_locks(ctx);
        ctx->alive.store(0, std::memory_order_release);
        /* Both signals only jump from a scan body, so release the in-flight slot. */
        worker_scan_done();
        if (ctx->crash_sig == PLC_TASK_ABORT_SIGNAL)
            log_error("[task %s] scan aborted by the watchdog", ctx->name);
        else
            log_error("[task %s] terminated by signal %d — other tasks keep running",
                      ctx->name, ctx->crash_sig);
        return;
    }

    auto *task = static_cast<strucpp::TaskInstance *>(ctx->task_handle);

    /* Worker loop. No clock: the GCD dispatcher owns all timing. One
     * scan per release; `completed` is bumped so overrun logic sees it. */
    while (true)
    {
        /* Block until the dispatcher releases us. sem_wait returns EINTR on a
         * signal (the SIGUSR1 stop-wake); just retry. On stop the dispatcher
         * posts `go` once and has already flipped plc_state, so the state check
         * below breaks us out. */
        while (sem_wait(&ctx->go) != 0 && errno == EINTR) { /* retry */ }
        if (plc_get_state() != PLC_STATE_RUNNING) break;

        /* Apply the dispatcher-stamped scan-stable IEC time. Must run on
         * THIS worker thread so the thread_local __CURRENT_TIME_NS is
         * what the task body and its FB timers read. */
        if (ext_strucpp_set_current_time)
            ext_strucpp_set_current_time(ctx->time_at_dispatch);

        scan_cycle_tracker_start(&ctx->tracker);

        /* Short locked window drains located inputs into the image, then
         * the body runs against .so storage directly. Shared globals have
         * their own strucpp mutexes. try/catch here contains strucpp
         * runtime throws so one task's fault does not abort the process. */
        try
        {
            /* 1. Copy-in located inputs (image_lock drains the journal). */
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

            /* 2. Run the bodies. Shared-global access self-serializes on each
             *    global's own mutex inside run() (strucpp GlobalVar<V>); no
             *    runtime-owned global lock and no private copy-in/out. */
            /* The abort may jump only out of IEC code, never out of a runtime lock window. */
            ctx->in_body = 1;
            for (size_t p = 0; p < task->program_count; ++p)
                task->programs[p]->run();
            ctx->in_body = 0;

            /* 3. Copy-out: journal changed located outputs (lock-free; applied
             *    to the image on the next drain — the dispatcher's frame top). */
            for (size_t p = 0; p < task->program_count; ++p)
            {
                uint32_t off = 0, cnt = 0;
                task->programs[p]->located_range(&off, &cnt);
                if (cnt) image_tables_threaded_copy_out(off, cnt);
            }
        }
        catch (const std::exception &e)
        {
            ctx->in_body = 0;
            plc_task_release_locks(ctx);
            ctx->alive.store(0, std::memory_order_release);
            worker_scan_done();   /* release the in-flight slot before exiting */
            log_error("[task %s] terminated by unhandled exception: %s — "
                      "other tasks keep running", ctx->name, e.what());
            return;
        }
        catch (...)
        {
            ctx->in_body = 0;
            plc_task_release_locks(ctx);
            ctx->alive.store(0, std::memory_order_release);
            worker_scan_done();   /* release the in-flight slot before exiting */
            log_error("[task %s] terminated by unknown exception — "
                      "other tasks keep running", ctx->name);
            return;
        }

        scan_cycle_tracker_end(&ctx->tracker);

        ctx->local_tick.fetch_add(1, std::memory_order_relaxed);
        /* Signal scan completion LAST (release order): the dispatcher reads
         * completed vs released to decide whether this worker is idle (safe to
         * re-release) or still in its scan (overrun — skip). */
        ctx->completed.fetch_add(1, std::memory_order_release);
        /* Release this scan's in-flight slot; if we're the last task still
         * running this frame, wake the dispatcher so it retires cycle_end off
         * the hot path. */
        worker_scan_done();
    }

    log_info("[task %s] stopped after %llu scans", ctx->name,
             (unsigned long long)ctx->local_tick.load());
}

static void *plc_task_thread(void *arg)
{
    PlcTaskCtx *ctx = static_cast<PlcTaskCtx *>(arg);
    plc_task_body(ctx);
    ctx->exited.store(1, std::memory_order_release);
    return nullptr;
}

static bool task_in_scan(const PlcTaskCtx *c)
{
    return c->released.load(std::memory_order_relaxed) !=
           c->completed.load(std::memory_order_acquire);
}

static int64_t task_stuck_limit_ns(const PlcTaskCtx *c)
{
    return PLC_TASK_STUCK_PERIODS * c->interval_ns;
}

static bool task_first_scan_done(const PlcTaskCtx *c)
{
    return c->completed.load(std::memory_order_acquire) > 0;
}

/* How long the current scan may run after its release before the teardown aborts it. */
static int64_t task_scan_limit_ns(const PlcTaskCtx *c)
{
    const int64_t first_ns = PLC_FIRST_SCAN_TIMEOUT_MS * NS_PER_MS;
    if (!task_first_scan_done(c) && first_ns > task_stuck_limit_ns(c))
        return first_ns;
    return task_stuck_limit_ns(c);
}

/* Waits for the worker to return. An in-flight scan may run until scan_deadline; an idle
 * worker (already woken) until idle_deadline. Returns true when it exited. */
static bool wait_task_exit(const PlcTaskCtx *c, int64_t scan_deadline, int64_t idle_deadline)
{
    const timespec poll = {0, PLC_TASK_EXIT_POLL_NS};
    while (!c->exited.load(std::memory_order_acquire))
    {
        const int64_t now = monotonic_ns();
        if (task_in_scan(c) ? now >= scan_deadline : now >= idle_deadline)
            return false;
        nanosleep(&poll, nullptr);
    }
    return true;
}

/* Signals the worker until it leaves its scan; exits the process if it never does. */
static void abort_task(PlcTaskCtx *c)
{
    if (task_in_scan(c))
        log_error("[task %s] scan still running %lld ms after its release: aborting it", c->name,
                  (long long)(task_scan_limit_ns(c) / NS_PER_MS));
    else
        log_error("[task %s] did not exit within %d ms of being woken", c->name,
                  PLC_TASK_ABORT_TIMEOUT_MS);
    g_task_fault.store(true, std::memory_order_release);

    const timespec poll        = {0, PLC_TASK_EXIT_POLL_NS};
    const int64_t  give_up     = monotonic_ns() + PLC_TASK_ABORT_TIMEOUT_MS * NS_PER_MS;
    int64_t        next_signal = 0;
    while (!c->exited.load(std::memory_order_acquire))
    {
        const int64_t now = monotonic_ns();
        if (now >= give_up)
        {
            char reason[WATCHDOG_REASON_LEN];
            std::snprintf(reason, sizeof reason, "task %s did not exit after the abort", c->name);
            watchdog_fatal_exit(reason);
        }
        if (now >= next_signal)
        {
            int rc = pthread_kill(c->thread, PLC_TASK_ABORT_SIGNAL);
            if (rc != 0)
                log_error("[task %s] abort signal failed: %s", c->name, strerror(rc));
            next_signal = now + PLC_TASK_ABORT_RESEND_NS;
        }
        nanosleep(&poll, nullptr);
    }
}

/* Wakes every worker, lets each in-flight scan run until PLC_TASK_STUCK_PERIODS of its own
 * periods after its release, aborts the ones still running, then joins and frees the array. */
static void reap_task_threads(void)
{
    log_info("Stopping %zu PLC task thread(s)", plc_task_count);
    for (size_t i = 0; i < plc_task_count; ++i)
    {
        sem_post(&plc_tasks[i].go);
        pthread_kill(plc_tasks[i].thread, SIGUSR1);
    }

    const int64_t t_reap = monotonic_ns();
    for (size_t i = 0; i < plc_task_count; ++i)
    {
        PlcTaskCtx   *c = &plc_tasks[i];
        const int64_t scan_deadline =
            c->release_ns.load(std::memory_order_acquire) + task_scan_limit_ns(c);
        const int64_t idle_deadline = (scan_deadline > t_reap ? scan_deadline : t_reap) +
                                      PLC_TASK_ABORT_TIMEOUT_MS * NS_PER_MS;
        if (!wait_task_exit(c, scan_deadline, idle_deadline))
            abort_task(c);
    }

    for (size_t i = 0; i < plc_task_count; ++i)
    {
        pthread_join(plc_tasks[i].thread, nullptr);
    }

    /* Under plc_tasks_lock so a STATS reader never sees a destroyed tracker. */
    pthread_mutex_lock(&plc_tasks_lock);
    for (size_t i = 0; i < plc_task_count; ++i)
    {
        scan_cycle_tracker_cleanup(&plc_tasks[i].tracker);
        sem_destroy(&plc_tasks[i].go);
    }
    std::free(plc_tasks);
    plc_tasks      = nullptr;
    plc_task_count = 0;
    pthread_mutex_unlock(&plc_tasks_lock);
}

/* Zeroes every output and runs one last I/O frame, then holds it so threaded plugins send it. */
static void plc_outputs_off(void)
{
    image_lock();
    /* Pending writes were drained by image_lock; reject later ones so none re-energise %Q. */
    journal_cleanup();
    image_tables_zero_outputs();
    image_unlock();
    if (plugin_driver)
    {
        plugin_driver_cycle_start(plugin_driver);
        plugin_driver_cycle_end(plugin_driver);
    }
    timespec settle = {PLC_OUTPUTS_OFF_SETTLE_MS / 1000,
                       (long)((PLC_OUTPUTS_OFF_SETTLE_MS % 1000) * NS_PER_MS)};
    nanosleep(&settle, nullptr);
    log_info("Outputs forced to 0");
}

void *plc_cycle_thread(void *arg)
{
    PluginManager *pm = (PluginManager *)arg;

    plc_crash_signal        = 0;
    bootstrap_crash_sig     = 0;
    bootstrap_holding_mutex = 0;
    g_task_fault.store(false, std::memory_order_release);
    g_first_scan_since_ns.store(0, std::memory_order_release);
    g_first_scan_trip.store(false, std::memory_order_release);
    watchdog_dispatcher_stopped();

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

    /* Retained variables. init() decides capability; read() loads values
     * for THIS program and discards a prior program's bytes. Must run
     * after located-variable binding so image slots exist. */
    plc_retain_init();
    plc_retain_read();

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

    struct sigaction abort_sa;
    std::memset(&abort_sa, 0, sizeof(abort_sa));
    abort_sa.sa_handler = plc_abort_handler;
    sigemptyset(&abort_sa.sa_mask);
    if (sigaction(PLC_TASK_ABORT_SIGNAL, &abort_sa, NULL) != 0)
        log_error("Failed to install the task abort handler: %s", strerror(errno));

    /* SIGUSR1 wake handler is installed once at process init (plc_main.c).
     * No per-thread re-installation here — the bootstrap thread inherits
     * the handler from the process. */

    log_info("Starting main loop");

    /* State stays TRANSITIONING_TO_RUN until the task threads exist and
     * the dispatcher is about to release the first scan. RUNNING is
     * published below after "Spawned N PLC task thread(s)". */

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

        /* Workers spawned before a dispatcher crash are still alive: reap them (aborting any
         * still scanning) so they are not orphaned, then drive outputs off. */
        if (plc_tasks && plc_task_count)
        {
            reap_task_threads();
            plc_outputs_off();
        }
        return NULL;
    }

    /* Walk the configuration via virtual dispatch: GCD base tick and flat task list. */
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

    /* Sub-millisecond base tick means fractional/sub-ms task intervals
     * near-coprime. Do NOT clamp (breaks the per-task time grid); run at
     * the true GCD and rely on overrun detection. */
    if (base_ns < 1000000ULL)
    {
        log_warn("PLC base tick is %llu ns (< 1 ms): dispatcher runs at %llu Hz. "
                 "Consider harmonizing task intervals to whole milliseconds.",
                 (unsigned long long)base_ns,
                 (unsigned long long)(1000000000ULL / (base_ns ? base_ns : 1)));
    }

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
                ctx->local_tick.store(0,    std::memory_order_relaxed);

                /* Dispatcher plumbing. divisor = interval / base_tick (exact;
                 * base_tick is the GCD of all intervals). The worker is due on
                 * master tick N iff N % divisor == 0. The release semaphore
                 * starts at 0 (worker blocks until the dispatcher posts). */
                sem_init(&ctx->go, 0, 0);
                ctx->divisor          = (uint64_t)(ctx->interval_ns / (int64_t)base_ns);
                if (ctx->divisor == 0) ctx->divisor = 1;
                ctx->time_at_dispatch = 0;
                ctx->alive.store(1,         std::memory_order_relaxed);
                ctx->released.store(0,      std::memory_order_relaxed);
                ctx->completed.store(0,     std::memory_order_relaxed);
                ctx->overrun_count.store(0, std::memory_order_relaxed);
                ctx->stuck_ticks.store(0,   std::memory_order_relaxed);
                ctx->exited.store(0,        std::memory_order_relaxed);
                ctx->release_ns.store(0,    std::memory_order_relaxed);
                ctx->in_body = 0;

                if (scan_cycle_tracker_init(&ctx->tracker, ctx->interval_ns) != 0)
                {
                    log_error("Failed to init scan-cycle tracker for task %s", ctx->name);
                }
                ++flat_idx;
            }
        }
    }

    {
        int64_t longest = 0;
        for (size_t i = 0; i < plc_task_count; ++i)
            if (plc_tasks[i].interval_ns > longest) longest = plc_tasks[i].interval_ns;
        g_longest_interval_ns.store(longest, std::memory_order_release);
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
        /* Housekeeping no longer rides a real task — the GCD master-tick
         * dispatcher owns time/cycle hooks/watchdog feed. is_fastest_task is kept
         * only as a STATS hint (the fastest task is the tightest schedule). */
        log_info("PLC: fastest task is %s (interval=%lld ns, priority=%d)",
                 plc_tasks[fastest_idx].name,
                 (long long)plc_tasks[fastest_idx].interval_ns,
                 plc_tasks[fastest_idx].priority);
    }

    /* Spawn task threads. On partial failure (pthread_create succeeds
     * for 0..i-1, fails at i): flip state to ERROR, post every
     * surviving thread's release so it wakes, join them, free the
     * array. Leaves plc_tasks nullptr and count 0 so STATS is UAF-safe. */
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

            /* Wake any worker blocked on its release semaphore so it observes
             * the state change and exits; SIGUSR1 also breaks one blocked in a
             * syscall. */
            for (size_t k = 0; k < spawned; ++k)
            {
                sem_post(&plc_tasks[k].go);
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
                sem_destroy(&plc_tasks[k].go);
            }
            std::free(plc_tasks);
            plc_tasks      = nullptr;
            plc_task_count = 0;
            pthread_mutex_unlock(&plc_tasks_lock);
            return NULL;
        }
    }
    log_info("Spawned %zu PLC task thread(s)", plc_task_count);

    /* Publish RUNNING only while OUR start is still the claimed
     * transition. Erasing a watchdog-ERROR or shutdown-TRANSITIONING_TO_STOP
     * would hang the join below. The crash and stop paths each publish
     * their own final state (ERROR / STOPPED). */
    if (!plc_publish_running_if_claimed())
    {
        log_warn("PLC bring-up finished but the start is no longer the transition in "
                 "flight (state is %d) — not releasing the first scan",
                 (int)plc_get_state());
        reap_task_threads();
        signal(SIGFPE,  SIG_DFL);
        signal(SIGSEGV, SIG_DFL);
        return NULL;
    }

    
    {
        pthread_setname_np(pthread_self(), "plc_dispatch");
        sched_param dsp{};
        dsp.sched_priority = PLC_FIFO_DISPATCHER;
        int rc             = pthread_setschedparam(pthread_self(), SCHED_FIFO, &dsp);
        if (rc != 0)
            log_warn("dispatcher SCHED_FIFO(%d) failed: %s", PLC_FIFO_DISPATCHER, strerror(rc));
    }

    /* Completion-signal condvar shares the CLOCK_MONOTONIC timeline with the
     * tick deadline (init-once). Reset the in-flight count so a STOP-time
     * remnant from the previous run can't make this run think a task is forever
     * outstanding. */
    pthread_once(&done_cond_once, init_done_cond);
    g_tasks_running.store(0, std::memory_order_relaxed);

    log_info("GCD master-tick dispatcher running (base tick %llu ns)",
             (unsigned long long)base_ns);
    watchdog_dispatcher_started((int64_t)base_ns);

    uint64_t master_tick      = 0;
    bool     cycle_end_pending = false;   /* a frame's cycle_end not yet fired */
    size_t   stuck_idx         = SIZE_MAX;
    bool     fault_stop_claimed = false;
    timespec next_tick;
    clock_gettime(CLOCK_MONOTONIC, &next_tick);

    while (plc_get_state() == PLC_STATE_RUNNING)
    {
        /* ---- Phase B: the tick (runs at the absolute deadline) ---- */
        const int64_t master_time = (int64_t)master_tick * (int64_t)base_ns;

        watchdog_feed();

        {
            int64_t oldest = 0;
            for (size_t i = 0; i < plc_task_count; ++i)
            {
                PlcTaskCtx *c = &plc_tasks[i];
                if (c->alive.load(std::memory_order_acquire) && task_in_scan(c) &&
                    !task_first_scan_done(c))
                {
                    const int64_t rel = c->release_ns.load(std::memory_order_acquire);
                    if (oldest == 0 || rel < oldest) oldest = rel;
                }
            }
            g_first_scan_since_ns.store(oldest, std::memory_order_release);
        }

        /* Which tasks are due this tick? */
        bool any_due = false;
        for (size_t i = 0; i < plc_task_count; ++i)
        {
            PlcTaskCtx *c = &plc_tasks[i];
            if (c->alive.load(std::memory_order_acquire) &&
                (master_tick % c->divisor) == 0)
            {
                any_due = true;
                break;
            }
        }

        if (any_due)
        {
            /* Overrun: previous frame's cycle_end not yet retired. Fire
             * it before opening the new frame. Only path where cycle_end
             * lands on the task-wake hot path. */
            if (cycle_end_pending)
            {
                image_lock();
                image_unlock();
                if (plugin_driver) plugin_driver_cycle_end(plugin_driver);
                cycle_end_pending = false;
            }
            if (plugin_driver) plugin_driver_cycle_start(plugin_driver);

            /* Prime config-scope shared globals from the input image.
             * Guarded by quiescence (no overrunning task is in run())
             * to avoid racing a per-global mutex. */
            if (g_tasks_running.load(std::memory_order_acquire) == 0)
            {
                image_lock();
                image_tables_copy_config_globals_in();
                image_unlock();
            }

            bool released_any = false;
            for (size_t i = 0; i < plc_task_count; ++i)
            {
                PlcTaskCtx *c = &plc_tasks[i];
                if (!c->alive.load(std::memory_order_acquire)) continue;
                if ((master_tick % c->divisor) != 0) continue;

                long r  = c->released.load(std::memory_order_relaxed);
                long cc = c->completed.load(std::memory_order_acquire);
                if (r == cc)
                {
                    /* Worker idle (caught up) → stamp time, count it in flight,
                     * and release. The fetch_add must happen-before sem_post so
                     * the worker's matching worker_scan_done() can never drive
                     * g_tasks_running negative. */
                    c->stuck_ticks.store(0, std::memory_order_relaxed);
                    c->release_ns.store(monotonic_ns(), std::memory_order_release);
                    c->time_at_dispatch = master_time;
                    c->released.store(r + 1, std::memory_order_relaxed);
                    g_tasks_running.fetch_add(1, std::memory_order_acq_rel);
                    sem_post(&c->go);
                    released_any = true;
                }
                else
                {
                    /* r > cc: worker still in its previous scan → overrun. Do
                     * NOT re-release (binary), so activations never pile up. The
                     * task simply runs at a lower effective rate; the others are
                     * unaffected. Rate-limit the log. */
                    /* Ticks, not wall time: replayed ticks after a late dispatcher are missed deadlines too. */
                    if (task_first_scan_done(c))
                    {
                        long st = c->stuck_ticks.fetch_add(1, std::memory_order_relaxed) + 1;
                        if (st >= PLC_TASK_STUCK_PERIODS && stuck_idx == SIZE_MAX)
                            stuck_idx = i;
                    }
                    long oc = c->overrun_count.fetch_add(1, std::memory_order_relaxed) + 1;
                    if (oc == 1 || (oc % 50) == 0)
                        log_warn("[task %s] scan overrun #%ld: body exceeds its "
                                 "%lld ms period — running at reduced rate, "
                                 "other tasks unaffected",
                                 c->name, oc,
                                 (long long)(c->interval_ns / 1000000));
                }
            }

            /* This frame owes a cycle_end once its released tasks all finish. */
            if (released_any) cycle_end_pending = true;
            ++scan_counter;

            bool first_scan_trip = false;
            if (stuck_idx == SIZE_MAX && g_first_scan_trip.exchange(false, std::memory_order_acq_rel))
            {
                for (size_t i = 0; i < plc_task_count; ++i)
                {
                    PlcTaskCtx *c = &plc_tasks[i];
                    if (!c->alive.load(std::memory_order_acquire) || !task_in_scan(c) ||
                        task_first_scan_done(c))
                        continue;
                    if (stuck_idx == SIZE_MAX ||
                        c->release_ns.load(std::memory_order_acquire) <
                            plc_tasks[stuck_idx].release_ns.load(std::memory_order_acquire))
                        stuck_idx = i;
                }
                first_scan_trip = (stuck_idx != SIZE_MAX);
            }

            if (stuck_idx != SIZE_MAX)
            {
                PlcTaskCtx *c = &plc_tasks[stuck_idx];
                if (first_scan_trip)
                    log_error("[task %s] first scan still running after %d ms: stopping the PLC",
                              c->name, PLC_FIRST_SCAN_TIMEOUT_MS);
                else
                    log_error("[task %s] stuck in one scan for %d periods (%lld ms): stopping the PLC",
                              c->name, PLC_TASK_STUCK_PERIODS,
                              (long long)(task_stuck_limit_ns(c) / NS_PER_MS));
                g_task_fault.store(true, std::memory_order_release);
                /* Claim now so no command lands mid-drain; the teardown runs after the reap. */
                fault_stop_claimed = plc_claim_transition(PLC_STATE_STOPPED);
                break;
            }
        }

        ++master_tick;

        /* Phase A: wait out the period on the absolute deadline, waking
         * early to retire cycle_end as soon as the frame completes
         * (g_tasks_running hits 0). Predicate checked under done_mutex
         * before the wait so a finishing task does not lose its wakeup. */
        next_tick.tv_nsec += (long)(base_ns % 1000000000ULL);
        next_tick.tv_sec  += (time_t)(base_ns / 1000000000ULL);
        if (next_tick.tv_nsec >= 1000000000L)
        {
            next_tick.tv_nsec -= 1000000000L;
            next_tick.tv_sec  += 1;
        }

        pthread_mutex_lock(&done_mutex);
        for (;;)
        {
            /* Break out promptly on STOP/ERROR instead of waiting for the
             * deadline (a finishing worker's signal, or a spurious wake, gives
             * us the chance; worst case is still one base tick via ETIMEDOUT). */
            if (plc_get_state() != PLC_STATE_RUNNING) break;

            if (cycle_end_pending &&
                g_tasks_running.load(std::memory_order_acquire) == 0)
            {
                pthread_mutex_unlock(&done_mutex);
                image_lock();          /* drain: commit this frame's outputs */
                /* Journal config-scope globals before the drain copies
                 * them to the image. Safe under quiescence; no per-global
                 * mutex needed. */
                image_tables_copy_config_globals_out();
                /* Apply queued external writes/forces (debugger, OPC-UA) here:
                 * g_tasks_running == 0 so no worker is mid-scan, and we hold
                 * image_lock. Cheap no-op when nothing is queued. */
                debug_write_journal_drain();
                /* Retained values once per scan, under quiescence. The
                 * plugin decides whether to actually commit now. */
                plc_retain_save();
                image_unlock();
                if (plugin_driver) plugin_driver_cycle_end(plugin_driver);
                cycle_end_pending = false;
                pthread_mutex_lock(&done_mutex);
                continue;              /* now just wait out the deadline */
            }
            /* An overrun may retire cycle_end before its worker finishes.
             * If no new task was released, quiescence still permits external
             * mutations even though this frame no longer owes cycle_end.
             * Do not repeat plugin hooks or copy stale program outputs here. */
            if (!cycle_end_pending &&
                g_tasks_running.load(std::memory_order_acquire) == 0)
            {
                pthread_mutex_unlock(&done_mutex);
                image_lock();
                debug_write_journal_drain();
                image_unlock();
                pthread_mutex_lock(&done_mutex);
            }
            int rc = pthread_cond_timedwait(&done_cond, &done_mutex, &next_tick);
            if (rc == ETIMEDOUT) break;   /* deadline reached → next tick */
            /* rc == 0 (signalled) or spurious → loop and re-check the predicate */
        }
        pthread_mutex_unlock(&done_mutex);
    }

    watchdog_dispatcher_stopped();
    g_first_scan_since_ns.store(0, std::memory_order_release);
    reap_task_threads();
    plc_outputs_off();

    signal(SIGFPE,  SIG_DFL);
    signal(SIGSEGV, SIG_DFL);

    /* The stop's unload joins this thread, so it must run on another one. */
    if (fault_stop_claimed && !plc_complete_claimed_transition_async(PLC_STATE_STOPPED))
        log_error("Could not start the fault stop; the PLC stays in TRANSITIONING_TO_STOP");

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
        /* Log only: must NOT publish PLC_STATE_INIT here, which would
         * erase TRANSITIONING_TO_RUN and let a stop be claimed mid-start. */
        log_info("Loading PLC application");

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
        // Clear plc_program so the next Start re-runs find_libplc_file.
        // The build rotates libplc_<ts>.so, so the stale so_path would
        // fail with "cannot open shared object file".
        if (pm == plc_program) plc_program = NULL;
        plugin_manager_destroy(pm);
        return -1;
    }
}

/* Serialises unloads: a shutdown and a fault stop may both reach here; the second finds nothing. */
static pthread_mutex_t unload_mutex = PTHREAD_MUTEX_INITIALIZER;

__attribute__((constructor)) static void unload_mutex_init_pi(void)
{
    rt_mutex_upgrade_static(&unload_mutex, "unload_mutex");
}

static int unload_plc_program_locked(PluginManager *pm);

extern "C" int unload_plc_program(PluginManager *pm)
{
    pthread_mutex_lock(&unload_mutex);
    int rc = unload_plc_program_locked(pm);
    pthread_mutex_unlock(&unload_mutex);
    return rc;
}

static int unload_plc_program_locked(PluginManager *pm)
{
    if (pm && pm == plc_program)
    {
        /* Publish TRANSITIONING_TO_STOP to break the dispatcher and
         * workers out of their RUNNING loops. Overwrite anything but
         * ERROR, which must survive teardown. */
        pthread_mutex_lock(&state_mutex);
        if (plc_state != PLC_STATE_ERROR)
        {
            plc_state = PLC_STATE_TRANSITIONING_TO_STOP;
        }
        pthread_mutex_unlock(&state_mutex);

        pthread_join(plc_thread, NULL);

        /* Commit retained values: AFTER the join (consistent snapshot)
         * and BEFORE plugin_driver_stop (plugin-backed stores need to
         * still be alive to answer). */
        plc_retain_flush();

        journal_cleanup();
        debug_write_journal_reset();
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

        /* An aborted task makes the stop a fault: land ERROR. ERROR also survives STOPPED. */
        if (g_task_fault.exchange(false, std::memory_order_acq_rel))
            plc_publish_final_state(PLC_STATE_ERROR);
        else
            plc_publish_final_state(PLC_STATE_STOPPED);
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
    return plc_state.load(std::memory_order_acquire);
}

extern "C" int64_t plc_stop_budget_ms(void)
{
    int64_t grace_ms =
        PLC_TASK_STUCK_PERIODS * g_longest_interval_ns.load(std::memory_order_acquire) / NS_PER_MS;
    if (grace_ms < PLC_FIRST_SCAN_TIMEOUT_MS)
        grace_ms = PLC_FIRST_SCAN_TIMEOUT_MS;
    return grace_ms + PLC_OUTPUTS_OFF_SETTLE_MS + PLC_STOP_TEARDOWN_ALLOWANCE_MS;
}

extern "C" bool plc_outputs_off_without_program(void)
{
    if (!plugin_driver)
    {
        log_warn("No plugin driver: outputs cannot be driven off");
        return false;
    }
    if (plugin_driver_update_config(plugin_driver, "./plugins.conf") != 0 ||
        plugin_driver_append_config(plugin_driver, "./vpp_plugins.conf") != 0)
    {
        log_error("[PLUGIN]: Could not load the plugin configuration to drive outputs off");
        return false;
    }
    if (plugin_driver_init(plugin_driver) != 0)
    {
        plugin_driver_cleanup_init(plugin_driver);
        log_error("[PLUGIN]: Plugin init failed: outputs cannot be driven off");
        return false;
    }

    pthread_mutex_t *itm = image_tables_mutex();
    pthread_mutex_lock(itm);
    image_tables_fill_null_pointers();
    pthread_mutex_unlock(itm);

    plugin_driver_start(plugin_driver);
    plc_outputs_off();
    plugin_driver_stop(plugin_driver);

    pthread_mutex_lock(itm);
    image_tables_clear_null_pointers();
    pthread_mutex_unlock(itm);
    return true;
}

extern "C" int64_t plc_first_scan_pending_since_ns(void)
{
    return g_first_scan_since_ns.load(std::memory_order_acquire);
}

extern "C" void plc_request_first_scan_trip(void)
{
    g_first_scan_trip.store(true, std::memory_order_release);
}

extern "C" bool plc_state_is_transitioning(void)
{
    const PLCState s = plc_get_state();
    return s == PLC_STATE_TRANSITIONING_TO_RUN || s == PLC_STATE_TRANSITIONING_TO_STOP;
}

extern "C" bool plc_claim_transition(PLCState target)
{
    if (target != PLC_STATE_RUNNING && target != PLC_STATE_STOPPED)
    {
        log_error("Refusing to transition to state %d: only RUNNING and STOPPED are"
                  " requestable targets", (int)target);
        return false;
    }

    pthread_mutex_lock(&state_mutex);

    /* Drop, don't queue. Requests are dropped while a transition is in flight,
     * and the switch's intent is recovered afterwards by reconciliation (see
     * plc_switch_take_movement), so nothing has to be remembered here. */
    if (plc_state == PLC_STATE_TRANSITIONING_TO_RUN || plc_state == PLC_STATE_TRANSITIONING_TO_STOP)
    {
        pthread_mutex_unlock(&state_mutex);
        return false;
    }

    if (plc_state == target)
    {
        pthread_mutex_unlock(&state_mutex);
        return false;
    }

    plc_state = (target == PLC_STATE_RUNNING) ? PLC_STATE_TRANSITIONING_TO_RUN
                                              : PLC_STATE_TRANSITIONING_TO_STOP;
    pthread_mutex_unlock(&state_mutex);

    log_info("PLC State: %s", target == PLC_STATE_RUNNING ? "TRANSITIONING_TO_RUN"
                                                          : "TRANSITIONING_TO_STOP");
    return true;
}

extern "C" void plc_publish_final_state(PLCState final_state)
{
    const char *name = "UNKNOWN";
    switch (final_state)
    {
    case PLC_STATE_RUNNING: name = "RUNNING"; break;
    case PLC_STATE_STOPPED: name = "STOPPED"; break;
    case PLC_STATE_ERROR:   name = "ERROR";   break;
    case PLC_STATE_EMPTY:   name = "EMPTY";   break;
    default: break;
    }

    pthread_mutex_lock(&state_mutex);

    /* ERROR outranks a STOPPED landing: a task that crashed mid-teardown recorded
     * the fact that matters, and the teardown completing must not erase it. */
    if (plc_state == PLC_STATE_ERROR && final_state == PLC_STATE_STOPPED)
    {
        pthread_mutex_unlock(&state_mutex);
        log_info("Transition finished in ERROR — keeping ERROR rather than STOPPED");
        return;
    }

    plc_state = final_state;
    pthread_mutex_unlock(&state_mutex);
    log_info("PLC State: %s", name);
}

extern "C" bool plc_publish_running_if_claimed(void)
{
    /* Check and publish RUNNING under one critical section, so a stop
     * cannot be claimed between the read and the write. */
    pthread_mutex_lock(&state_mutex);
    if (plc_state != PLC_STATE_TRANSITIONING_TO_RUN)
    {
        pthread_mutex_unlock(&state_mutex);
        return false;
    }
    plc_state = PLC_STATE_RUNNING;
    pthread_mutex_unlock(&state_mutex);
    log_info("PLC State: RUNNING");
    return true;
}

extern "C" bool plc_set_state(PLCState new_state)
{
    // Executes a transition already claimed via plc_claim_transition.
    // No state is written here: the final state is published by whoever
    // confirms the transition finished (plc_cycle_thread for RUNNING,
    // unload_plc_program for STOPPED).

    if (new_state == PLC_STATE_RUNNING)
    {
        if (plc_program == NULL)
        {
            char *libplc_path = find_libplc_file(libplc_build_dir);
            if (libplc_path == NULL)
            {
                log_error("Failed to find libplc file");
                plc_publish_final_state(PLC_STATE_EMPTY);
                return false;
            }

            plc_program = plugin_manager_create(libplc_path);
            free(libplc_path);

            if (plc_program == NULL)
            {
                log_error("Failed to create PluginManager");
                plc_publish_final_state(PLC_STATE_EMPTY);
                return false;
            }
        }
        if (load_plc_program(plc_program) < 0)
        {
            /* load_plc_program publishes ERROR or EMPTY itself on the paths it
             * knows about; this covers anything it does not, so a claimed
             * transition can never end without a final state. Re-publishing the
             * same value is harmless. */
            if (plc_state_is_transitioning()) plc_publish_final_state(PLC_STATE_ERROR);
            return false;
        }
    }
    else if (new_state == PLC_STATE_STOPPED)
    {
        if (plc_program)
        {
            if (unload_plc_program(plc_program) < 0)
            {
                /* Teardown failed. STOPPED is still the honest landing -- there
                 * is no program running -- and leaving TRANSITIONING set would
                 * make the runtime refuse every command from here on. */
                if (plc_state_is_transitioning()) plc_publish_final_state(PLC_STATE_STOPPED);
                return false;
            }
        }
        else
        {
            /* Nothing loaded, so the stop is already true. Still has to be
             * published: the transition was claimed, and only a final state
             * ends it. */
            plc_publish_final_state(PLC_STATE_STOPPED);
        }
    }

    return true;
}

extern "C" void plc_state_manager_cleanup(void)
{
    /* Wait for any in-flight transition to land before teardown, so we
     * never join an unassigned plc_thread or race the cycle thread's
     * RUNNING publish. Bounded by the transition-landing timeout. */
    const int poll_ms = 20;
    int       waited  = 0;
    while (plc_state_is_transitioning() && waited < PLC_TRANSITION_LANDING_TIMEOUT_MS)
    {
        struct timespec poll = { 0, (long)poll_ms * 1000000L };
        nanosleep(&poll, nullptr);
        waited += poll_ms;
    }
    if (waited > 0)
    {
        log_info("Shutdown waited %d ms for the state change in flight to land", waited);
    }
    if (plc_state_is_transitioning())
    {
        log_warn("Shutdown proceeding with a state change still in flight after %d ms",
                 waited);
    }

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
