// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef WATCHDOG_H
#define WATCHDOG_H

/**
 * @brief Initialize the watchdog
 * @return int 0 on success, -1 on failure
 */
#ifdef __cplusplus
extern "C" {
#endif

int watchdog_init(void);

/* Called once per dispatcher tick. Independent of wall-clock adjustments. */
void watchdog_feed(void);

#ifdef __cplusplus
}
#endif

#endif // WATCHDOG_H
