// _GNU_SOURCE is required for pthread_setaffinity_np and CPU_SET macros
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "utils.h"
#include <errno.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// MSYS2/Cygwin does not support mlockall or real-time scheduling
// These features are only available on Linux
#if !defined(__CYGWIN__) && !defined(__MSYS__)
#include <sched.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#define HAS_REALTIME_FEATURES 1
#else
#define HAS_REALTIME_FEATURES 0
#endif

unsigned long long *ext_common_ticktime__ = NULL;
unsigned long tick__                      = 0;
char *ext_plc_program_md5                 = NULL;

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
#if HAS_REALTIME_FEATURES
    clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, ts, NULL);
#else
    // Fallback for MSYS2/Cygwin: use nanosleep (relative sleep)
    struct timespec now, remaining;
    clock_gettime(CLOCK_MONOTONIC, &now);
    remaining.tv_sec  = ts->tv_sec - now.tv_sec;
    remaining.tv_nsec = ts->tv_nsec - now.tv_nsec;
    if (remaining.tv_nsec < 0)
    {
        remaining.tv_sec--;
        remaining.tv_nsec += 1000000000L;
    }
    if (remaining.tv_sec >= 0)
    {
        nanosleep(&remaining, NULL);
    }
#endif
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

// CPU affinity is left to deployment (taskset, cgroup cpuset, systemd
// CPUAffinity=) so the runtime stays hardware-agnostic.
void set_realtime_priority(void)
{
#if HAS_REALTIME_FEATURES
    struct sched_param param;
    param.sched_priority = 20;

    if (sched_setscheduler(0, SCHED_FIFO, &param) != 0)
    {
        log_error("sched_setscheduler failed: %s", strerror(errno));
    }
    else
    {
        log_info("Scheduler set to SCHED_FIFO, priority %d", param.sched_priority);
    }

    // Default timer slack (50us) directly becomes wake-up jitter on
    // clock_nanosleep(TIMER_ABSTIME). PR_SET_TIMERSLACK is per-thread.
    if (prctl(PR_SET_TIMERSLACK, 1UL, 0, 0, 0) != 0)
    {
        log_warn("PR_SET_TIMERSLACK failed: %s", strerror(errno));
    }
    else
    {
        log_info("Timer slack reduced to 1ns for PLC thread");
    }
#else
    // Real-time scheduling not available on MSYS2/Cygwin
    log_info("Real-time scheduling not available on this platform");
#endif
}

// Lock all memory pages to prevent page faults during PLC execution
void lock_memory(void)
{
#if HAS_REALTIME_FEATURES
    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0)
    {
        log_error("mlockall failed: %s", strerror(errno));
    }
    else
    {
        log_info("Memory locked successfully (MCL_CURRENT | MCL_FUTURE)");
    }
#else
    // Memory locking not available on MSYS2/Cygwin
    log_info("Memory locking not available on this platform");
#endif
}

int init_rt_mutex(pthread_mutex_t *mutex)
{
#if HAS_REALTIME_FEATURES
    pthread_mutexattr_t attr;
    if (pthread_mutexattr_init(&attr) != 0)
        return -1;
    if (pthread_mutexattr_setprotocol(&attr, PTHREAD_PRIO_INHERIT) != 0)
    {
        pthread_mutexattr_destroy(&attr);
        return -1;
    }
    if (pthread_mutex_init(mutex, &attr) != 0)
    {
        pthread_mutexattr_destroy(&attr);
        return -1;
    }
    pthread_mutexattr_destroy(&attr);
    return 0;
#else
    return pthread_mutex_init(mutex, NULL);
#endif
}

size_t parse_hex_string(const char *hex_string, uint8_t *data)
{
    size_t count    = 0;
    const char *ptr = hex_string;

    while (*ptr != '\0')
    {
        // Skip leading spaces
        while (*ptr == ' ')
        {
            ptr++;
        }

        if (*ptr == '\0')
        {
            break;
        }

        // Read two hex digits
        unsigned int value;
        int scanned = sscanf(ptr, "%2x", &value);
        if (scanned != 1)
        {
            break;
        }

        data[count++] = (uint8_t)value;

        // Move past the parsed value (2 hex chars)
        while (*ptr != '\0' && *ptr != ' ')
        {
            ptr++;
        }
    }

    return count;
}

void bytes_to_hex_string(const uint8_t *bytes, size_t len, char *out_str, size_t out_size,
                         const char *prepend)
{
    size_t pos = 0;

    // Add prepend string first, if provided
    if (prepend != NULL)
    {
        size_t prepend_len = strlen(prepend);
        if (prepend_len >= out_size)
        {
            // Not enough space even for prepend
            if (out_size > 0)
            {
                out_str[0] = '\0';
            }
            return;
        }
        strcpy(out_str, prepend);
        pos = prepend_len;
    }

    for (size_t i = 0; i < len; i++)
    {
        // Each byte needs up to 3 chars: "xx " + null terminator at the end
        int written = snprintf(out_str + pos, out_size - pos, "%02x", bytes[i]);
        if (written < 0 || (size_t)written >= out_size - pos)
        {
            // Stop if buffer is full or error
            break;
        }

        pos += written;

        if (i < len - 1)
        {
            if (pos + 1 >= out_size)
            {
                break;
            }
            out_str[pos++] = ' ';
            out_str[pos]   = '\0';
        }
    }

    // Ensure null termination
    if (pos < out_size)
    {
        out_str[pos] = '\0';
    }
    else
    {
        out_str[out_size - 1] = '\0';
    }
}
