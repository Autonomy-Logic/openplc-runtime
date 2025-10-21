#include "utils.h"
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

unsigned long long *ext_common_ticktime__ = NULL;
unsigned long tick__ = 0;
char *ext_plc_program_md5 = NULL;

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

size_t parse_hex_string(const char *hex_string, uint8_t *data)
{
    size_t count = 0;
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

void bytes_to_hex_string(const uint8_t *bytes, size_t len, char *out_str, size_t out_size, const char *prepend)
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
            out_str[pos] = '\0';
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

