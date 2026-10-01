// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef TASK_POLICY_H
#define TASK_POLICY_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C"
{
#endif

/* SCHED_FIFO priority of the main watchdog thread. Above everything else. */
#define PLC_FIFO_WATCHDOG 99

/* SCHED_FIFO priority of the GCD master-tick dispatcher. */
#define PLC_FIFO_DISPATCHER 98

/* Highest SCHED_FIFO priority an IEC task may get. Kept below the PREEMPT_RT
 * default of 50 for threaded IRQ handlers. */
#define PLC_FIFO_TASK_MAX 49

/* A task still in one scan after this many of its own periods is stuck. Also
 * the grace each task gets to finish its scan when the PLC stops. */
#define PLC_TASK_STUCK_PERIODS 10

/* IEC TASK priority range accepted by the runtime. 0 is the highest. */
#define PLC_IEC_PRIORITY_MIN 0
#define PLC_IEC_PRIORITY_MAX 48

    /**
     * @brief Map an IEC 61131-3 TASK priority to a SCHED_FIFO priority.
     *
     * IEC priority 0 is the highest and maps to PLC_FIFO_TASK_MAX (49); IEC
     * priority 48 maps to 1. Values outside 0..48 are clamped to the nearest end.
     *
     * @param iec_priority priority declared on the IEC TASK
     * @param clamped      set to true when iec_priority was out of range; may be NULL
     * @return SCHED_FIFO priority in 1..PLC_FIFO_TASK_MAX
     */
    int plc_task_fifo_priority(int iec_priority, bool *clamped);

#ifdef __cplusplus
}
#endif

#endif // TASK_POLICY_H
