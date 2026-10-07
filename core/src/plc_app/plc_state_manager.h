// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef PLC_STATE_MANAGER_H
#define PLC_STATE_MANAGER_H

#include "plcapp_manager.h"
#include "scan_cycle_manager.h"
#include <pthread.h>
#include <semaphore.h>
#include <setjmp.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>

/* Dual-language atomic types: C uses <stdatomic.h>'s _Atomic-typedef
 * forms; C++ uses std::atomic<T>. Both have the same memory layout, so
 * a struct that contains plc_atomic_long_t compiles in both languages
 * and the linker treats it as the same storage. */
#ifdef __cplusplus
#include <atomic>
typedef std::atomic<long>               plc_atomic_long_t;
typedef std::atomic<uint_least64_t>     plc_atomic_u64_t;
typedef std::atomic<int_least64_t>      plc_atomic_i64_t;
extern "C" {
#else
#include <stdatomic.h>
typedef atomic_long                     plc_atomic_long_t;
typedef atomic_uint_least64_t           plc_atomic_u64_t;
typedef atomic_int_least64_t            plc_atomic_i64_t;
#endif

/**
 * Runtime states.
 *
 * The two TRANSITIONING values are APPENDED, never inserted: the first five are
 * wire-visible (FC 0x46 reports them, and plugin_get_plc_state maps them for
 * vendor status indicators), so renumbering would quietly change what boards
 * report.
 *
 * RUNNING means running -- it is published at the moment the dispatcher is about
 * to release the first scan, not when a start is requested. Everything in
 * between is a TRANSITIONING state, and because both compare unequal to
 * PLC_STATE_RUNNING, every loop that gates on RUNNING treats them correctly
 * without modification: a stop's teardown still gets its exit signal, and a
 * half-started PLC cannot scan.
 *
 * The direction is carried (rather than one flat TRANSITIONING) so that any code
 * found to need the target state before it lands can test for the specific
 * direction instead of having to reintroduce a premature RUNNING.
 */
typedef enum
{
    PLC_STATE_INIT,
    PLC_STATE_RUNNING,
    PLC_STATE_STOPPED,
    PLC_STATE_ERROR,
    PLC_STATE_EMPTY,
    PLC_STATE_TRANSITIONING_TO_RUN,
    PLC_STATE_TRANSITIONING_TO_STOP
} PLCState;

typedef struct PlcTaskCtx
{
    size_t                idx;                /* index into plc_tasks[] */
    int64_t               interval_ns;
    int                   priority;           /* IEC TASK priority, mapped to SCHED_FIFO */
    uint64_t              cpu_affinity_mask;  /* 0 = no pinning, kernel decides */
    bool                  is_fastest_task;    /* retained for STATS; housekeeping is on the dispatcher */
    void                 *task_handle;        /* opaque strucpp::TaskInstance* */
    pthread_t             thread;
    char                  name[32];

    
    sem_t                 go;
    uint64_t              divisor;
    int64_t               time_at_dispatch;
    plc_atomic_long_t     alive;
    plc_atomic_long_t     released;
    plc_atomic_long_t     completed;
    plc_atomic_long_t     overrun_count;
    plc_atomic_long_t     stuck_ticks;     /* consecutive due ticks found still in one scan */
    plc_atomic_long_t     exited;          /* 1 once the thread function has returned */
    plc_atomic_i64_t      release_ns;      /* CLOCK_MONOTONIC time of the last release */

    sigjmp_buf            crash_jmp;
    volatile sig_atomic_t crash_sig;
    volatile sig_atomic_t holding_mutex;   /* image-tables mutex held (crash unlock) */
    volatile sig_atomic_t in_body;         /* inside IEC program code, where the abort may jump out */

    plc_atomic_u64_t      local_tick;

    /* Per-task scan/cycle/latency tracker. Each task thread updates its
     * own tracker around its scan body; the STATS handler walks all
     * trackers to emit per-task entries. Replaces the old single global
     * plc_timing_stats from scan_cycle_manager.c which only tracked the
     * fastest task. */
    scan_cycle_tracker_t  tracker;
} PlcTaskCtx;

extern PlcTaskCtx *plc_tasks;
extern size_t      plc_task_count;

/* Lifecycle lock for plc_tasks / plc_task_count.
 *
 * The plc_cycle_thread owns the array — it allocates after walking the
 * configuration (load) and frees after joining task threads (stop).
 * Concurrently, the unix-socket thread services STATS by iterating the
 * array under format_timing_stats_response. The TRANSITIONING state gates new
 * commands but doesn't bracket an in-flight STATS call: a plugin-initiated
 * stop can fire mid-iteration, free plc_tasks, and the STATS reader
 * dereferences freed memory.
 *
 * Readers (STATS) hold this lock for the duration of the iteration.
 * The writer (plc_cycle_thread) holds it while allocating, while
 * publishing the count, and while freeing. STOP itself doesn't need the
 * lock: task threads exit via plc_state observation; the lock only
 * brackets the array swap. Held briefly enough that adding latency to
 * STATS during a STOP transition is acceptable. */
void plc_tasks_reader_lock(void);
void plc_tasks_reader_unlock(void);

/**
 * @brief Get the current PLC state.
 * @return PLCState The current PLC state
 */
PLCState plc_get_state(void);

/**
 * @brief Body of a transition that has ALREADY been claimed. Not a setter.
 *
 * PRECONDITION: plc_claim_transition(new_state) returned true, so a TRANSITIONING
 * state is current. Calling this without a claim tears the program down with no
 * TRANSITIONING_TO_STOP for the scan loops to observe and then publishes a final
 * state nobody claimed, corrupting the interlock the TRANSITIONING states are.
 *
 * Writes no state of its own: the landing is published by whoever knows the work
 * finished -- plc_cycle_thread for RUNNING, unload_plc_program for STOPPED -- with
 * the failure paths here publishing ERROR or EMPTY.
 *
 * Go through plc_begin_transition() instead. It is the only intended entry point
 * for a state change and it does the claiming for you.
 *
 * @param new_state PLC_STATE_RUNNING or PLC_STATE_STOPPED
 * @return true if the transition body completed without error
 */
bool plc_set_state(PLCState new_state);

/**
 * @brief Claim the right to transition, publishing the matching TRANSITIONING state.
 *
 * The single arbiter for "may this change start". Under the state lock: a request
 * arriving while a TRANSITIONING state is current is DROPPED -- you cannot change
 * state in the middle of changing state -- and so is a request for the state the
 * runtime is already in. Otherwise the direction is published and the caller owns
 * the transition until it publishes a final state.
 *
 * @param target PLC_STATE_RUNNING or PLC_STATE_STOPPED
 * @return true when the transition is claimed and the caller must complete it
 */
bool plc_claim_transition(PLCState target);

/**
 * @brief Publish the state a transition landed on: RUNNING, STOPPED, ERROR or EMPTY.
 *
 * Ends the transition. ERROR is sticky against STOPPED: a task that crashed while
 * a stop was tearing down has already recorded the more important fact, and the
 * teardown finishing must not paper over it.
 */
void plc_publish_final_state(PLCState final_state);

/**
 * @brief Publish RUNNING, but only if the start being completed is still in flight.
 *
 * Same landing as plc_publish_final_state(PLC_STATE_RUNNING), refused when the
 * current state is not TRANSITIONING_TO_RUN. Ending a transition is only the
 * caller's to do while it owns it: a watchdog-forced ERROR or a shutdown that
 * published TRANSITIONING_TO_STOP has taken ownership away, and overwriting either
 * with RUNNING erases a landing (or, on the shutdown path, hangs the join waiting
 * for a state that is never written again).
 *
 * @return true when RUNNING was published and the caller may start scanning
 */
bool plc_publish_running_if_claimed(void);

/**
 * @brief Longest a stop may take before the watchdog exits the process.
 *
 * PLC_TASK_STUCK_PERIODS times the longest task interval of the loaded program,
 * plus the outputs-off settle time and PLC_STOP_TEARDOWN_ALLOWANCE_MS.
 */
int64_t plc_stop_budget_ms(void);

/**
 * @brief Drive every output to 0 with no program loaded.
 *
 * Used on the safe-mode boot after a watchdog exit: loads and starts the configured
 * plugins (including VPP board plugins), runs the same outputs-off sequence as a
 * stop, then stops the plugins again. The caller must hold a claimed stop.
 *
 * @return true when the plugins were brought up and the zeroed outputs were pushed
 */
bool plc_outputs_off_without_program(void);

/**
 * @brief CLOCK_MONOTONIC release time (ns) of the oldest first scan still running, or 0.
 *
 * Read by the watchdog to bound first scans, which the dispatcher does not count.
 */
int64_t plc_first_scan_pending_since_ns(void);

/**
 * @brief Ask the dispatcher to trip on the oldest first scan still running.
 *
 * Called by the watchdog when that scan exceeds PLC_FIRST_SCAN_TIMEOUT_MS. The
 * dispatcher then stops the PLC exactly as for a stuck task.
 */
void plc_request_first_scan_trip(void);

/** @brief True while a transition is in flight (either direction). */
bool plc_state_is_transitioning(void);

/* transition_worker stops waiting for a landing at PLC_TRANSITION_LANDING_TIMEOUT_MS.
 * The watchdog forces ERROR on a start stuck past PLC_TRANSITION_STUCK_TIMEOUT_MS; a
 * stuck stop is bounded by plc_stop_budget_ms() and ends in watchdog_fatal_exit(). */
#define PLC_TRANSITION_LANDING_TIMEOUT_MS 90000
#define PLC_TRANSITION_STUCK_TIMEOUT_MS   (PLC_TRANSITION_LANDING_TIMEOUT_MS + 30000)

/**
 * @brief Cleanup the PLC state manager and unloads the plugin manager.
 * @return void
 */
void plc_state_manager_cleanup(void);

/**
 * @brief Force the PLC into ERROR state from any thread.
 *
 * This is intended for the watchdog thread to transition the PLC to ERROR
 * state without triggering program load/unload side effects (unlike plc_set_state).
 */
void plc_force_error_state(void);

/**
 * @brief Get the signal number that caused the last PLC crash.
 * @return The signal number (e.g. SIGFPE, SIGSEGV), or 0 if no crash occurred
 */
int plc_get_crash_signal(void);

#ifdef __cplusplus
}
#endif

#endif // PLC_STATE_MANAGER_H
