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

/* Per-task tracker. The IEC task thread is the sole writer; STATS
 * briefly takes the mutex to snapshot. `interval_ns` projects
 * next-expected start for latency. EWMA sum form (sum += sample -
 * sum/N; avg = sum/N) avoids integer-precision stalls when delta<N. */
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

/* Format multi-task STATS response: `STATS:{"tasks":[{...}]}`. Plugins
 * that drive their own threads report timing through their own
 * channels, not through STATS. Returns chars written (no NUL). */
int format_timing_stats_response(char *buffer, size_t buffer_size);

#ifdef __cplusplus
}
#endif

#endif // SCAN_CYCLE_MANAGER_H
