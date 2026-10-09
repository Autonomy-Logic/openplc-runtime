// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef WATCHDOG_H
#define WATCHDOG_H

#include <stdint.h>

/* Size of a watchdog_fatal_exit() reason buffer. */
#define WATCHDOG_REASON_LEN 160

#ifdef __cplusplus
extern "C"
{
#endif

    /**
     * @brief Initialize the watchdog
     * @return int 0 on success, -1 on failure
     */
    int watchdog_init(void);

    /**
     * @brief Record a dispatcher tick. Called once per master tick while RUNNING.
     */
    void watchdog_feed(void);

    /**
     * @brief Arm the dispatcher-stall check for a dispatcher ticking every period_ns.
     */
    void watchdog_dispatcher_started(int64_t period_ns);

    /**
     * @brief Disarm the dispatcher-stall check once the dispatcher has left its loop.
     */
    void watchdog_dispatcher_stopped(void);

    /**
     * @brief Name the operation in progress, reported by watchdog_fatal_exit() and
     *        written into the fault marker. NULL clears it.
     * @param context string with static storage duration, or NULL
     */
    void watchdog_set_fault_context(const char *context);

    /**
     * @brief Last resort when the runtime cannot recover in-process.
     *
     * Logs reason without blocking on any lock and terminates the process with
     * PLC_EXIT_WATCHDOG_FAULT, so the supervisor restarts it in safe mode.
     * Outputs are not driven; the hardware safe-state watchdog covers them.
     *
     * @param reason message for the log; must not be NULL
     */
    void watchdog_fatal_exit(const char *reason) __attribute__((noreturn));

#ifdef __cplusplus
}
#endif

#endif // WATCHDOG_H
