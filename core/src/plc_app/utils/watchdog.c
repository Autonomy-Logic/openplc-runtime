// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

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

#define WATCHDOG_TICK_MS 100

static int64_t mono_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

void watchdog_feed(void)
{
    atomic_store_explicit(&g_dispatch_beat_ms, mono_ms(), memory_order_relaxed);
}

void watchdog_dispatcher_started(int64_t period_ns)
{
    int64_t stall = PLC_TASK_STUCK_PERIODS * period_ns / 1000000;
    if (stall < PLC_DISPATCHER_STALL_MIN_MS)
        stall = PLC_DISPATCHER_STALL_MIN_MS;
    atomic_store(&g_dispatch_stall_ms, stall);
    watchdog_feed();
}

void watchdog_dispatcher_stopped(void)
{
    atomic_store(&g_dispatch_beat_ms, 0);
}

void watchdog_fatal_exit(const char *reason)
{
    char msg[512];
    snprintf(msg, sizeof(msg), "Watchdog: %s. Exiting with code %d for a safe-mode restart", reason,
             PLC_EXIT_WATCHDOG_FAULT);
    log_emergency(msg);
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

    PLCState watched_state     = PLC_STATE_STOPPED;
    int64_t transition_since   = 0;
    const struct timespec tick = {0, WATCHDOG_TICK_MS * 1000000L};

    while (1)
    {
        nanosleep(&tick, NULL);
        const PLCState state = plc_get_state();
        const int64_t now    = mono_ms();

        if (state != watched_state)
        {
            watched_state    = state;
            transition_since = now;
        }

        if (state == PLC_STATE_TRANSITIONING_TO_STOP)
        {
            const int64_t budget = plc_stop_budget_ms();
            if (now - transition_since > budget)
            {
                char reason[160];
                snprintf(reason, sizeof(reason), "stop did not complete within %lld ms",
                         (long long)budget);
                watchdog_fatal_exit(reason);
            }
            continue;
        }

        /* A start that never lands keeps the runtime refusing commands; release it. */
        if (state == PLC_STATE_TRANSITIONING_TO_RUN)
        {
            if (now - transition_since > PLC_TRANSITION_STUCK_TIMEOUT_MS)
            {
                log_error("Watchdog: start stuck in progress for over %d s — forcing ERROR",
                          PLC_TRANSITION_STUCK_TIMEOUT_MS / 1000);
                plc_force_error_state();
            }
            continue;
        }

        if (state == PLC_STATE_RUNNING)
        {
            const int64_t beat  = atomic_load_explicit(&g_dispatch_beat_ms, memory_order_relaxed);
            const int64_t stall = atomic_load(&g_dispatch_stall_ms);
            if (beat != 0 && now - beat > stall)
            {
                char reason[160];
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
