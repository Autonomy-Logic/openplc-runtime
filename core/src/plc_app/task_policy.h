// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef TASK_POLICY_H
#define TASK_POLICY_H

#include <signal.h>
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

/* A task's first scan is exempt from the stuck count; the main watchdog trips it only
 * past this bound, so a hung initialisation is not permanent. */
#define PLC_FIRST_SCAN_TIMEOUT_MS 10000

/* How long zeroed outputs are held on stop before plugins are stopped, so
 * plugins polling the image from their own threads can send them. */
#define PLC_OUTPUTS_OFF_SETTLE_MS 500

/* How long an aborted task, or an idle one that was woken, may take to exit
 * before the runtime gives up and exits with PLC_EXIT_WATCHDOG_FAULT. */
#define PLC_TASK_ABORT_TIMEOUT_MS 2000

/* Fixed allowance for the stop teardown (plugin stop, unload) on top of the
 * task grace. Past the whole budget the watchdog exits the process. */
#define PLC_STOP_TEARDOWN_ALLOWANCE_MS 30000

/* A dispatcher with no tick for max(10 base ticks, this) is stalled. */
#define PLC_DISPATCHER_STALL_MIN_MS 1000

/* Process exit code for an unrecoverable watchdog fault. The webserver
 * (webserver/runtimemanager.py) restarts the runtime in safe mode on it. */
#define PLC_EXIT_WATCHDOG_FAULT 42

/* Written before the watchdog exit and consumed at the next boot, which then
 * starts in safe mode reporting ERROR even when the exit code was not seen. */
#define PLC_WATCHDOG_FAULT_MARKER "/run/runtime/watchdog_fault"

/* Fault context written into the marker when the safe-mode boot's outputs-off
 * itself hangs; the next boot then skips it instead of looping. */
#define PLC_FAULT_CONTEXT_BOOT_OUTPUTS_OFF "safe-mode boot outputs-off"

/* Signal the teardown sends to a task still in its scan after its grace. Runtime
 * code reached from IEC bodies blocks it around fork/allocation-heavy sections. */
#define PLC_TASK_ABORT_SIGNAL SIGUSR2

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
