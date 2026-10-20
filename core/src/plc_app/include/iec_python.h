// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// This header declares the Python Function Block loader functions.
// These functions are used by the generated PLC code to load and execute
// Python function blocks via shared memory communication.

#ifndef IEC_PYTHON_H
#define IEC_PYTHON_H

#include <stddef.h>
#include <sys/types.h>
#include <unistd.h>

#ifdef __cplusplus
extern "C"
{
#endif

    /**
     * @brief Create a unique shared memory name
     *
     * Creates a unique name for shared memory regions using mkstemp.
     * The name is suitable for use with shm_open().
     *
     * @param buf Buffer to store the generated name
     * @param size Size of the buffer
     * @return 0 on success, -1 on failure
     */
    int create_shm_name(char *buf, size_t size);

    /**
     * @brief Load and start a Python function block
     *
     * Writes the Python script to disk, creates shared memory regions for
     * input/output data exchange, and spawns a Python process to execute
     * the function block.
     *
     * @param script_name Path where the Python script will be written
     * @param script_content The Python script content (with format specifiers for pid and shm_name)
     * @param shm_name Base name for shared memory regions
     * @param shm_in_size Size of the input shared memory region
     * @param shm_out_size Size of the output shared memory region
     * @param shm_in_ptr Pointer to store the mapped input shared memory address
     * @param shm_out_ptr Pointer to store the mapped output shared memory address
     * @param pid PLC process ID (passed to Python script for monitoring)
     * @return 0 on success, -1 on failure
     */
    int python_block_loader(const char *script_name, const char *script_content, char *shm_name,
                            size_t shm_in_size, size_t shm_out_size, void **shm_in_ptr,
                            void **shm_out_ptr, pid_t pid);

    /**
     * @brief Set logging function pointers for the Python loader
     *
     * This function must be called after loading libplc.so to inject the
     * runtime's logging functions. Without this, logging will fall back
     * to stderr output.
     *
     * @param log_info_func Pointer to the log_info function
     * @param log_error_func Pointer to the log_error function
     */
    void python_loader_set_loggers(void (*log_info_func)(const char *, ...),
                                   void (*log_error_func)(const char *, ...));

    /**
     * @brief Cleanup all Python function blocks
     *
     * This function must be called before unloading libplc.so to:
     * 1. Terminate all Python subprocesses (SIGTERM, then SIGKILL)
     * 2. Wait for all runner threads to exit
     * 3. Unmap and unlink shared memory regions
     * 4. Remove Python script files
     *
     * Failure to call this before dlclose() will cause a crash because
     * the runner threads' code lives in the shared library.
     */
    void python_blocks_cleanup(void);

#ifdef __cplusplus
}
#endif

#endif /* IEC_PYTHON_H */
