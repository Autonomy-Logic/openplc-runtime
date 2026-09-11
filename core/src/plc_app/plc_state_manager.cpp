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

#include "debug_write_journal.h"
#include "image_tables.h"
#include "journal_buffer.h"
#include "plc_retain.h"
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

/* -----------------------------------------------------------------------
 * Task-completion signalling (for off-hot-path cycle_end).
 *
 * g_tasks_running = number of task scans currently in flight (released by
 * the dispatcher but not yet finished). The dispatcher increments it once
 * per release; every worker decrements it exactly once when it leaves a scan
 * — via ANY exit path (normal completion, C++ exception, hardware-signal
 * recovery) — and the worker that brings it to 0 signals done_cond.
 *
 * The dispatcher waits on done_cond with the next tick's ABSOLUTE
 * CLOCK_MONOTONIC deadline (pthread_cond_timedwait). It therefore wakes on
 * whichever comes first: all-tasks-done (fire cycle_end now, off the
 * task-wake hot path) or the deadline (start the next tick). The condvar's
 * clock is set to CLOCK_MONOTONIC so its deadline shares the dispatcher's
 * timeline. g_tasks_running is reset to 0 at each program load, so a stale
 * count left by a STOP (a worker woken to exit without finishing a scan)
 * never carries into the next run.
 * --------------------------------------------------------------------- */
static std::atomic<int> g_tasks_running{0};
static pthread_mutex_t  done_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t   done_cond;   /* initialised once, CLOCK_MONOTONIC */
static pthread_once_t   done_cond_once = PTHREAD_ONCE_INIT;

/* One-time init of done_cond on the CLOCK_MONOTONIC clock (the default is
 * CLOCK_REALTIME, which would mismatch the dispatcher's monotonic deadline and
 * jump under NTP). Init-once (not per-load) so a crash that skips teardown
 * never leaves a destroyed-then-reinitialised cond. */
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
 * thrown mid-scan can't leave the image mutex locked when the thread unwinds
 * and exits. holding_mutex is set only inside the locked window, so at most
 * that lock is released.
 *
 * Shared globals are no longer synced under a single runtime-owned mutex:
 * each shared global carries its own std::mutex inside the .so (strucpp's
 * GlobalVar<V>), taken and released around each access within run(). Those
 * fine-grained locks are always released before run() returns, so there is
 * nothing global for the crash path to unwind here. */
static void plc_task_release_locks(PlcTaskCtx *ctx)
{
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

    /* 99 is reserved for the dispatcher, which has to be strictly above every
     * worker for its tick never to be delayed by a busy one. A worker allowed to
     * reach 99 would only TIE it, and SCHED_FIFO does not time-slice between equal
     * priorities: a task that never blocks (an unbounded loop in IEC code) would
     * then keep the dispatcher off that CPU entirely, along with anything else
     * trying to bring the PLC down. */
    int rt = ctx->priority;
    if (rt < 1)  rt = 1;
    if (rt > 98) rt = 98;
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

    /* Per-thread fault recovery for HARDWARE signals (SIGSEGV/SIGFPE), e.g. a
     * raw integer divide-by-zero in IEC code. The signal handler siglongjmp's
     * here. Like the C++ exception path below, we isolate per task: release any
     * held lock, mark this worker dead so the dispatcher stops releasing it, and
     * exit this thread only. The other task threads keep running. (Bodies run on
     * private storage, so a fault is contained to this task's data; the shared
     * image stays protected by the journal + locks.) */
    if (sigsetjmp(ctx->crash_jmp, 1) != 0)
    {
        plc_task_release_locks(ctx);
        ctx->alive.store(0, std::memory_order_release);
        /* A hardware fault only fires from a scan body, so this thread held an
         * in-flight slot — release it (and wake the dispatcher if last) so the
         * completion count never leaks. */
        worker_scan_done();
        log_error("[task %s] terminated by signal %d — other tasks keep running",
                  ctx->name, ctx->crash_sig);
        return nullptr;
    }

    auto *task = static_cast<strucpp::TaskInstance *>(ctx->task_handle);

    /* Worker loop. No clock here: the GCD master-tick dispatcher owns all
     * timing. The worker blocks on its release semaphore between scans, runs
     * exactly one scan per release, and bumps `completed` so the dispatcher's
     * binary-release / overrun logic can see it finished. */
    while (true)
    {
        /* Block until the dispatcher releases us. sem_wait returns EINTR on a
         * signal (the SIGUSR1 stop-wake); just retry. On stop the dispatcher
         * posts `go` once and has already flipped plc_state, so the state check
         * below breaks us out. */
        while (sem_wait(&ctx->go) != 0 && errno == EINTR) { /* retry */ }
        if (plc_get_state() != PLC_STATE_RUNNING) break;

        /* Apply this task's scan-stable IEC time, stamped by the dispatcher at
         * release. Runs ON this worker thread so the thread_local
         * __CURRENT_TIME_NS lands where this task's body (and its FB timers)
         * read it — TIME() is constant for the whole scan and unaffected by the
         * dispatcher advancing the master clock for other tasks. */
        if (ext_strucpp_set_current_time)
            ext_strucpp_set_current_time(ctx->time_at_dispatch);

        scan_cycle_tracker_start(&ctx->tracker);

        /* Process-image model: a short locked window drains located inputs into
         * the image; the body then runs against the .so storage directly.
         * Shared globals are NOT copied to private per-task storage — each
         * global's own mutex (strucpp GlobalVar<V>) serializes concurrent
         * access inside run(), so task bodies still execute in parallel and only
         * contend on the specific globals they touch. holding_mutex gates the
         * crash-handler unlock of the image lock.
         *
         * The whole scan body runs under try/catch. On this hosted,
         * exceptions-enabled build the STruC++ runtime THROWS on an
         * unrecoverable fault (null IEC reference, array index out of bounds,
         * bad located address). If that throw escaped this pthread entry
         * function it would hit std::terminate -> abort the whole process ->
         * the daemon would bounce every task. Instead we catch it here: this one
         * task releases its lock, marks itself dead, and terminates; the
         * dispatcher then stops releasing it and the other tasks keep scanning. */
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
            for (size_t p = 0; p < task->program_count; ++p)
                task->programs[p]->run();

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
            plc_task_release_locks(ctx);
            ctx->alive.store(0, std::memory_order_release);
            worker_scan_done();   /* release the in-flight slot before exiting */
            log_error("[task %s] terminated by unhandled exception: %s — "
                      "other tasks keep running", ctx->name, e.what());
            return nullptr;
        }
        catch (...)
        {
            plc_task_release_locks(ctx);
            ctx->alive.store(0, std::memory_order_release);
            worker_scan_done();   /* release the in-flight slot before exiting */
            log_error("[task %s] terminated by unknown exception — "
                      "other tasks keep running", ctx->name);
            return nullptr;
        }

        scan_cycle_tracker_end(&ctx->tracker);

        ctx->heartbeat.store((long)time(nullptr), std::memory_order_relaxed);
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
    return nullptr;
}

/* Wake every worker, join them, and destroy the task array.
 *
 * Two callers: the normal end of the dispatcher loop, and the early-out below
 * when bring-up finished but this start is no longer the transition in flight.
 * The second one exists so that path tears its workers down instead of leaving
 * them parked on a semaphore nobody will ever post again. */
static void reap_task_threads(void)
{
    log_info("Stopping %zu PLC task thread(s)", plc_task_count);
    /* Wake every worker: post its release semaphore (breaks sem_wait) and
     * SIGUSR1 (breaks a syscall). A worker mid-scan finishes, loops to
     * sem_wait, consumes the post, observes state != RUNNING, and exits. */
    for (size_t i = 0; i < plc_task_count; ++i)
    {
        sem_post(&plc_tasks[i].go);
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
        sem_destroy(&plc_tasks[i].go);
    }
    std::free(plc_tasks);
    plc_tasks      = nullptr;
    plc_task_count = 0;
    pthread_mutex_unlock(&plc_tasks_lock);
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

    /* Retained variables. init() decides once whether retain can run here —
     * does the .so export the entry points, does the program retain anything,
     * which driver will hold the bytes — and read() asks that driver for what it
     * has for THIS program, which is also where a driver discards a previous
     * program's values. Both must follow the located-variable binding above: a
     * retained variable may also be located, and its image slot has to exist
     * before anything writes through it. Both are no-ops when retain is not in
     * play, and read() lands before the first task is released, so a new
     * program never runs a scan on the old one's state. */
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

    /* SIGUSR1 wake handler is installed once at process init (plc_main.c).
     * No per-thread re-installation here — the bootstrap thread inherits
     * the handler from the process. */

    log_info("Starting main loop");

    /* NOT where RUNNING is published. The state stays TRANSITIONING_TO_RUN until
     * the task threads exist and the dispatcher is about to release the first
     * scan -- see the publish below the "Spawned N PLC task thread(s)" log. Two
     * writes used to happen before this point (here, and in plc_set_state before
     * this thread was even created), and both claimed RUNNING while nothing was
     * scanning yet. The one here was also a deadlock: a stop landing in the
     * window before this thread was first scheduled wrote STOPPED and joined us,
     * and this line put RUNNING back -- after which no loop below would ever
     * exit, so the join never returned and the runtime refused every command for
     * the rest of the process's life. */

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

        /* If the crash happened in the dispatcher loop (after workers were
         * spawned), the workers are still alive — wake, join, and free them so
         * they aren't orphaned (which would UAF on the next load). The state is
         * already ERROR, so each worker exits after its current scan. */
        if (plc_tasks && plc_task_count)
        {
            for (size_t i = 0; i < plc_task_count; ++i)
            {
                sem_post(&plc_tasks[i].go);
                pthread_kill(plc_tasks[i].thread, SIGUSR1);
            }
            for (size_t i = 0; i < plc_task_count; ++i)
                pthread_join(plc_tasks[i].thread, nullptr);
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

    /* Sub-millisecond base tick warning. Whole-millisecond task intervals
     * always yield a GCD >= 1 ms; a base tick below that means fractional/sub-ms
     * intervals that are near-coprime, so the dispatcher must wake faster than
     * 1 kHz. We do NOT clamp (that would break the per-task time grid); we run
     * at the true GCD and rely on overrun detection if it can't keep up. */
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
        /* Housekeeping no longer rides a real task — the GCD master-tick
         * dispatcher owns time/cycle hooks/heartbeat. is_fastest_task is kept
         * only as a STATS hint (the fastest task is the tightest schedule). */
        log_info("PLC: fastest task is %s (interval=%lld ns, priority=%d)",
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

    /* RUNNING, at last, and this is the earliest point it is true: the workers
     * exist and the very next thing that happens is the dispatcher releasing the
     * first scan. Nothing observes RUNNING too early as a result -- the workers
     * are parked in sem_wait and are only ever posted from the loop below, and
     * the loop itself needs RUNNING visible to run at all.
     *
     * This is also what ends the transition claimed by plc_claim_transition, so
     * every path out of this thread from here on must land a final state: the
     * crash recovery above publishes ERROR, and the stop path publishes STOPPED
     * once teardown joins.
     *
     * Conditional, because ending a transition is only ours to do while it is
     * still the one in flight. Two paths get here otherwise: the watchdog forced
     * ERROR because this start exceeded its bound, and plc_state_manager_cleanup
     * published TRANSITIONING_TO_STOP on shutdown and is now blocked joining this
     * very thread. Publishing RUNNING in either case erases a state someone else
     * landed, and in the second it hangs the process -- the dispatcher loop below
     * would never see a non-RUNNING state and the join would never return. */
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

    /* ---------------------------------------------------------------------
     * GCD master-tick dispatcher.
     *
     * This thread is the single time authority. It wakes every base_ns on an
     * absolute deadline anchored at one t0, and on each tick:
     *   - bumps the global heartbeat (every tick, so the watchdog sees us);
     *   - computes the due set (task due iff masterTick % divisor == 0);
     *   - on a task-bearing tick: drains the journal (committing the previous
     *     frame's outputs), fires cycle_end (prev frame) then cycle_start (new
     *     frame), stamps each due+alive task's dispatch time and releases it
     *     (binary — never queues a second activation), bumps scan_counter.
     * It NEVER waits for a worker body (a long body would stall the clock); a
     * worker still running when re-due is an overrun and is simply not
     * re-released that tick. A faulted worker (alive==0) is skipped forever.
     *
     * Run at SCHED_FIFO 99 — above every worker — so the tick is never delayed
     * by a busy worker on a shared CPU.
     * --------------------------------------------------------------------- */
    {
        sched_param dsp{};
        dsp.sched_priority = 99;
        if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &dsp) != 0)
            log_warn("dispatcher SCHED_FIFO(99) failed: %s", strerror(errno));
    }

    /* Completion-signal condvar shares the CLOCK_MONOTONIC timeline with the
     * tick deadline (init-once). Reset the in-flight count so a STOP-time
     * remnant from the previous run can't make this run think a task is forever
     * outstanding. */
    pthread_once(&done_cond_once, init_done_cond);
    g_tasks_running.store(0, std::memory_order_relaxed);

    log_info("GCD master-tick dispatcher running (base tick %llu ns)",
             (unsigned long long)base_ns);

    uint64_t master_tick      = 0;
    bool     cycle_end_pending = false;   /* a frame's cycle_end not yet fired */
    timespec next_tick;
    clock_gettime(CLOCK_MONOTONIC, &next_tick);

    while (plc_get_state() == PLC_STATE_RUNNING)
    {
        /* ---- Phase B: the tick (runs at the absolute deadline) ---- */
        const int64_t master_time = (int64_t)master_tick * (int64_t)base_ns;

        /* Always: feed the global watchdog. */
        plc_heartbeat.store((long)time(nullptr), std::memory_order_relaxed);

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
            /* Worst case: the previous frame's tasks didn't all finish before
             * this tick (overrun), so its cycle_end was never retired in Phase A
             * below. Fire it now — drain to commit those outputs, then cycle_end
             * — before opening the new frame with cycle_start. This is the only
             * path where cycle_end lands on the task-wake hot path. */
            if (cycle_end_pending)
            {
                image_lock();
                image_unlock();
                if (plugin_driver) plugin_driver_cycle_end(plugin_driver);
                cycle_end_pending = false;
            }
            if (plugin_driver) plugin_driver_cycle_start(plugin_driver);

            /* Config-scope shared globals: prime the canonical storage from the
             * freshly-read input image before releasing this frame's tasks. Only
             * when quiescent (g_tasks_running == 0): if a previous frame's task
             * is still overrunning it is accessing the canonical storage under
             * its own per-global mutex, so a raw copy here would race — skip the
             * refresh for this frame (bounded staleness under overrun, matching
             * the "sync only on the guarded no-overrun path" contract). */
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
        }

        ++master_tick;

        /* ---- Phase A: wait out the period on the absolute deadline, waking
         * early to retire cycle_end the instant the frame's tasks all finish.
         *
         * pthread_cond_timedwait wakes on whichever comes first: the
         * completion signal (g_tasks_running hit 0) or the deadline. When the
         * frame is done we drain + fire cycle_end here — OFF the task-wake hot
         * path — then keep waiting (cycle_end_pending now false) purely for the
         * deadline. The predicate (g_tasks_running == 0) is checked under
         * done_mutex BEFORE waiting, so a task that finishes before we reach the
         * wait can't lose its wakeup. ---- */
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
                /* Config-scope shared globals (VAR_GLOBAL ... AT): g_tasks_running
                 * == 0 so no worker is mid-scan touching the canonical storage —
                 * journal changed output/memory globals here, before the drain
                 * applies them to the image. Safe without the per-global mutex
                 * (quiescence is the synchronization). */
                image_tables_copy_config_globals_out();
                /* Apply queued external writes/forces (debugger, OPC-UA) here:
                 * g_tasks_running == 0 so no worker is mid-scan, and we hold
                 * image_lock. Cheap no-op when nothing is queued. */
                debug_write_journal_drain();
                /* Retained values, once per scan. This window is the only place
                 * they can be read safely: g_tasks_running == 0, so no worker is
                 * inside a body mutating them — the same guarantee
                 * copy_config_globals_out relies on. The plugin decides whether
                 * these bytes are actually committed now; no-op with no store. */
                plc_retain_save();
                image_unlock();
                if (plugin_driver) plugin_driver_cycle_end(plugin_driver);
                cycle_end_pending = false;
                pthread_mutex_lock(&done_mutex);
                continue;              /* now just wait out the deadline */
            }
            int rc = pthread_cond_timedwait(&done_cond, &done_mutex, &next_tick);
            if (rc == ETIMEDOUT) break;   /* deadline reached → next tick */
            /* rc == 0 (signalled) or spurious → loop and re-check the predicate */
        }
        pthread_mutex_unlock(&done_mutex);
    }

    reap_task_threads();

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
        /* Progress, not a state change. This used to publish PLC_STATE_INIT,
         * which is now actively wrong: it overwrites TRANSITIONING_TO_RUN, so the
         * runtime would stop reporting a transition in flight and let a stop be
         * claimed while the start was still landing -- the same hole the
         * TRANSITIONING states exist to close. (It is also what made STATUS
         * flicker to INIT mid-start.) INIT survives in the enum as a startup
         * value; nothing writes it during a transition. */
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
        /* The dispatcher and its workers leave their loops on anything that is
         * not RUNNING, and the join below depends on that. A claimed stop has
         * already published TRANSITIONING_TO_STOP, which is that signal.
         *
         * Anything that is not ERROR is overwritten, not just RUNNING, because
         * this also covers the shutdown path (plc_state_manager_cleanup), which
         * tears down without claiming a transition first. On SIGTERM during a
         * start the state is TRANSITIONING_TO_RUN, and a guard that only matched
         * RUNNING published nothing at all -- so the cycle thread went on to
         * publish RUNNING and the join below never returned. Pairs with the
         * conditional publish in plc_cycle_thread: this write is what makes that
         * one decline. ERROR is left alone -- it must survive teardown. */
        pthread_mutex_lock(&state_mutex);
        if (plc_state != PLC_STATE_ERROR)
        {
            plc_state = PLC_STATE_TRANSITIONING_TO_STOP;
        }
        pthread_mutex_unlock(&state_mutex);

        pthread_join(plc_thread, NULL);

        /* Retained values: ask the store to commit whatever it is still
         * holding. Placed exactly here on purpose — AFTER the join, so no scan
         * is mid-save and the bytes are a consistent snapshot, and BEFORE
         * plugin_driver_stop(), because a plugin-backed store has to still be
         * alive to answer. A driver that already commits inside save() has
         * nothing to do; what this buys is that a clean stop loses nothing on
         * one that buffers. */
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

        /* The teardown is done, so this is the moment STOPPED becomes true.
         * plc_publish_final_state keeps ERROR if a task crashed on the way out. */
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
    pthread_mutex_lock(&state_mutex);
    PLCState s = plc_state;
    pthread_mutex_unlock(&state_mutex);
    return s;
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
    /* Land RUNNING only while the start we are completing is still the transition
     * in flight. The check and the write share one critical section: reading the
     * state and then publishing in two steps would let a stop be claimed in
     * between, and RUNNING would go down on top of it. */
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
    // Performs a transition already claimed via plc_claim_transition(), which
    // published TRANSITIONING_TO_RUN or TRANSITIONING_TO_STOP. No state is
    // written here: writing the target up front is what used to let a stop's
    // STOPPED be resurrected by a start still landing. The final state is
    // published by whoever knows the transition actually finished --
    // plc_cycle_thread for RUNNING (just before it releases the first scan) and
    // unload_plc_program for STOPPED (after the teardown joins) -- with the
    // failure paths below publishing ERROR or EMPTY.
    //
    // The current TRANSITIONING state is itself the signal the task and
    // dispatcher loops need: they run while plc_get_state() == RUNNING, so
    // TRANSITIONING_TO_STOP breaks them exactly as the old early STOPPED did.

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
    /* Let an in-flight transition finish before tearing anything down.
     *
     * Shutdown is the one state change that does not go through
     * plc_claim_transition, so it can land on top of a start that is still
     * running. Tearing down from there is not safe at any point of it: during
     * plugin bring-up load_plc_program has not assigned plc_thread yet, so the
     * join below would run on a handle that was never set; a moment later the
     * cycle thread is mid-bring-up and would have RUNNING published underneath
     * the teardown. Both disappear if the transition is allowed to land first --
     * then this is an ordinary stop of a RUNNING (or ERROR, or EMPTY) runtime.
     *
     * Bounded by the same constant the transition worker waits on, so a
     * transition that will never land cannot hold the process open forever; the
     * teardown then proceeds and does what it can. */
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
