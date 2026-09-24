// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// This file is responsible for loading function blocks written in Python.
// Python function blocks communicate with the PLC runtime via shared memory.
//
// Logging is done via function pointers that are set by the runtime after
// loading libplc.so. This avoids symbol resolution issues between the
// shared library and the main executable.

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include "include/iec_python.h"

// Function pointers for logging - set by python_loader_set_loggers()
// These are always initialized by symbols_init() before any Python FB code runs
static void (*py_log_info)(const char *fmt, ...);
static void (*py_log_error)(const char *fmt, ...);

// Simple wrapper macros for logging
#define LOG_INFO(...)                                                                              \
    do                                                                                             \
    {                                                                                              \
        if (py_log_info)                                                                           \
            py_log_info(__VA_ARGS__);                                                              \
    } while (0)
#define LOG_ERROR(...)                                                                             \
    do                                                                                             \
    {                                                                                              \
        if (py_log_error)                                                                          \
            py_log_error(__VA_ARGS__);                                                             \
    } while (0)

// Maximum number of Python function blocks that can be loaded simultaneously
#define MAX_PYTHON_BLOCKS 128

// Tracking structure for each Python function block
typedef struct
{
    bool active;              // Whether this slot is in use
    pthread_t thread;         // Runner thread ID
    pid_t python_pid;         // Python subprocess PID
    int pipe_fd;              // Pipe read end for stdout
    void *shm_in_ptr;         // Mapped input shared memory
    void *shm_out_ptr;        // Mapped output shared memory
    size_t shm_in_size;       // Size of input shared memory
    size_t shm_out_size;      // Size of output shared memory
    char shm_in_name[256];    // Name of input shared memory region
    char shm_out_name[256];   // Name of output shared memory region
    char script_name[256];    // Python script filename
} python_block_t;

// Array to track all Python blocks
static python_block_t python_blocks[MAX_PYTHON_BLOCKS];
static int python_block_count                  = 0;
static pthread_mutex_t python_blocks_mutex     = PTHREAD_MUTEX_INITIALIZER;
static volatile bool python_cleanup_in_progress = false;

void python_loader_set_loggers(void (*log_info_func)(const char *, ...),
                               void (*log_error_func)(const char *, ...))
{
    py_log_info  = log_info_func;
    py_log_error = log_error_func;
}

/**
 * @brief Thread function that reads Python subprocess output and logs it
 *
 * This function reads from the pipe connected to Python's stdout/stderr
 * and logs the output. It exits when the pipe is closed (Python exits or
 * cleanup closes the pipe).
 *
 * @param arg Pointer to the python_block_t structure for this block
 * @return NULL
 */
static void *runner_thread(void *arg)
{
    python_block_t *block = (python_block_t *)arg;
    int pipe_fd           = block->pipe_fd;

    // Convert pipe fd to FILE* for easier line reading
    FILE *fp = fdopen(pipe_fd, "r");
    if (fp == NULL)
    {
        LOG_ERROR("[Python] Failed to fdopen pipe: %s", strerror(errno));
        close(pipe_fd);
        return NULL;
    }

    char buffer[512];
    while (!python_cleanup_in_progress && fgets(buffer, sizeof(buffer), fp) != NULL)
    {
        // Check if cleanup started while we were blocked
        if (python_cleanup_in_progress)
        {
            break;
        }

        // Remove trailing newline if present
        size_t len = strlen(buffer);
        if (len > 0 && buffer[len - 1] == '\n')
        {
            buffer[len - 1] = '\0';
        }
        LOG_INFO("[Python] %s", buffer);
    }

    fclose(fp); // This also closes pipe_fd
    return NULL;
}

int create_shm_name(char *buf, size_t size)
{
    char shm_mask[] = "/tmp/shmXXXXXXXXXXXX";
    int fd          = mkstemp(shm_mask);
    if (fd == -1)
    {
        LOG_ERROR("[Python loader] mkstemp failed: %s", strerror(errno));
        return -1;
    }
    close(fd);

    snprintf(buf, size, "%s", strrchr(shm_mask, '/') + 1);
    unlink(shm_mask);

    return 0;
}

int python_block_loader(const char *script_name, const char *script_content, char *shm_name,
                        size_t shm_in_size, size_t shm_out_size, void **shm_in_ptr,
                        void **shm_out_ptr, pid_t pid)
{
    char shm_in_name[256];
    char shm_out_name[256];

    // Find a free slot for this Python block
    pthread_mutex_lock(&python_blocks_mutex);
    int slot = -1;
    for (int i = 0; i < MAX_PYTHON_BLOCKS; i++)
    {
        if (!python_blocks[i].active)
        {
            slot = i;
            break;
        }
    }
    if (slot == -1)
    {
        pthread_mutex_unlock(&python_blocks_mutex);
        LOG_ERROR("[Python loader] Maximum number of Python blocks (%d) reached",
                  MAX_PYTHON_BLOCKS);
        return -1;
    }
    python_block_t *block = &python_blocks[slot];
    memset(block, 0, sizeof(python_block_t));
    block->active = true;
    python_block_count++;
    pthread_mutex_unlock(&python_blocks_mutex);

    // Write the Python script to disk
    FILE *fp = fopen(script_name, "w");
    if (!fp)
    {
        LOG_ERROR("[Python loader] Failed to write Python script: %s", strerror(errno));
        goto error_deactivate;
    }
    chmod(script_name, 0640);

    LOG_INFO("[Python loader] Random shared memory location: %s", shm_name);

    snprintf(shm_in_name, sizeof(shm_in_name), "/%s_in", shm_name);
    snprintf(shm_out_name, sizeof(shm_out_name), "/%s_out", shm_name);

    // Store names for cleanup
    strncpy(block->shm_in_name, shm_in_name, sizeof(block->shm_in_name) - 1);
    strncpy(block->shm_out_name, shm_out_name, sizeof(block->shm_out_name) - 1);
    strncpy(block->script_name, script_name, sizeof(block->script_name) - 1);

    // Write script content with format specifiers replaced
    fprintf(fp, script_content, pid, shm_name, shm_name);
    fflush(fp);
    fsync(fileno(fp));
    fclose(fp);

    // Map shared memory for inputs
    int shm_in_fd = shm_open(shm_in_name, O_CREAT | O_RDWR, 0660);
    if (shm_in_fd < 0)
    {
        LOG_ERROR("[Python loader] shm_open (input) error: %s", strerror(errno));
        goto error_deactivate;
    }
    if (ftruncate(shm_in_fd, shm_in_size) == -1)
    {
        LOG_ERROR("[Python loader] ftruncate (input) error: %s", strerror(errno));
        close(shm_in_fd);
        goto error_deactivate;
    }
    *shm_in_ptr = mmap(NULL, shm_in_size, PROT_READ | PROT_WRITE, MAP_SHARED, shm_in_fd, 0);
    if (*shm_in_ptr == MAP_FAILED)
    {
        LOG_ERROR("[Python loader] mmap (input) error: %s", strerror(errno));
        close(shm_in_fd);
        goto error_deactivate;
    }
    close(shm_in_fd);

    // Store for cleanup
    block->shm_in_ptr  = *shm_in_ptr;
    block->shm_in_size = shm_in_size;

    // Map shared memory for outputs
    int shm_out_fd = shm_open(shm_out_name, O_CREAT | O_RDWR, 0660);
    if (shm_out_fd < 0)
    {
        LOG_ERROR("[Python loader] shm_open (output) error: %s", strerror(errno));
        goto error_cleanup_shm_in;
    }
    if (ftruncate(shm_out_fd, shm_out_size) == -1)
    {
        LOG_ERROR("[Python loader] ftruncate (output) error: %s", strerror(errno));
        close(shm_out_fd);
        goto error_cleanup_shm_in;
    }
    *shm_out_ptr = mmap(NULL, shm_out_size, PROT_READ | PROT_WRITE, MAP_SHARED, shm_out_fd, 0);
    if (*shm_out_ptr == MAP_FAILED)
    {
        LOG_ERROR("[Python loader] mmap (output) error: %s", strerror(errno));
        close(shm_out_fd);
        goto error_cleanup_shm_in;
    }
    close(shm_out_fd);

    // Store for cleanup
    block->shm_out_ptr  = *shm_out_ptr;
    block->shm_out_size = shm_out_size;

    // Create pipe for Python stdout/stderr
    int pipefd[2];
    if (pipe(pipefd) == -1)
    {
        LOG_ERROR("[Python loader] pipe() failed: %s", strerror(errno));
        goto error_cleanup_shm_out;
    }

    // Fork to create Python subprocess
    pid_t child_pid = fork();
    if (child_pid == -1)
    {
        LOG_ERROR("[Python loader] fork() failed: %s", strerror(errno));
        close(pipefd[0]);
        close(pipefd[1]);
        goto error_cleanup_shm_out;
    }

    if (child_pid == 0)
    {
        // Child process - execute Python
        close(pipefd[0]); // Close read end

        // Redirect stdout and stderr to pipe
        dup2(pipefd[1], STDOUT_FILENO);
        dup2(pipefd[1], STDERR_FILENO);
        close(pipefd[1]);

        // Execute Python with unbuffered output
        execlp("python3", "python3", "-u", script_name, (char *)NULL);

        // If exec fails
        _exit(127);
    }

    // Parent process
    close(pipefd[1]); // Close write end
    block->pipe_fd    = pipefd[0];
    block->python_pid = child_pid;

    // Spawn thread to read Python output
    if (pthread_create(&block->thread, NULL, runner_thread, block) != 0)
    {
        LOG_ERROR("[Python loader] pthread_create failed: %s", strerror(errno));
        close(pipefd[0]);
        kill(child_pid, SIGKILL);
        waitpid(child_pid, NULL, 0);
        goto error_cleanup_shm_out;
    }

    LOG_INFO("[Python loader] Started Python function block: %s (PID %d)", script_name, child_pid);

    return 0;

error_cleanup_shm_out:
    munmap(*shm_out_ptr, shm_out_size);
    shm_unlink(shm_out_name);
error_cleanup_shm_in:
    munmap(*shm_in_ptr, shm_in_size);
    shm_unlink(shm_in_name);
error_deactivate:
    pthread_mutex_lock(&python_blocks_mutex);
    block->active = false;
    python_block_count--;
    pthread_mutex_unlock(&python_blocks_mutex);
    return -1;
}

void python_blocks_cleanup(void)
{
    LOG_INFO("[Python loader] Cleaning up %d Python function block(s)...", python_block_count);

    // Signal that cleanup is in progress - runner threads will check this flag
    python_cleanup_in_progress = true;

    pthread_mutex_lock(&python_blocks_mutex);

    for (int i = 0; i < MAX_PYTHON_BLOCKS; i++)
    {
        python_block_t *block = &python_blocks[i];
        if (!block->active)
        {
            continue;
        }

        LOG_INFO("[Python loader] Stopping Python block: %s (PID %d)", block->script_name,
                 block->python_pid);

        // Send SIGTERM to Python subprocess
        if (block->python_pid > 0)
        {
            kill(block->python_pid, SIGTERM);
        }
    }

    pthread_mutex_unlock(&python_blocks_mutex);

    // Give Python processes time to exit gracefully
    usleep(100000); // 100ms

    pthread_mutex_lock(&python_blocks_mutex);

    for (int i = 0; i < MAX_PYTHON_BLOCKS; i++)
    {
        python_block_t *block = &python_blocks[i];
        if (!block->active)
        {
            continue;
        }

        // Check if process exited, if not send SIGKILL
        if (block->python_pid > 0)
        {
            int status;
            pid_t result = waitpid(block->python_pid, &status, WNOHANG);
            if (result == 0)
            {
                // Process still running, force kill
                LOG_INFO("[Python loader] Force killing Python block: %s (PID %d)",
                         block->script_name, block->python_pid);
                kill(block->python_pid, SIGKILL);
                waitpid(block->python_pid, NULL, 0);
            }
        }

        // Wait for runner thread to exit (it will get EOF from closed pipe)
        pthread_join(block->thread, NULL);

        // Cleanup shared memory
        if (block->shm_in_ptr && block->shm_in_size > 0)
        {
            munmap(block->shm_in_ptr, block->shm_in_size);
        }
        if (block->shm_out_ptr && block->shm_out_size > 0)
        {
            munmap(block->shm_out_ptr, block->shm_out_size);
        }
        if (block->shm_in_name[0] != '\0')
        {
            shm_unlink(block->shm_in_name);
        }
        if (block->shm_out_name[0] != '\0')
        {
            shm_unlink(block->shm_out_name);
        }

        // Remove Python script file
        if (block->script_name[0] != '\0')
        {
            unlink(block->script_name);
        }

        block->active = false;
    }

    python_block_count         = 0;
    python_cleanup_in_progress = false;

    pthread_mutex_unlock(&python_blocks_mutex);

    LOG_INFO("[Python loader] Cleanup complete");
}
