#ifndef UNIX_SOCKET_H
#define UNIX_SOCKET_H

#define SOCKET_PATH "/run/runtime/plc_runtime.socket"
#define COMMAND_BUFFER_SIZE 8192
#define MAX_RESPONSE_SIZE 16384
#define MAX_CLIENTS 1

int setup_unix_socket();
void close_unix_socket();
void *unix_socket_thread(void *arg);

#endif // UNIX_SOCKET_H
