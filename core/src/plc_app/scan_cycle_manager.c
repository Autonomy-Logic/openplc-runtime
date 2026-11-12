// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#include <inttypes.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "plc_state_manager.h"
#include "scan_cycle_manager.h"
#include "utils/utils.h"

// EWMA window (us) for time-based averages. Matches the editor's 2 s poll
// so the displayed avg tracks recent drift and does not freeze like a
// Welford historical mean would after ~10^7 cycles.
#define EWMA_TARGET_WINDOW_US 2000000

static int64_t ewma_window(int64_t interval_ns)
{
    const int64_t interval_us = interval_ns / 1000;
    const int64_t window      = (interval_us > 0) ? (EWMA_TARGET_WINDOW_US / interval_us) : 1;
    return (window < 1) ? 1 : window;
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
    tracker->stats.scan_time_min      = INT64_MAX;
    tracker->stats.cycle_time_min     = INT64_MAX;
    tracker->stats.cycle_latency_min  = INT64_MAX;
    tracker->stats.dispatch_delay_min = INT64_MAX;
    tracker->stats.wake_latency_min   = INT64_MAX;
    tracker->interval_ns              = interval_ns;
    tracker->avg_window               = ewma_window(interval_ns);

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
        record_sample((release_ns - scheduled_ns) / 1000, &s->dispatch_delay_min,
                      &s->dispatch_delay_max, &tracker->dispatch_delay_sum,
                      &tracker->dispatch_delay_samples, tracker->avg_window);
        record_sample((now_ns - release_ns) / 1000, &s->wake_latency_min, &s->wake_latency_max,
                      &tracker->wake_latency_sum, &tracker->wake_latency_samples,
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
    scan_cycle_tracker_start_at(tracker, scheduled_ns, release_ns, monotonic_ns());
}

void scan_cycle_tracker_end(scan_cycle_tracker_t *tracker)
{
    if (!tracker) return;
    scan_cycle_tracker_end_at(tracker, monotonic_ns());
}

bool scan_cycle_tracker_snapshot(scan_cycle_tracker_t *tracker, plc_timing_stats_t *out)
{
    if (!tracker || !out) return false;
    pthread_mutex_lock(&tracker->mutex);
    *out = tracker->stats;
    out->scan_time_avg      = tracker->scan_time_sum      / tracker->avg_window;
    out->cycle_time_avg     = tracker->cycle_time_sum     / tracker->avg_window;
    out->cycle_latency_avg  = tracker->cycle_latency_sum  / tracker->avg_window;
    out->dispatch_delay_avg = tracker->dispatch_delay_sum / tracker->avg_window;
    out->wake_latency_avg   = tracker->wake_latency_sum   / tracker->avg_window;
    const bool valid = tracker->scan_time_samples > 0 && tracker->cycle_time_samples > 0 &&
                       tracker->cycle_latency_samples > 0;
    pthread_mutex_unlock(&tracker->mutex);
    return valid;
}

dispatcher_stats_t plc_dispatcher_stats;

int dispatcher_stats_init(dispatcher_stats_t *ds)
{
    if (!ds) return -1;
    if (ds->initialized) return 0;
    if (init_rt_mutex(&ds->mutex) != 0) return -1;
    ds->initialized = true;
    dispatcher_stats_reset(ds, 0);
    return 0;
}

void dispatcher_stats_reset(dispatcher_stats_t *ds, int64_t base_tick_ns)
{
    if (!ds || !ds->initialized) return;
    pthread_mutex_lock(&ds->mutex);
    memset(&ds->stats, 0, sizeof(ds->stats));
    ds->stats.wake_jitter_min      = INT64_MAX;
    ds->stats.cycle_start_time_min = INT64_MAX;
    ds->wake_jitter_sum            = 0;
    ds->cycle_start_time_sum       = 0;
    ds->avg_window                 = ewma_window(base_tick_ns);
    ds->tick_seen                  = false;
    ds->cycle_start_seen           = false;
    pthread_mutex_unlock(&ds->mutex);
}

void dispatcher_stats_record_tick(dispatcher_stats_t *ds, int64_t deadline_ns, int64_t wake_ns)
{
    if (!ds || !ds->initialized) return;
    pthread_mutex_lock(&ds->mutex);
    plc_dispatcher_stats_t *s = &ds->stats;
    s->tick_count++;
    if (!ds->tick_seen)
    {
        ds->tick_seen = true;
    }
    else if (deadline_ns > 0 && wake_ns >= deadline_ns)
    {
        record_sample((wake_ns - deadline_ns) / 1000, &s->wake_jitter_min, &s->wake_jitter_max,
                      &ds->wake_jitter_sum, &s->wake_jitter_samples, ds->avg_window);
    }
    pthread_mutex_unlock(&ds->mutex);
}

void dispatcher_stats_record_cycle_start(dispatcher_stats_t *ds, int64_t begin_ns,
                                         int64_t end_ns)
{
    if (!ds || !ds->initialized) return;
    pthread_mutex_lock(&ds->mutex);
    plc_dispatcher_stats_t *s = &ds->stats;
    if (!ds->cycle_start_seen)
    {
        ds->cycle_start_seen = true;
    }
    else if (end_ns >= begin_ns)
    {
        record_sample((end_ns - begin_ns) / 1000, &s->cycle_start_time_min,
                      &s->cycle_start_time_max, &ds->cycle_start_time_sum,
                      &s->cycle_start_samples, ds->avg_window);
    }
    pthread_mutex_unlock(&ds->mutex);
}

bool dispatcher_stats_snapshot(dispatcher_stats_t *ds, plc_dispatcher_stats_t *out)
{
    if (!ds || !out || !ds->initialized) return false;
    pthread_mutex_lock(&ds->mutex);
    *out                      = ds->stats;
    out->wake_jitter_avg      = ds->wake_jitter_sum / ds->avg_window;
    out->cycle_start_time_avg = ds->cycle_start_time_sum / ds->avg_window;
    pthread_mutex_unlock(&ds->mutex);
    return true;
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
                           "\"scan_count\":%" PRId64 ","
                           "\"scan_time_min\":null,"
                           "\"scan_time_max\":null,"
                           "\"scan_time_avg\":null,"
                           "\"cycle_time_min\":null,"
                           "\"cycle_time_max\":null,"
                           "\"cycle_time_avg\":null,"
                           "\"cycle_latency_min\":null,"
                           "\"cycle_latency_max\":null,"
                           "\"cycle_latency_avg\":null,"
                           "\"overruns\":%" PRId64 ","
                           "\"dispatch_delay_min\":null,"
                           "\"dispatch_delay_max\":null,"
                           "\"dispatch_delay_avg\":null,"
                           "\"wake_latency_min\":null,"
                           "\"wake_latency_max\":null,"
                           "\"wake_latency_avg\":null}",
                           name, s->scan_count, s->overruns);
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
                           "\"overruns\":%" PRId64 ","
                           "\"dispatch_delay_min\":%" PRId64 ","
                           "\"dispatch_delay_max\":%" PRId64 ","
                           "\"dispatch_delay_avg\":%" PRId64 ","
                           "\"wake_latency_min\":%" PRId64 ","
                           "\"wake_latency_max\":%" PRId64 ","
                           "\"wake_latency_avg\":%" PRId64 "}",
                           name,
                           s->scan_count, s->scan_time_min, s->scan_time_max, s->scan_time_avg,
                           s->cycle_time_min, s->cycle_time_max, s->cycle_time_avg,
                           s->cycle_latency_min, s->cycle_latency_max, s->cycle_latency_avg,
                           s->overruns, s->dispatch_delay_min, s->dispatch_delay_max,
                           s->dispatch_delay_avg, s->wake_latency_min, s->wake_latency_max,
                           s->wake_latency_avg);
    }
    if (written < 0) return 0;
    return written;
}

/* Writes a metric value as a JSON number, or `null` when it has no sample. */
static void format_metric(char *out, size_t out_size, bool has_sample, int64_t value)
{
    if (has_sample)
        snprintf(out, out_size, "%" PRId64, value);
    else
        snprintf(out, out_size, "null");
}

/* Appends `],"dispatcher":{...}}` and the newline that close the STATS payload. */
static int append_dispatcher_tail(char *buffer, size_t buffer_size, size_t offset)
{
    plc_dispatcher_stats_t d;
    memset(&d, 0, sizeof(d));
    const bool initialized = dispatcher_stats_snapshot(&plc_dispatcher_stats, &d);
    const bool has_jitter  = initialized && d.wake_jitter_samples > 0;
    const bool has_start   = initialized && d.cycle_start_samples > 0;

    char jitter_min[24], jitter_max[24], jitter_avg[24];
    char start_min[24], start_max[24], start_avg[24];
    format_metric(jitter_min, sizeof(jitter_min), has_jitter, d.wake_jitter_min);
    format_metric(jitter_max, sizeof(jitter_max), has_jitter, d.wake_jitter_max);
    format_metric(jitter_avg, sizeof(jitter_avg), has_jitter, d.wake_jitter_avg);
    format_metric(start_min, sizeof(start_min), has_start, d.cycle_start_time_min);
    format_metric(start_max, sizeof(start_max), has_start, d.cycle_start_time_max);
    format_metric(start_avg, sizeof(start_avg), has_start, d.cycle_start_time_avg);

    const int written = snprintf(buffer + offset, buffer_size - offset,
                                 "],\"dispatcher\":{"
                                 "\"tick_count\":%" PRId64 ","
                                 "\"wake_jitter_min\":%s,"
                                 "\"wake_jitter_max\":%s,"
                                 "\"wake_jitter_avg\":%s,"
                                 "\"cycle_start_time_min\":%s,"
                                 "\"cycle_start_time_max\":%s,"
                                 "\"cycle_start_time_avg\":%s}}\n",
                                 d.tick_count, jitter_min, jitter_max, jitter_avg, start_min,
                                 start_max, start_avg);
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

    if (offset < buffer_size)
    {
        n = append_dispatcher_tail(buffer, buffer_size, offset);
        offset += (size_t)n;
    }
    return (int)offset;
}
