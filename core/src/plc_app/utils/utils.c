#include "utils.h"
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

unsigned long long *ext_common_ticktime__ = NULL;
unsigned long tick__ = 0;

void normalize_timespec(struct timespec *ts) 
{
    while (ts->tv_nsec >= 1e9) 
    {
        ts->tv_nsec -= 1e9;
        ts->tv_sec++;
    }
}

void sleep_until(struct timespec *ts) 
{
    clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, ts, NULL);
}

void timespec_diff(struct timespec *a, struct timespec *b, struct timespec *result) 
{
    // Calculate the difference in seconds
    result->tv_sec = a->tv_sec - b->tv_sec;

    // Calculate the difference in nanoseconds
    result->tv_nsec = a->tv_nsec - b->tv_nsec;

    // Handle borrowing if nanoseconds are negative
    if (result->tv_nsec < 0) 
    {
        // Borrow 1 second (1e9 nanoseconds)
        --result->tv_sec;
        result->tv_nsec += 1000000000L;
    }
}

// configure SCHED_FIFO priority
void set_realtime_priority(void) 
{
    struct sched_param param;
    param.sched_priority = 20; // Priority between 1 and 99

    if (sched_setscheduler(0, SCHED_FIFO, &param) != 0) 
    {
        log_error("sched_setscheduler failed: %s", strerror(errno));
    } 
    else 
    {
        log_info("Scheduler set to SCHED_FIFO, priority %d", param.sched_priority);
    }
}
