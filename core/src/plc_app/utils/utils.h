#ifndef UTILS_H
#define UTILS_H

#include <dlfcn.h>
#include <sched.h>
#include <time.h>

#include "log.h"

extern unsigned long long *ext_common_ticktime__;
extern unsigned long tick__;


/**
 * @brief Normalize a timespec structure
 *
 * @param ts The timespec structure to normalize
 */
void normalize_timespec(struct timespec *ts);

/**
 * @brief Sleep until a specific timespec
 *
 * @param ts The timespec to sleep until
 */
void sleep_until(struct timespec *ts);

/**
 * @brief Calculate the difference between two timespec structures
 *
 * @param a The first timespec
 * @param b The second timespec
 * @param result The timespec to store the result
 */
void timespec_diff(struct timespec *a, struct timespec *b,
                   struct timespec *result);

/**
 * @brief Set the realtime priority object
 */
void set_realtime_priority(void);

#endif // UTILS_H
