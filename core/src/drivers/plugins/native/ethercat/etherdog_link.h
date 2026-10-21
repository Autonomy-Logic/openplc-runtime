// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file etherdog_link.h
 * @brief Client side of the EtherDOG protocol: JSON control lines and cyclic data datagrams.
 *
 * The session file written by the webserver gives the control endpoint, the data transport and
 * the bus configuration to load when the bus starts.
 */

#ifndef ETHERDOG_LINK_H
#define ETHERDOG_LINK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/socket.h>

#define EDL_MAX_MASTERS 4
#define EDL_FRAME_HEADER 24
/**
 * Largest payload one data datagram carries: what fits a loopback UDP datagram (65507 bytes)
 * after the frame header, within the header's 16-bit length field. A master whose process image
 * exceeds it in one direction is refused when the mapping is bound.
 */
#define EDL_MAX_PAYLOAD (65507u - EDL_FRAME_HEADER)
/** Largest control reply accepted, far above any real bus. */
#define EDL_MAX_REPLY (64u * 1024u * 1024u)

/** Default location of the session file the webserver writes before starting plc_main. */
#define EDL_SESSION_FILE "/run/runtime/etherdog.json"

typedef struct {
    char control[160];   /* "unix:<path>" or "tcp:127.0.0.1:<port>" */
    char token[160];     /* optional */
    char busconfig[512]; /* bus configuration file, "" when there is none */
    bool udp;            /* data transport: loopback UDP instead of AF_UNIX */
} edl_session_t;

typedef struct {
    int ctl_fd;
    char *rbuf; /* grows with the longest reply line */
    size_t rcap;
    size_t rlen;

    int data_fd;
    char data_path[108];
    struct sockaddr_storage server[EDL_MAX_MASTERS];
    socklen_t server_len[EDL_MAX_MASTERS];
    uint64_t session[EDL_MAX_MASTERS];
    uint32_t tx_seq[EDL_MAX_MASTERS];
    bool open[EDL_MAX_MASTERS];
    uint8_t *tx_frame; /* output frame buffer, sized by edl_reserve_outputs */
    size_t tx_payload_cap;
} edl_link_t;

/** edl_read_session: the webserver disabled EtherDOG; @p err holds the reason. */
#define EDL_DISABLED (-2)

/** Read the webserver's session file. Returns 0, EDL_DISABLED or -1, with @p err filled. */
int edl_read_session(const char *path, edl_session_t *out, char *err, size_t err_size);

void edl_init(edl_link_t *link);

/** Connect the control socket and send "hello" (with the token, if any). */
int edl_connect(edl_link_t *link, const edl_session_t *session, char *err, size_t err_size);

/**
 * @brief Send one request line and read one reply line of any length (up to EDL_MAX_REPLY).
 * @param response set to the reply, heap-allocated; the caller frees it
 * @return 0, or -1 on I/O failure, timeout or a reply over EDL_MAX_REPLY
 */
int edl_call(edl_link_t *link, const char *request, char **response, int timeout_ms);

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

/**
 * @brief Size the output frame buffer for payloads up to @p max_payload bytes.
 * @return 0, or -1 when @p max_payload exceeds EDL_MAX_PAYLOAD or memory runs out
 */
int edl_reserve_outputs(edl_link_t *link, size_t max_payload);

/**
 * Send one output frame to @p master. @p valid false asks EtherDOG for the safe state.
 * @p len must not exceed the size given to edl_reserve_outputs.
 */
int edl_send_outputs(edl_link_t *link, int master, const uint8_t *payload, size_t len,
                     bool valid);

/** Close the data socket (and unlink its path) and the control connection; frees buffers. */
void edl_close(edl_link_t *link);

/** Input frame flags. */
#define EDL_FLAG_VALID 0x01
#define EDL_FLAG_WKC_OK 0x02

#endif /* ETHERDOG_LINK_H */
