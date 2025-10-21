#ifndef UTILS_H
#define UTILS_H

#include <dlfcn.h>
#include <sched.h>
#include <time.h>
#include <stdint.h>

#include "log.h"

extern unsigned long long *ext_common_ticktime__;
extern unsigned long tick__;
extern char *ext_plc_program_md5;


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

#endif // UTILS_H
