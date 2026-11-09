// SPDX-License-Identifier: GPL-3.0-only
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

/* Per-task tracker, written by the task thread only; STATS takes the mutex to snapshot. Latency
 * is measured against the scheduled tick. EWMA sum form: sum += sample - sum/N, avg = sum/N. */
typedef struct
{
    plc_timing_stats_t stats;
    int64_t            scan_time_sum;
    int64_t            cycle_time_sum;
    int64_t            cycle_latency_sum;
    int64_t            avg_window;        /* N = target_window_us / interval_us, >= 1 */
    int64_t            interval_ns;
    int64_t            last_start_ns;
    int64_t            last_scheduled_ns;
    bool               last_stamps_valid;
    int64_t            scan_time_samples;
    int64_t            cycle_time_samples;
    int64_t            cycle_latency_samples;
    pthread_mutex_t    mutex;
} scan_cycle_tracker_t;

/* Initialise a tracker for a task with the given scheduling interval.
 * Call before the task thread enters its scan loop. Returns 0 on
 * success, non-zero on mutex-init failure. */
int scan_cycle_tracker_init(scan_cycle_tracker_t *tracker, int64_t interval_ns);

/* Release the tracker's mutex. */
void scan_cycle_tracker_cleanup(scan_cycle_tracker_t *tracker);

/* Mark the start / end of a scan body, reading CLOCK_MONOTONIC. Task thread only. `scheduled_ns`
 * is the deadline of the tick that released the scan, `release_ns` the release time, both in ns. */
void scan_cycle_tracker_start(scan_cycle_tracker_t *tracker, int64_t scheduled_ns,
                              int64_t release_ns);
void scan_cycle_tracker_end(scan_cycle_tracker_t *tracker);

/* Same as scan_cycle_tracker_start / _end, with the current CLOCK_MONOTONIC time given in ns. */
void scan_cycle_tracker_start_at(scan_cycle_tracker_t *tracker, int64_t scheduled_ns,
                                 int64_t release_ns, int64_t now_ns);
void scan_cycle_tracker_end_at(scan_cycle_tracker_t *tracker, int64_t now_ns);

/* Atomically copy the tracker's stats. Returns true once scan time, cycle
 * time and latency each hold at least one sample. */
bool scan_cycle_tracker_snapshot(scan_cycle_tracker_t *tracker, plc_timing_stats_t *out);

/* Format multi-task STATS response: `STATS:{"tasks":[{...}]}`. Plugins
 * that drive their own threads report timing through their own
 * channels, not through STATS. Returns chars written (no NUL). */
int format_timing_stats_response(char *buffer, size_t buffer_size);

#ifdef __cplusplus
}
#endif

#endif // SCAN_CYCLE_MANAGER_H
