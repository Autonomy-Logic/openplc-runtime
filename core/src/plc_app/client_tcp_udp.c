// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// This is the file for the network routines of the OpenPLC. It has procedures
// to create a socket and connect to a server. These functions are called by
// the TCP communication function blocks (TCP_CONNECT, TCP_SEND, TCP_RECEIVE,
// TCP_CLOSE) defined in communication.h.

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "utils/log.h"

#define METHOD_UDP 1
#define METHOD_TCP 0

int connect_to_tcp_server(uint8_t *ip_address, uint16_t port, int method)
{
    int sockfd;
    struct sockaddr_in servaddr;

    if (method == METHOD_TCP)
        sockfd = socket(AF_INET, SOCK_STREAM, 0);
    else if (method == METHOD_UDP)
        sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    else
        return -1;

    if (sockfd == -1)
    {
        log_error("TCP Client: error creating socket => %s", strerror(errno));
        return -1;
    }
    memset(&servaddr, 0, sizeof(servaddr));

    // Configure socket
    servaddr.sin_family = AF_INET;
    servaddr.sin_addr.s_addr = inet_addr((const char *)ip_address);
    servaddr.sin_port = htons(port);

    // Connect to server
    if (connect(sockfd, (struct sockaddr *)&servaddr, sizeof(servaddr)) != 0)
    {
        log_error("TCP Client: error connecting to server => %s", strerror(errno));
        close(sockfd);
        return -1;
    }

    // Set non-blocking socket
    int flags = fcntl(sockfd, F_GETFL, 0);
    if (flags == -1)
    {
        log_error("TCP Client: error reading flags from socket => %s", strerror(errno));
        return -1;
    }
    flags = (flags | O_NONBLOCK);
    if (fcntl(sockfd, F_SETFL, flags) != 0)
    {
        log_error("TCP Client: error setting flags for socket => %s", strerror(errno));
        return -1;
    }

    return sockfd;
}

int send_tcp_message(uint8_t *msg, size_t msg_size, int socket_id)
{
    int bytes_sent = write(socket_id, msg, msg_size);
    if (bytes_sent < 0)
    {
        log_error("TCP Client: error sending msg to server => %s", strerror(errno));
        return -1;
    }

    return bytes_sent;
}

int receive_tcp_message(uint8_t *msg_buffer, size_t buffer_size, int socket_id)
{
    int bytes_received = read(socket_id, msg_buffer, buffer_size);

    if (bytes_received < 0 && errno != EAGAIN && errno != EWOULDBLOCK)
    {
        return -1;
    }
    else if (bytes_received >= 0)
    {
        msg_buffer[bytes_received] = 0;
    }

    return bytes_received;
}

int close_tcp_connection(int socket_id)
{
    return close(socket_id);
}
