// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#include <inttypes.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "plc_state_manager.h"
#include "scan_cycle_manager.h"
#include "utils/utils.h"

// CLOCK_MONOTONIC matches clock_nanosleep's clock in sleep_until().
// CLOCK_MONOTONIC_RAW would drift against the sleep clock through NTP
// slew and cause false overruns after ~30-60 minutes.
#define OPENPLC_CLOCK CLOCK_MONOTONIC

// EWMA window (us) for time-based averages. Matches the editor's 2 s poll
// so the displayed avg tracks recent drift and does not freeze like a
// Welford historical mean would after ~10^7 cycles.
#define EWMA_TARGET_WINDOW_US 2000000

static int64_t ts_now_ns(void)
{
    struct timespec ts;
    clock_gettime(OPENPLC_CLOCK, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + (int64_t)ts.tv_nsec;
}

static void record_sample(int64_t sample_us, int64_t *min, int64_t *max, int64_t *sum,
                          int64_t *samples, int64_t avg_window)
{
    if (sample_us < *min) *min = sample_us;
    if (sample_us > *max) *max = sample_us;
    *sum += sample_us - *sum / avg_window;
    ++*samples;
}

int scan_cycle_tracker_init(scan_cycle_tracker_t *tracker, int64_t interval_ns)
{
    if (!tracker) return -1;
    memset(tracker, 0, sizeof(*tracker));
    tracker->stats.scan_time_min     = INT64_MAX;
    tracker->stats.cycle_time_min    = INT64_MAX;
    tracker->stats.cycle_latency_min = INT64_MAX;
    tracker->interval_ns             = interval_ns;

    int64_t interval_us = interval_ns / 1000;
    tracker->avg_window =
        (interval_us > 0) ? (EWMA_TARGET_WINDOW_US / interval_us) : 1;
    if (tracker->avg_window < 1) tracker->avg_window = 1;

    return init_rt_mutex(&tracker->mutex);
}

void scan_cycle_tracker_cleanup(scan_cycle_tracker_t *tracker)
{
    if (!tracker) return;
    pthread_mutex_destroy(&tracker->mutex);
}

void scan_cycle_tracker_start_at(scan_cycle_tracker_t *tracker, int64_t scheduled_ns,
                                 int64_t release_ns, int64_t now_ns)
{
    if (!tracker) return;

    pthread_mutex_lock(&tracker->mutex);

    plc_timing_stats_t *s = &tracker->stats;
    s->scan_count++;
    const bool stamps_valid =
        scheduled_ns > 0 && scheduled_ns <= release_ns && release_ns <= now_ns;

    if (s->scan_count >= 3)
    {
        record_sample((now_ns - tracker->last_start_ns) / 1000, &s->cycle_time_min,
                      &s->cycle_time_max, &tracker->cycle_time_sum, &tracker->cycle_time_samples,
                      tracker->avg_window);
    }
    if (s->scan_count >= 2 && stamps_valid)
    {
        record_sample((now_ns - scheduled_ns) / 1000, &s->cycle_latency_min, &s->cycle_latency_max,
                      &tracker->cycle_latency_sum, &tracker->cycle_latency_samples,
                      tracker->avg_window);
    }

    tracker->last_start_ns     = now_ns;
    tracker->last_scheduled_ns = scheduled_ns;
    tracker->last_stamps_valid = stamps_valid;

    pthread_mutex_unlock(&tracker->mutex);
}

void scan_cycle_tracker_end_at(scan_cycle_tracker_t *tracker, int64_t now_ns)
{
    if (!tracker) return;

    pthread_mutex_lock(&tracker->mutex);

    plc_timing_stats_t *s = &tracker->stats;
    if (s->scan_count >= 2)
    {
        record_sample((now_ns - tracker->last_start_ns) / 1000, &s->scan_time_min,
                      &s->scan_time_max, &tracker->scan_time_sum, &tracker->scan_time_samples,
                      tracker->avg_window);
        if (tracker->last_stamps_valid &&
            now_ns - tracker->last_scheduled_ns > tracker->interval_ns)
        {
            s->overruns++;
        }
    }

    pthread_mutex_unlock(&tracker->mutex);
}

void scan_cycle_tracker_start(scan_cycle_tracker_t *tracker, int64_t scheduled_ns,
                              int64_t release_ns)
{
    if (!tracker) return;
    scan_cycle_tracker_start_at(tracker, scheduled_ns, release_ns, ts_now_ns());
}

void scan_cycle_tracker_end(scan_cycle_tracker_t *tracker)
{
    if (!tracker) return;
    scan_cycle_tracker_end_at(tracker, ts_now_ns());
}

bool scan_cycle_tracker_snapshot(scan_cycle_tracker_t *tracker, plc_timing_stats_t *out)
{
    if (!tracker || !out) return false;
    pthread_mutex_lock(&tracker->mutex);
    *out = tracker->stats;
    out->scan_time_avg     = tracker->scan_time_sum     / tracker->avg_window;
    out->cycle_time_avg    = tracker->cycle_time_sum    / tracker->avg_window;
    out->cycle_latency_avg = tracker->cycle_latency_sum / tracker->avg_window;
    const bool valid = tracker->scan_time_samples > 0 && tracker->cycle_time_samples > 0 &&
                       tracker->cycle_latency_samples > 0;
    pthread_mutex_unlock(&tracker->mutex);
    return valid;
}

// Forward declaration to avoid pulling plc_state_manager.h into the
// header. PlcTaskCtx exposes `name` and a `tracker` field.
extern PlcTaskCtx *plc_tasks;
extern size_t      plc_task_count;

static int append_task_entry(char *buffer, size_t buffer_size, size_t offset,
                             const char *name, const plc_timing_stats_t *s,
                             bool valid)
{
    int written;
    if (!valid)
    {
        written = snprintf(buffer + offset, buffer_size - offset,
                           "{\"name\":\"%s\","
                           "\"scan_count\":0,"
                           "\"scan_time_min\":null,"
                           "\"scan_time_max\":null,"
                           "\"scan_time_avg\":null,"
                           "\"cycle_time_min\":null,"
                           "\"cycle_time_max\":null,"
                           "\"cycle_time_avg\":null,"
                           "\"cycle_latency_min\":null,"
                           "\"cycle_latency_max\":null,"
                           "\"cycle_latency_avg\":null,"
                           "\"overruns\":0}",
                           name);
    }
    else
    {
        written = snprintf(buffer + offset, buffer_size - offset,
                           "{\"name\":\"%s\","
                           "\"scan_count\":%" PRId64 ","
                           "\"scan_time_min\":%" PRId64 ","
                           "\"scan_time_max\":%" PRId64 ","
                           "\"scan_time_avg\":%" PRId64 ","
                           "\"cycle_time_min\":%" PRId64 ","
                           "\"cycle_time_max\":%" PRId64 ","
                           "\"cycle_time_avg\":%" PRId64 ","
                           "\"cycle_latency_min\":%" PRId64 ","
                           "\"cycle_latency_max\":%" PRId64 ","
                           "\"cycle_latency_avg\":%" PRId64 ","
                           "\"overruns\":%" PRId64 "}",
                           name,
                           s->scan_count, s->scan_time_min, s->scan_time_max, s->scan_time_avg,
                           s->cycle_time_min, s->cycle_time_max, s->cycle_time_avg,
                           s->cycle_latency_min, s->cycle_latency_max, s->cycle_latency_avg,
                           s->overruns);
    }
    if (written < 0) return 0;
    return written;
}

int format_timing_stats_response(char *buffer, size_t buffer_size)
{
    if (buffer == NULL || buffer_size == 0) return 0;

    size_t offset = 0;
    int    n;

    n = snprintf(buffer + offset, buffer_size - offset, "STATS:{\"tasks\":[");
    if (n < 0) return 0;
    offset += (size_t)n;

    /* Hold plc_tasks_reader_lock for the whole iteration: a plugin STOP
     * can fire mid-loop and free plc_tasks[]. The lock makes the alloc/
     * free critical section in plc_cycle_thread mutually exclusive. */
    plc_tasks_reader_lock();
    for (size_t i = 0; i < plc_task_count; ++i)
    {
        if (i > 0)
        {
            if (offset >= buffer_size) break;
            buffer[offset++] = ',';
        }
        plc_timing_stats_t snap;
        bool valid = scan_cycle_tracker_snapshot(&plc_tasks[i].tracker, &snap);
        n = append_task_entry(buffer, buffer_size, offset, plc_tasks[i].name, &snap, valid);
        if (n <= 0) break;
        offset += (size_t)n;
        if (offset >= buffer_size) break;
    }
    plc_tasks_reader_unlock();

    n = snprintf(buffer + offset, buffer_size - offset, "]}\n");
    if (n > 0) offset += (size_t)n;
    return (int)offset;
}
