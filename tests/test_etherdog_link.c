// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file test_etherdog_link.c
 * @brief Unit tests for the EtherDOG client: session file, control replies and data frames.
 */

#include "etherdog_link.h"
#include "unity.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

TEST_SOURCE_FILE("core/src/drivers/plugins/native/cjson/cJSON.c")

static const char *SESSION = "test_etherdog_link_session.json";
static edl_link_t lk;
static int peer = -1;

void setUp(void)
{
    edl_init(&lk);
    peer = -1;
}

void tearDown(void)
{
    edl_close(&lk);
    if (peer >= 0)
        close(peer);
    remove(SESSION);
}

static void write_session(const char *json)
{
    FILE *fp = fopen(SESSION, "w");
    TEST_ASSERT_NOT_NULL(fp);
    fputs(json, fp);
    fclose(fp);
}

static void put_le(uint8_t *p, uint64_t v, int n)
{
    for (int i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * i));
}

/* An input frame as EtherDOG sends it. */
static size_t make_frame(uint8_t *buf, uint8_t kind, uint8_t master, uint64_t session,
                         uint16_t len, uint8_t flags)
{
    memcpy(buf, "EDOG", 4);
    buf[4] = 1;
    buf[5] = kind;
    buf[6] = master;
    buf[7] = flags;
    put_le(buf + 8, session, 8);
    put_le(buf + 16, 1, 4);
    put_le(buf + 20, len, 2);
    put_le(buf + 22, 3, 2);
    for (int i = 0; i < len; i++)
        buf[EDL_FRAME_HEADER + i] = (uint8_t)(0xA0 + i);
    return EDL_FRAME_HEADER + len;
}

static void open_data_pair(uint64_t session)
{
    int sv[2];
    TEST_ASSERT_EQUAL_INT(0, socketpair(AF_UNIX, SOCK_DGRAM, 0, sv));
    lk.data_fd = sv[0];
    peer = sv[1];
    lk.open[0] = true;
    lk.session[0] = session;
}

/* --- session file ------------------------------------------------------------------------ */

void test_session_reads_endpoint_transport_and_busconfig(void)
{
    write_session("{\"control\":\"unix:/run/x.sock\",\"data\":\"udp\",\"busconfig\":\"/b.json\"}");
    edl_session_t s;
    char err[256];
    TEST_ASSERT_EQUAL_INT(0, edl_read_session(SESSION, &s, err, sizeof(err)));
    TEST_ASSERT_EQUAL_STRING("unix:/run/x.sock", s.control);
    TEST_ASSERT_TRUE(s.udp);
    TEST_ASSERT_EQUAL_STRING("/b.json", s.busconfig);
    TEST_ASSERT_EQUAL_STRING("", s.token);
}

void test_session_disabled_returns_reason(void)
{
    write_session("{\"disabled\":\"Npcap is not installed\"}");
    edl_session_t s;
    char err[256];
    TEST_ASSERT_EQUAL_INT(EDL_DISABLED, edl_read_session(SESSION, &s, err, sizeof(err)));
    TEST_ASSERT_NOT_NULL(strstr(err, "Npcap is not installed"));
}

void test_session_missing_file_fails(void)
{
    edl_session_t s;
    char err[256];
    TEST_ASSERT_EQUAL_INT(-1, edl_read_session("/nonexistent/etherdog.json", &s, err, sizeof(err)));
}

/* --- control replies --------------------------------------------------------------------- */

#define BIG_REPLY (1024 * 1024)

static void *big_reply_server(void *arg)
{
    int fd = *(int *)arg;
    char req[256];
    if (recv(fd, req, sizeof(req), 0) <= 0)
        return NULL;
    char *line = malloc(BIG_REPLY + 1);
    memset(line, 'x', BIG_REPLY);
    line[0] = '"';
    line[BIG_REPLY - 1] = '"';
    line[BIG_REPLY] = '\n';
    size_t off = 0;
    while (off < BIG_REPLY + 1) {
        ssize_t n = send(fd, line + off, BIG_REPLY + 1 - off, 0);
        if (n <= 0)
            break;
        off += (size_t)n;
    }
    free(line);
    return NULL;
}

void test_call_returns_reply_longer_than_initial_buffer(void)
{
    int sv[2];
    TEST_ASSERT_EQUAL_INT(0, socketpair(AF_UNIX, SOCK_STREAM, 0, sv));
    lk.ctl_fd = sv[0];
    peer = sv[1];
    pthread_t t;
    pthread_create(&t, NULL, big_reply_server, &peer);

    char *reply = NULL;
    TEST_ASSERT_EQUAL_INT(0, edl_call(&lk, "{\"command\":\"layout\"}", &reply, 5000));
    pthread_join(t, NULL);
    TEST_ASSERT_NOT_NULL(reply);
    TEST_ASSERT_EQUAL_size_t(BIG_REPLY, strlen(reply));
    free(reply);
}

void test_call_times_out_without_reply(void)
{
    int sv[2];
    TEST_ASSERT_EQUAL_INT(0, socketpair(AF_UNIX, SOCK_STREAM, 0, sv));
    lk.ctl_fd = sv[0];
    peer = sv[1];
    char *reply = NULL;
    TEST_ASSERT_EQUAL_INT(-1, edl_call(&lk, "{\"command\":\"status\"}", &reply, 50));
    TEST_ASSERT_NULL(reply);
}

/* --- data frames ------------------------------------------------------------------------- */

void test_recv_accepts_valid_input_frame(void)
{
    open_data_pair(0x1122334455667788ull);
    uint8_t buf[EDL_FRAME_HEADER + 8];
    size_t n = make_frame(buf, 2, 0, 0x1122334455667788ull, 4, EDL_FLAG_VALID | EDL_FLAG_WKC_OK);
    TEST_ASSERT_EQUAL_INT((int)n, (int)send(peer, buf, n, 0));

    uint8_t frame[EDL_FRAME_HEADER + EDL_MAX_PAYLOAD];
    int master = -1;
    uint8_t flags = 0;
    const uint8_t *payload = NULL;
    size_t len = 0;
    TEST_ASSERT_EQUAL_INT(1, edl_recv_inputs(&lk, frame, sizeof(frame), 100, &master, &flags,
                                             &payload, &len));
    TEST_ASSERT_EQUAL_INT(0, master);
    TEST_ASSERT_EQUAL_HEX8(EDL_FLAG_VALID | EDL_FLAG_WKC_OK, flags);
    TEST_ASSERT_EQUAL_size_t(4, len);
    TEST_ASSERT_EQUAL_HEX8(0xA0, payload[0]);
    TEST_ASSERT_EQUAL_HEX8(0xA3, payload[3]);
}

void test_recv_drops_bad_frames(void)
{
    open_data_pair(42);
    uint8_t buf[EDL_FRAME_HEADER + 8];
    uint8_t frame[EDL_FRAME_HEADER + EDL_MAX_PAYLOAD];
    int master;
    uint8_t flags;
    const uint8_t *payload;
    size_t len;

    size_t n = make_frame(buf, 2, 0, 43, 4, EDL_FLAG_VALID); /* wrong session */
    send(peer, buf, n, 0);
    TEST_ASSERT_EQUAL_INT(0, edl_recv_inputs(&lk, frame, sizeof(frame), 100, &master, &flags,
                                             &payload, &len));

    n = make_frame(buf, 1, 0, 42, 4, EDL_FLAG_VALID); /* outputs kind */
    send(peer, buf, n, 0);
    TEST_ASSERT_EQUAL_INT(0, edl_recv_inputs(&lk, frame, sizeof(frame), 100, &master, &flags,
                                             &payload, &len));

    n = make_frame(buf, 2, 0, 42, 4, EDL_FLAG_VALID);
    buf[0] = 'X'; /* bad magic */
    send(peer, buf, n, 0);
    TEST_ASSERT_EQUAL_INT(0, edl_recv_inputs(&lk, frame, sizeof(frame), 100, &master, &flags,
                                             &payload, &len));

    n = make_frame(buf, 2, 0, 42, 4, EDL_FLAG_VALID);
    send(peer, buf, n - 1, 0); /* length does not match the header */
    TEST_ASSERT_EQUAL_INT(0, edl_recv_inputs(&lk, frame, sizeof(frame), 100, &master, &flags,
                                             &payload, &len));

    n = make_frame(buf, 2, 1, 42, 4, EDL_FLAG_VALID); /* master with no session */
    send(peer, buf, n, 0);
    TEST_ASSERT_EQUAL_INT(0, edl_recv_inputs(&lk, frame, sizeof(frame), 100, &master, &flags,
                                             &payload, &len));
}

void test_recv_times_out(void)
{
    open_data_pair(1);
    uint8_t frame[EDL_FRAME_HEADER + EDL_MAX_PAYLOAD];
    int master;
    uint8_t flags;
    const uint8_t *payload;
    size_t len;
    TEST_ASSERT_EQUAL_INT(0, edl_recv_inputs(&lk, frame, sizeof(frame), 20, &master, &flags,
                                             &payload, &len));
}

void test_send_outputs_encodes_header(void)
{
    int sv[2];
    TEST_ASSERT_EQUAL_INT(0, socketpair(AF_UNIX, SOCK_DGRAM, 0, sv));
    lk.data_fd = sv[0];
    peer = sv[1];
    lk.open[0] = true;
    lk.session[0] = 0xABCDull;
    lk.server_len[0] = 0; /* connected pair: no destination needed */

    const uint8_t out[3] = { 1, 2, 3 };
    TEST_ASSERT_EQUAL_INT(0, edl_send_outputs(&lk, 0, out, sizeof(out), true));
    uint8_t buf[64];
    ssize_t n = recv(peer, buf, sizeof(buf), 0);
    TEST_ASSERT_EQUAL_INT(EDL_FRAME_HEADER + 3, (int)n);
    TEST_ASSERT_EQUAL_MEMORY("EDOG", buf, 4);
    TEST_ASSERT_EQUAL_UINT8(1, buf[5]); /* outputs */
    TEST_ASSERT_EQUAL_HEX8(EDL_FLAG_VALID, buf[7]);
    TEST_ASSERT_EQUAL_HEX8(0xCD, buf[8]);
    TEST_ASSERT_EQUAL_HEX8(0xAB, buf[9]);
    TEST_ASSERT_EQUAL_UINT8(1, buf[16]); /* first sequence */
    TEST_ASSERT_EQUAL_UINT8(3, buf[20]);
    TEST_ASSERT_EQUAL_MEMORY(out, buf + EDL_FRAME_HEADER, 3);

    TEST_ASSERT_EQUAL_INT(-1, edl_send_outputs(&lk, 1, out, sizeof(out), true)); /* not open */
    TEST_ASSERT_EQUAL_INT(-1, edl_send_outputs(&lk, 0, out, EDL_MAX_PAYLOAD + 1, true));
}
