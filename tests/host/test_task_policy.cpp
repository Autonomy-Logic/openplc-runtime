// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

// Host test: IEC TASK priority to SCHED_FIFO mapping and busy-tick overrun accounting.

#include "task_policy.h"

#include <climits>
#include <cstdio>

static int g_failures = 0;

#define CHECK(cond, what)                                                                          \
    do                                                                                             \
    {                                                                                              \
        if (!(cond))                                                                               \
        {                                                                                          \
            std::printf("  FAIL  %s\n", what);                                                     \
            g_failures++;                                                                          \
        }                                                                                          \
    } while (0)

int main()
{
    std::printf("task_policy: IEC priority mapping\n");
    bool clamped = true;

    CHECK(plc_task_fifo_priority(0, &clamped) == 49 && !clamped, "IEC 0 -> FIFO 49");
    CHECK(plc_task_fifo_priority(1, &clamped) == 48 && !clamped, "IEC 1 -> FIFO 48");
    CHECK(plc_task_fifo_priority(48, &clamped) == 1 && !clamped, "IEC 48 -> FIFO 1");
    CHECK(plc_task_fifo_priority(-1, &clamped) == 49 && clamped, "IEC -1 clamps to FIFO 49");
    CHECK(plc_task_fifo_priority(-1000, &clamped) == 49 && clamped, "IEC -1000 clamps to 49");
    CHECK(plc_task_fifo_priority(49, &clamped) == 1 && clamped, "IEC 49 clamps to FIFO 1");
    CHECK(plc_task_fifo_priority(1000, &clamped) == 1 && clamped, "IEC 1000 clamps to FIFO 1");
    CHECK(plc_task_fifo_priority(10, nullptr) == 39, "NULL clamped flag accepted");

    for (int p = -100; p <= 100; ++p)
    {
        int f = plc_task_fifo_priority(p, nullptr);
        if (f < 1 || f > PLC_FIFO_TASK_MAX || f >= PLC_FIFO_DISPATCHER)
        {
            std::printf("  FAIL  IEC %d -> FIFO %d out of task range\n", p, f);
            g_failures++;
        }
    }
    CHECK(PLC_FIFO_WATCHDOG > PLC_FIFO_DISPATCHER, "watchdog above dispatcher");
    CHECK(PLC_FIFO_DISPATCHER > PLC_FIFO_TASK_MAX, "dispatcher above every task");

    std::printf("task_policy: busy-tick accounting\n");
    CHECK(!plc_task_skip_is_counted(0), "skip during the first scan is not counted");
    CHECK(plc_task_skip_is_counted(1), "skip after one completed scan is counted");
    CHECK(plc_task_skip_is_counted(LONG_MAX), "skip after LONG_MAX completed scans is counted");
    CHECK(!plc_task_skip_is_counted(-1), "negative completed count is not counted");

    if (g_failures == 0)
        std::printf("all cases passed\n");
    return g_failures == 0 ? 0 : 1;
}
