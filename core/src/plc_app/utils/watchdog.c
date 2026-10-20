// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "../plc_state_manager.h"
#include "../task_policy.h"
#include "log.h"
#include "utils.h"
#include "watchdog.h"

/* CLOCK_MONOTONIC ms of the last dispatcher tick; 0 while no dispatcher runs. */
static atomic_llong g_dispatch_beat_ms;
static atomic_llong g_dispatch_stall_ms;
static const char *_Atomic g_fault_context;

#define WATCHDOG_TICK_MS 100

static int64_t mono_ms(void)
{
    return monotonic_ns() / NS_PER_MS;
}

void watchdog_feed(void)
{
    atomic_store_explicit(&g_dispatch_beat_ms, mono_ms(), memory_order_relaxed);
}

void watchdog_dispatcher_started(int64_t period_ns)
{
    int64_t stall = PLC_TASK_STUCK_PERIODS * period_ns / NS_PER_MS;
    if (stall < PLC_DISPATCHER_STALL_MIN_MS)
        stall = PLC_DISPATCHER_STALL_MIN_MS;
    atomic_store_explicit(&g_dispatch_stall_ms, stall, memory_order_relaxed);
    watchdog_feed();
}

void watchdog_dispatcher_stopped(void)
{
    atomic_store_explicit(&g_dispatch_beat_ms, 0, memory_order_relaxed);
}

void watchdog_set_fault_context(const char *context)
{
    atomic_store(&g_fault_context, context);
}

void watchdog_fatal_exit(const char *reason)
{
    const char *context = atomic_load(&g_fault_context);
    char msg[512];
    snprintf(msg, sizeof(msg), "Watchdog: %s%s%s. Exiting with code %d for a safe-mode restart",
             reason, context ? " during " : "", context ? context : "", PLC_EXIT_WATCHDOG_FAULT);
    log_emergency(msg);
    int fd = open(PLC_WATCHDOG_FAULT_MARKER, O_CREAT | O_WRONLY | O_TRUNC, 0644);
    if (fd >= 0)
    {
        if (write(fd, msg, strnlen(msg, sizeof(msg))) < 0)
        {
            /* The marker exists even if empty; the boot check only tests for it. */
        }
        close(fd);
    }
    _exit(PLC_EXIT_WATCHDOG_FAULT);
}

void *watchdog_thread(void *arg)
{
    (void)arg;
    pthread_setname_np(pthread_self(), "plc_watchdog");

    struct sched_param sp = {.sched_priority = PLC_FIFO_WATCHDOG};
    int rc                = pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp);
    if (rc != 0)
        log_warn("Watchdog: SCHED_FIFO(%d) failed: %s", PLC_FIFO_WATCHDOG, strerror(rc));
    else
        log_info("Watchdog: SCHED_FIFO priority %d", PLC_FIFO_WATCHDOG);

    PLCState last_state        = PLC_STATE_STOPPED;
    int64_t state_since_ms     = 0;
    const struct timespec tick = {0, (long)(WATCHDOG_TICK_MS * NS_PER_MS)};

    while (1)
    {
        nanosleep(&tick, NULL);
        const PLCState state = plc_get_state();
        const int64_t now    = mono_ms();

        if (state != last_state)
        {
            last_state     = state;
            state_since_ms = now;
        }

        if (state == PLC_STATE_TRANSITIONING_TO_STOP)
        {
            const int64_t budget = plc_stop_budget_ms();
            if (now - state_since_ms > budget)
            {
                char reason[WATCHDOG_REASON_LEN];
                snprintf(reason, sizeof(reason), "stop did not complete within %lld ms",
                         (long long)budget);
                watchdog_fatal_exit(reason);
            }
            continue;
        }

        /* A start that never lands keeps the runtime refusing commands; release it. */
        if (state == PLC_STATE_TRANSITIONING_TO_RUN)
        {
            if (now - state_since_ms > PLC_TRANSITION_STUCK_TIMEOUT_MS)
            {
                log_error("Watchdog: start stuck in progress for over %d s — forcing ERROR",
                          PLC_TRANSITION_STUCK_TIMEOUT_MS / 1000);
                plc_force_error_state();
            }
            continue;
        }

        if (state == PLC_STATE_RUNNING)
        {
            const int64_t first_since = plc_first_scan_pending_since_ns();
            if (first_since != 0 &&
                monotonic_ns() - first_since > (int64_t)PLC_FIRST_SCAN_TIMEOUT_MS * NS_PER_MS)
                plc_request_first_scan_trip();

            const int64_t beat  = atomic_load_explicit(&g_dispatch_beat_ms, memory_order_relaxed);
            const int64_t stall = atomic_load_explicit(&g_dispatch_stall_ms, memory_order_relaxed);
            if (beat != 0 && now - beat > stall)
            {
                char reason[WATCHDOG_REASON_LEN];
                snprintf(reason, sizeof(reason), "dispatcher stalled, no tick for %lld ms",
                         (long long)(now - beat));
                watchdog_fatal_exit(reason);
            }
        }
    }

    return NULL;
}

int watchdog_init(void)
{
    pthread_t wd_thread;
    if (pthread_create(&wd_thread, NULL, watchdog_thread, NULL) != 0)
    {
        log_error("Failed to create watchdog thread");
        return -1;
    }
    pthread_detach(wd_thread); // Detach the thread to avoid memory leaks
    return 0;
}
