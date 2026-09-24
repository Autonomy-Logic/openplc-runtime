// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file etherdog_link.h
 * @brief Client side of the EtherDOG protocol: JSON control lines and cyclic data datagrams.
 *
 * The session file written by the webserver gives the control endpoint and the token.
 */

#ifndef ETHERDOG_LINK_H
#define ETHERDOG_LINK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/socket.h>

#define EDL_MAX_MASTERS 4
#define EDL_FRAME_HEADER 24
#define EDL_MAX_PAYLOAD 4096

/** Default location of the session file the webserver writes before starting plc_main. */
#define EDL_SESSION_FILE "/run/runtime/etherdog.json"

typedef struct {
    char control[160]; /* "unix:<path>" or "tcp:127.0.0.1:<port>" */
    char token[160];
    bool udp;          /* data transport: loopback UDP instead of AF_UNIX */
} edl_session_t;

typedef struct {
    int ctl_fd;
    char rbuf[64 * 1024];
    size_t rlen;

    int data_fd;
    char data_path[108];
    struct sockaddr_storage server[EDL_MAX_MASTERS];
    socklen_t server_len[EDL_MAX_MASTERS];
    uint64_t session[EDL_MAX_MASTERS];
    uint32_t tx_seq[EDL_MAX_MASTERS];
    bool open[EDL_MAX_MASTERS];
} edl_link_t;

/** edl_read_session: the webserver disabled EtherDOG; @p err holds the reason. */
#define EDL_DISABLED (-2)

/** Read the webserver's session file. Returns 0, EDL_DISABLED or -1, with @p err filled. */
int edl_read_session(const char *path, edl_session_t *out, char *err, size_t err_size);

void edl_init(edl_link_t *link);

/** Connect the control socket and authenticate with "hello". */
int edl_connect(edl_link_t *link, const edl_session_t *session, char *err, size_t err_size);

/** Send one request line and read one response line. Returns 0, or -1 on I/O failure. */
int edl_call(edl_link_t *link, const char *request, char *response, size_t response_size,
             int timeout_ms);

/**
 * @brief Bind a local datagram socket and ask EtherDOG to open the data session.
 *
 * On success every running master listed in the reply has its endpoint and session stored.
 */
int edl_open_data(edl_link_t *link, const edl_session_t *session, const char *local_dir,
                  char *err, size_t err_size);

/**
 * @brief Wait for one valid input frame.
 * @return 1 on a frame (outputs filled), 0 on timeout, -1 on socket error.
 */
int edl_recv_inputs(edl_link_t *link, uint8_t *buf, size_t buf_size, int timeout_ms,
                    int *master_out, uint8_t *flags_out, const uint8_t **payload_out,
                    size_t *len_out);

/** Send one output frame to @p master. @p valid false asks EtherDOG for the safe state. */
int edl_send_outputs(edl_link_t *link, int master, const uint8_t *payload, size_t len,
                     bool valid);

/** Close the data socket (and unlink its path) and the control connection. */
void edl_close(edl_link_t *link);

/** Input frame flags. */
#define EDL_FLAG_VALID 0x01
#define EDL_FLAG_WKC_OK 0x02

#endif /* ETHERDOG_LINK_H */
