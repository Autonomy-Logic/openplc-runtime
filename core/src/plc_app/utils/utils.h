#ifndef UTILS_H
#define UTILS_H

#include <dlfcn.h>
#include <pthread.h>
#include <sched.h>
#include <sys/mman.h>
#include <time.h>
#include <stdint.h>

#include "log.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Base tick interval in nanoseconds — GCD of declared task intervals.
 * Computed runtime-side after the .so loads (see image_tables.cpp's
 * compute_base_tick_from_config). Default 20 ms before computation. */
extern uint64_t base_tick_ns;

/* Scan counter — incremented once per scan cycle by plc_run_io_cycle_threaded_post.
 * Reported in DEBUG_GET / DEBUG_GET_LIST responses so the editor can
 * detect cycle boundaries. */
extern unsigned long scan_counter;

/* Project MD5 string (resolved from the .so at load time). */
extern char *ext_strucpp_program_md5;


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

/**
 * @brief Lock all current and future memory pages to prevent page faults
 * 
 * This prevents the kernel from swapping out memory pages during PLC execution,
 * which could cause unpredictable latency spikes in the scan cycle.
 */
void lock_memory(void);

/**
 * @brief Parse a hex string into a byte array
 *
 * @param hex_string The hex string to parse
 * @param data The byte array to store the result
 * @return The number of bytes parsed
 */
size_t parse_hex_string(const char *hex_string, uint8_t *data);

/**
 * @brief Convert a byte array to a hex string
 *
 * @param bytes The byte array to convert
 * @param len The length of the byte array
 * @param out_str The string to store the result
 * @param out_size The size of the output string buffer
 * @param prepend An optional string to prepend to the output (can be NULL)
 */
void bytes_to_hex_string(const uint8_t *bytes, size_t len, char *out_str, size_t out_size, const char *prepend);

/**
 * @brief Initialize a mutex with PTHREAD_PRIO_INHERIT for priority inversion avoidance
 *
 * On Linux, sets the mutex protocol to PTHREAD_PRIO_INHERIT so that a thread
 * holding the mutex temporarily inherits the priority of any higher-priority
 * waiter. On MSYS2/Cygwin (no RT support), falls back to a regular mutex.
 *
 * @param mutex The mutex to initialize
 * @return 0 on success, -1 on failure
 */
int init_rt_mutex(pthread_mutex_t *mutex);

#ifdef __cplusplus
}
#endif

#endif // UTILS_H
