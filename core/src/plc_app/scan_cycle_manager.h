// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef SCAN_CYCLE_MANAGER_H
#define SCAN_CYCLE_MANAGER_H

#include <pthread.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct
{
    int64_t scan_time_min;
    int64_t scan_time_max;
    int64_t scan_time_avg;

    int64_t cycle_time_min;
    int64_t cycle_time_max;
    int64_t cycle_time_avg;

    int64_t cycle_latency_min;
    int64_t cycle_latency_max;
    int64_t cycle_latency_avg;

    int64_t scan_count;
    int64_t overruns;
} plc_timing_stats_t;

/* Per-task scan-cycle tracker. Each IEC task thread owns one and is the
 * exclusive writer; the snapshot reader (the STATS handler) acquires the
 * mutex briefly to copy out a consistent view.
 *
 * `interval_ns` is this task's scheduling period, used to project the
 * next-expected start time so latency = actual_start - expected_start
 * stays meaningful across tasks with different periods.
 *
 * Averages use a time-based EWMA targeting a wall-clock window that
 * matches the editor's polling cadence (so the displayed avg doesn't
 * decorrelate between consecutive polls). Each `*_sum` field holds an
 * approximate sum of the last `avg_window` samples; the per-cycle update
 * is `sum += sample - sum/avg_window` and the read is `avg = sum/avg_window`.
 * This accumulator-then-divide form avoids the integer-precision stall
 * of the incremental `avg += (sample - avg)/N` shape when delta < N. */
typedef struct
{
    plc_timing_stats_t stats;
    int64_t            scan_time_sum;
    int64_t            cycle_time_sum;
    int64_t            cycle_latency_sum;
    int64_t            avg_window;        /* N = target_window_us / interval_us, >= 1 */
    uint64_t           expected_start_us;
    uint64_t           last_start_us;
    int64_t            interval_ns;
    pthread_mutex_t    mutex;
} scan_cycle_tracker_t;

/* Initialise a tracker for a task with the given scheduling interval.
 * Call before the task thread enters its scan loop. Returns 0 on
 * success, non-zero on mutex-init failure. */
int scan_cycle_tracker_init(scan_cycle_tracker_t *tracker, int64_t interval_ns);

/* Release the tracker's mutex. */
void scan_cycle_tracker_cleanup(scan_cycle_tracker_t *tracker);

/* Mark the start / end of a scan body on this tracker's task. Must only
 * be called from the task thread that owns the tracker (the mutex is
 * for the snapshot reader, not for cross-task synchronisation). */
void scan_cycle_tracker_start(scan_cycle_tracker_t *tracker);
void scan_cycle_tracker_end(scan_cycle_tracker_t *tracker);

/* Atomically copy the tracker's stats. Returns true if the task has
 * completed at least one full cycle (snapshot is meaningful). */
bool scan_cycle_tracker_snapshot(scan_cycle_tracker_t *tracker, plc_timing_stats_t *out);

/* Format the multi-task STATS response. Walks plc_tasks[] and emits
 * `STATS:{"tasks":[{...},{...}]}`. Plugins that drive their own threads
 * (e.g. the EtherCAT bus thread) report timing through their own
 * dedicated channels (in EtherCAT's case the
 * `/api/discovery/ethercat/{runtime-status,diagnostics}` routes), not
 * through this STATS feed. Returns chars written excluding the null
 * terminator. */
int format_timing_stats_response(char *buffer, size_t buffer_size);

#ifdef __cplusplus
}
#endif

#endif // SCAN_CYCLE_MANAGER_H
