/**
 * @file ethercat_proc.c
 * @brief Process-spawn helpers for the EtherCAT plugin.
 */

#include "ethercat_proc.h"

#if !defined(__CYGWIN__) && !defined(_WIN32)

#include <fcntl.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/types.h>

int ecat_run_argv(const char *bin, char *const argv[],
                  char *capture_buf, size_t capture_size)
{
    if (bin == NULL || argv == NULL)
        return -1;

    int pipefd[2] = { -1, -1 };
    int capturing = (capture_buf != NULL && capture_size > 0);
    if (capturing) {
        if (pipe(pipefd) != 0)
            return -1;
    }

    pid_t pid = fork();
    if (pid < 0) {
        if (capturing) { close(pipefd[0]); close(pipefd[1]); }
        return -1;
    }

    if (pid == 0) {
        /* --- child --- */
        int devnull = open("/dev/null", O_WRONLY);
        if (capturing) {
            close(pipefd[0]);
            if (pipefd[1] != STDOUT_FILENO) {
                dup2(pipefd[1], STDOUT_FILENO);
                close(pipefd[1]);
            }
        } else if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
        }
        if (devnull >= 0) {
            dup2(devnull, STDERR_FILENO);
            if (devnull > STDERR_FILENO)
                close(devnull);
        }
        execvp(bin, argv);
        _exit(127);
    }

    /* --- parent --- */
    if (capturing) {
        close(pipefd[1]);
        size_t total = 0;
        while (total + 1 < capture_size) {
            ssize_t n = read(pipefd[0], capture_buf + total,
                             capture_size - 1 - total);
            if (n <= 0)
                break;
            total += (size_t)n;
        }
        capture_buf[total] = '\0';
        /* Drain any remaining output so the child does not block on a
         * full pipe; we have already truncated to capture_size. */
        char drain[256];
        while (read(pipefd[0], drain, sizeof(drain)) > 0)
            ;
        close(pipefd[0]);
    }

    int status = 0;
    if (waitpid(pid, &status, 0) < 0)
        return -1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

#else /* MSYS2 / Cygwin / native Windows -- shell-out paths are not used */

int ecat_run_argv(const char *bin, char *const argv[],
                  char *capture_buf, size_t capture_size)
{
    (void)bin;
    (void)argv;
    (void)capture_buf;
    (void)capture_size;
    return -1;
}

#endif
