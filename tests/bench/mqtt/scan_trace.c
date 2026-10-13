// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Benchmark-only scan trace, linked into plc_main with
// -Wl,--wrap=scan_cycle_tracker_start,--wrap=scan_cycle_tracker_end.
// Per scan the task thread stores (start us since the first scan, body ns);
// a background thread appends new pairs to $OPENPLC_SCAN_TRACE once per second.

#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "scan_cycle_manager.h"

#define TRACE_CAPACITY (1u << 22)

void __real_scan_cycle_tracker_start(scan_cycle_tracker_t *tracker);
void __real_scan_cycle_tracker_end(scan_cycle_tracker_t *tracker);

static uint32_t trace_samples[TRACE_CAPACITY][2];
static atomic_uint trace_count;
static _Thread_local struct timespec trace_started;
static struct timespec trace_origin;
static atomic_int trace_origin_set;

static uint64_t elapsed_ns(const struct timespec *from, const struct timespec *to)
{
    return (uint64_t)(to->tv_sec - from->tv_sec) * 1000000000ull + (uint64_t)to->tv_nsec -
           (uint64_t)from->tv_nsec;
}

void __wrap_scan_cycle_tracker_start(scan_cycle_tracker_t *tracker)
{
    __real_scan_cycle_tracker_start(tracker);
    clock_gettime(CLOCK_MONOTONIC, &trace_started);
    if (!atomic_load_explicit(&trace_origin_set, memory_order_relaxed))
    {
        trace_origin = trace_started;
        atomic_store_explicit(&trace_origin_set, 1, memory_order_release);
    }
}

void __wrap_scan_cycle_tracker_end(scan_cycle_tracker_t *tracker)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint64_t ns        = elapsed_ns(&trace_started, &now);
    unsigned int index = atomic_load_explicit(&trace_count, memory_order_relaxed);
    if (index < TRACE_CAPACITY)
    {
        trace_samples[index][0] = (uint32_t)(elapsed_ns(&trace_origin, &trace_started) / 1000u);
        trace_samples[index][1] = ns > UINT32_MAX ? UINT32_MAX : (uint32_t)ns;
        atomic_store_explicit(&trace_count, index + 1, memory_order_release);
    }
    __real_scan_cycle_tracker_end(tracker);
}

static void *trace_writer(void *arg)
{
    FILE *out            = (FILE *)arg;
    unsigned int written = 0;
    for (;;)
    {
        sleep(1);
        unsigned int count = atomic_load_explicit(&trace_count, memory_order_acquire);
        if (count > written)
        {
            size_t done =
                fwrite(&trace_samples[written], sizeof(trace_samples[0]), count - written, out);
            written += (unsigned int)done;
            if (fflush(out) != 0)
                return NULL;
        }
    }
    return NULL;
}

__attribute__((constructor)) static void trace_init(void)
{
    const char *path = getenv("OPENPLC_SCAN_TRACE");
    if (path == NULL || path[0] == '\0')
        return;
    FILE *out = fopen(path, "ab");
    if (out == NULL)
    {
        fprintf(stderr, "scan trace: cannot open %s (errno %d)\n", path, errno);
        return;
    }
    pthread_t thread;
    if (pthread_create(&thread, NULL, trace_writer, out) != 0)
    {
        fclose(out);
        return;
    }
    pthread_detach(thread);
}
