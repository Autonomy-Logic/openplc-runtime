#include "log.h"
#include <pthread.h>
#include <stdarg.h>
#include <string.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <errno.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <errno.h>
#include <unistd.h>
#include <stdbool.h>

static LogLevel current_level = LOG_LEVEL_INFO;
static pthread_mutex_t log_mutex = PTHREAD_MUTEX_INITIALIZER;
int socket_fd = -1;
bool print_logs = false;

extern volatile sig_atomic_t keep_running;

void log_set_level(LogLevel level) { current_level = level; }

// Create circular buffer for unsent logs
#define LOG_BUFFER_SIZE 1024
#define LOG_MESSAGE_SIZE 2048
char log_buffer[LOG_BUFFER_SIZE][LOG_MESSAGE_SIZE];
int log_buffer_start = 0;
int log_buffer_end = 0;


void *log_thread_management(void *arg) 
{
    char *unix_socket_path = (char *)arg;

    while(keep_running)
    {
        if (socket_fd < 0) 
        {
            struct sockaddr_un addr;
            socket_fd = socket(AF_UNIX, SOCK_STREAM, 0);
            if (socket_fd < 0)
            {
                log_error("Log socket creation failed: %s", strerror(errno));
                // Wait before retrying
                sleep(1);
                continue;
            }

            memset(&addr, 0, sizeof(addr));
            addr.sun_family = AF_UNIX;
            strncpy(addr.sun_path, unix_socket_path, sizeof(addr.sun_path) - 1);
            if (connect(socket_fd, (struct sockaddr *)&addr, sizeof(addr)) == -1)
            {
                log_error("Log socket connection failed: %s", strerror(errno));
                close(socket_fd);
                socket_fd = -1;
            }
        }

        // Wait before rechecking the connection
        sleep(1);
    }

    close(socket_fd);
    socket_fd = -1;

    return NULL;
}

void store_on_buffer(const char *msg) 
{
    strncpy(log_buffer[log_buffer_end], msg, sizeof(log_buffer[log_buffer_end]) - 1);
    log_buffer[log_buffer_end][sizeof(log_buffer[log_buffer_end]) - 1] = '\0';
    log_buffer_end = (log_buffer_end + 1) % LOG_BUFFER_SIZE;

    // If buffer is full, move start forward
    if (log_buffer_end == log_buffer_start)
    {
        log_buffer_start = (log_buffer_start + 1) % LOG_BUFFER_SIZE;
    }
}

char *retrieve_from_buffer() 
{
    if (log_buffer_start == log_buffer_end)
    {
        return NULL; // Buffer is empty
    }

    char *msg = log_buffer[log_buffer_start];
    log_buffer_start = (log_buffer_start + 1) % LOG_BUFFER_SIZE;
    return msg;
}

int log_init(char *unix_socket_path) 
{
    // Create a copy of the socket path in the heap
    char *path_copy = malloc(strlen(unix_socket_path) + 1);
    if (!path_copy)
    {
        perror("Failed to allocate memory for socket path");
        return -1;
    }
    strcpy(path_copy, unix_socket_path);

    // Create the logging thread
    pthread_t thread_id;
    if (pthread_create(&thread_id, NULL, log_thread_management, path_copy) != 0) 
    {
        free(path_copy);
        perror("Failed to create log thread");
        return -1;
    }

    return 0; // Success
}


static const char *level_to_str(LogLevel level) 
{
    switch (level) 
    {
      case LOG_LEVEL_DEBUG:
          return "DEBUG";
      case LOG_LEVEL_INFO:
          return "INFO";
      case LOG_LEVEL_WARN:
          return "WARN";
      case LOG_LEVEL_ERROR:
          return "ERROR";
      default:
          return "UNKNOWN";
    }
}

static void log_write(LogLevel level, const char *fmt, va_list args) 
{
    if (level < current_level)
    {
        return;
    }
    
    // Capture time for timestamp
    time_t now = time(NULL);
    struct tm t;
    localtime_r(&now, &t);

    char stdout_msg[LOG_MESSAGE_SIZE];
    char log_msg[LOG_MESSAGE_SIZE];

    if (print_logs)
    {
        // Create a copy of va_list for stdout formatting
        va_list args_copy;
        va_copy(args_copy, args);

        // Format for stdout
        char time_buf[20];
        strftime(time_buf, sizeof(time_buf), "%Y-%m-%d %H:%M:%S", &t);

        int n = snprintf(stdout_msg, sizeof(stdout_msg), "[%s] [%s] ", time_buf, level_to_str(level));
        n += vsnprintf(stdout_msg + n, sizeof(stdout_msg) - n, fmt, args_copy);
        snprintf(stdout_msg + n, sizeof(stdout_msg) - n, "\n");

        // Cleanup
        va_end(args_copy);
    }

    // Format the log message in JSON format
    int n = snprintf(log_msg, sizeof(log_msg), "{\"timestamp\":\"%ld\",\"level\":\"%s\",\"message\":\"", (long)now, level_to_str(level));
    n += vsnprintf(log_msg + n, sizeof(log_msg) - n, fmt, args);
    snprintf(log_msg + n, sizeof(log_msg) - n, "\"}\n");

    // Send to unix socket if connected
    pthread_mutex_lock(&log_mutex);
    if (socket_fd >= 0) 
    {
        // Send any buffered messages first
        char *buffered_msg = retrieve_from_buffer();
        while (buffered_msg != NULL)
        {
            if (write(socket_fd, buffered_msg, strlen(buffered_msg)) == -1)
            {
                // On error, close the socket to trigger reconnection
                close(socket_fd);
                socket_fd = -1;
                // Rewind index to re-store the message
                log_buffer_start = (log_buffer_start - 1 + LOG_BUFFER_SIZE) % LOG_BUFFER_SIZE;
                break;
            }
            buffered_msg = retrieve_from_buffer();
        }

        // Send current message
        if (socket_fd >= 0)
        {
            if (write(socket_fd, log_msg, strlen(log_msg)) == -1)
            {
                // On error, close the socket to trigger reconnection
                close(socket_fd);
                socket_fd = -1;

                // Store message in buffer
                store_on_buffer(log_msg);
            }
        }
    }
    else
    {
        store_on_buffer(log_msg);
    }

    // Print to stdout if enabled
    if (print_logs)
    {
        fputs(stdout_msg, stdout);
    }

    pthread_mutex_unlock(&log_mutex);
}

void log_info(const char *fmt, ...) 
{
    va_list args;
    va_start(args, fmt);
    log_write(LOG_LEVEL_INFO, fmt, args);
    va_end(args);
}

void log_debug(const char *fmt, ...) 
{
    va_list args;
    va_start(args, fmt);
    log_write(LOG_LEVEL_DEBUG, fmt, args);
    va_end(args);
}

void log_warn(const char *fmt, ...) 
{
    va_list args;
    va_start(args, fmt);
    log_write(LOG_LEVEL_WARN, fmt, args);
    va_end(args);
}

void log_error(const char *fmt, ...) 
{
    va_list args;
    va_start(args, fmt);
    log_write(LOG_LEVEL_ERROR, fmt, args);
    va_end(args);
}
