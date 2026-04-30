/**
 * @file ethercat_proc.h
 * @brief Process-spawn helpers for the EtherCAT plugin.
 *
 * Wraps fork+execvp so the plugin can invoke external binaries (ethtool,
 * iptables) without going through a shell. Argv elements are passed
 * verbatim to execvp, so user-controlled strings (e.g. the configured
 * NIC interface name) cannot be reinterpreted as shell metacharacters.
 */

#ifndef ETHERCAT_PROC_H
#define ETHERCAT_PROC_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Run an external binary with explicit argv via fork+execvp.
 *
 * No shell is involved. argv must be NULL-terminated; argv[0] is passed
 * to the child as its argv[0] (conventionally equal to @p bin).
 *
 * Stderr in the child is always redirected to /dev/null. Stdout is either
 * captured (if @p capture_buf is non-NULL) or also redirected to /dev/null.
 *
 * @param bin           Binary name (resolved via PATH by execvp).
 * @param argv          NULL-terminated argument vector.
 * @param capture_buf   Optional buffer to receive child stdout, NUL-terminated.
 *                      Output is truncated to @p capture_size - 1 bytes.
 * @param capture_size  Size of @p capture_buf. Ignored if buffer is NULL.
 *
 * @return Exit status of the child (0 on success, non-zero from the child
 *         such as 127 if execvp failed), or -1 if the parent could not
 *         spawn or wait. Always returns -1 on non-Linux platforms.
 */
int ecat_run_argv(const char *bin, char *const argv[],
                  char *capture_buf, size_t capture_size);

#ifdef __cplusplus
}
#endif

#endif /* ETHERCAT_PROC_H */
