// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file test_ethercat_relay.c
 * @brief EtherCAT plugin against a fake EtherDOG: start, inputs, reconnect, stop.
 */

#include "etherdog_link.h"
#include "plugin_types.h"
#include "unity.h"

#include <poll.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

TEST_SOURCE_FILE("core/src/drivers/plugins/native/cjson/cJSON.c")
TEST_SOURCE_FILE("core/src/drivers/plugins/native/plugin_logger.c")
TEST_SOURCE_FILE("core/src/drivers/plugins/native/ethercat/etherdog_link.c")
TEST_SOURCE_FILE("core/src/drivers/plugins/native/ethercat/ethercat_iomap.c")
TEST_SOURCE_FILE("core/src/drivers/plugins/native/ethercat/ethercat_plugin.c")

int init(void *args);
int start_loop(void);
void stop_loop(void);

#define SESSION_ID 0xABull

static const char *LAYOUT =
    "{\"status\":\"success\",\"masters\":[{\"index\":0,\"name\":\"m0\",\"state\":\"OPERATIONAL\","
    "\"ready\":true,\"output_bytes\":1,\"input_bytes\":1,\"entries\":["
    "{\"slave\":1,\"pdo\":\"0x1a00\",\"index\":\"0x6000\",\"subindex\":1,\"direction\":\"input\","
    "\"bit_offset\":0,\"bit_length\":1,\"data_type\":\"BOOL\",\"name\":\"In1\"}]}]}";

static char ctl_path[108], data_path[108], session_path[128], mapping_path[128];

/* --- fake EtherDOG ----------------------------------------------------------------------- */

static int listen_fd = -1;
static int data_fd = -1;
static struct sockaddr_un client_addr;
static atomic_bool serving, feeding, have_client;
static atomic_int n_configure, n_start, n_stop;
static pthread_t ctl_thread, feed_thread;

static void reply(int fd, const char *text)
{
    send(fd, text, strlen(text), 0);
    send(fd, "\n", 1, 0);
}

static void handle(int fd, const char *line)
{
    if (strstr(line, "\"hello\"")) {
        reply(fd, "{\"status\":\"success\",\"name\":\"EtherDOG\"}");
    } else if (strstr(line, "\"configure\"")) {
        atomic_fetch_add(&n_configure, 1);
        reply(fd, "{\"status\":\"success\",\"masters\":[{\"index\":0,\"name\":\"m0\",\"task_priority\":50}]}");
    } else if (strstr(line, "\"start\"")) {
        atomic_fetch_add(&n_start, 1);
        reply(fd, "{\"status\":\"success\",\"started\":1,\"total\":1}");
    } else if (strstr(line, "\"layout\"")) {
        reply(fd, LAYOUT);
    } else if (strstr(line, "\"open_data\"")) {
        const char *ep = strstr(line, "unix:");
        memset(&client_addr, 0, sizeof(client_addr));
        client_addr.sun_family = AF_UNIX;
        size_t i = 0;
        for (ep += 5; *ep && *ep != '"' && i < sizeof(client_addr.sun_path) - 1; ep++)
            client_addr.sun_path[i++] = *ep;
        atomic_store(&have_client, true);
        atomic_store(&feeding, true);
        char text[256];
        snprintf(text, sizeof(text),
                 "{\"status\":\"success\",\"masters\":[{\"index\":0,\"endpoint\":\"unix:%s\","
                 "\"session\":\"%016llx\"}]}",
                 data_path, (unsigned long long)SESSION_ID);
        reply(fd, text);
    } else if (strstr(line, "\"close_data\"")) {
        atomic_store(&feeding, false);
        reply(fd, "{\"status\":\"success\"}");
    } else if (strstr(line, "\"stop\"")) {
        atomic_fetch_add(&n_stop, 1);
        atomic_store(&feeding, false);
        reply(fd, "{\"status\":\"success\"}");
    } else {
        reply(fd, "{\"error\":\"unknown\"}");
    }
}

static void *serve_control(void *arg)
{
    (void)arg;
    while (atomic_load(&serving)) {
        struct pollfd p = { .fd = listen_fd, .events = POLLIN };
        if (poll(&p, 1, 50) <= 0)
            continue;
        int fd = accept(listen_fd, NULL, NULL);
        if (fd < 0)
            continue;
        char buf[4096];
        size_t used = 0;
        while (atomic_load(&serving)) {
            struct pollfd c = { .fd = fd, .events = POLLIN };
            if (poll(&c, 1, 50) <= 0)
                continue;
            ssize_t n = recv(fd, buf + used, sizeof(buf) - 1 - used, 0);
            if (n <= 0)
                break;
            used += (size_t)n;
            buf[used] = '\0';
            char *nl;
            while ((nl = strchr(buf, '\n')) != NULL) {
                *nl = '\0';
                handle(fd, buf);
                used -= (size_t)(nl + 1 - buf);
                memmove(buf, nl + 1, used + 1);
            }
        }
        close(fd);
    }
    return NULL;
}

static void put_le(uint8_t *p, uint64_t v, int n)
{
    for (int i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * i));
}

/* One input frame every 5 ms while feeding: bit 0 set. */
static void *feed_inputs(void *arg)
{
    (void)arg;
    uint32_t seq = 0;
    while (atomic_load(&serving)) {
        if (atomic_load(&feeding) && atomic_load(&have_client)) {
            uint8_t f[EDL_FRAME_HEADER + 1];
            memcpy(f, "EDOG", 4);
            f[4] = 1;
            f[5] = 2;
            f[6] = 0;
            f[7] = EDL_FLAG_VALID | EDL_FLAG_WKC_OK;
            put_le(f + 8, SESSION_ID, 8);
            put_le(f + 16, ++seq, 4);
            put_le(f + 20, 1, 2);
            put_le(f + 22, 1, 2);
            f[EDL_FRAME_HEADER] = 0x01;
            sendto(data_fd, f, sizeof(f), 0, (struct sockaddr *)&client_addr, sizeof(client_addr));
        }
        usleep(5000);
    }
    return NULL;
}

/* --- fake runtime ------------------------------------------------------------------------ */

#define BUF 8
static IEC_BOOL bool_vals[BUF][8];
static IEC_BOOL *bool_ptrs[BUF][8];
static plugin_runtime_args_t args;
static atomic_int input_bit_writes;
static char warnings[4096];
static pthread_mutex_t warn_lock = PTHREAD_MUTEX_INITIALIZER;

static void noop_lock(void) {}
static int fake_bool(int type, int index, int bit, int value)
{
    (void)type;
    if (index == 0 && bit == 0 && value == 1)
        atomic_fetch_add(&input_bit_writes, 1);
    return 0;
}
static int fake_byte(int t, int i, int v) { (void)t; (void)i; (void)v; return 0; }
static int fake_int(int t, int i, int v) { (void)t; (void)i; (void)v; return 0; }
static int fake_dint(int t, int i, unsigned int v) { (void)t; (void)i; (void)v; return 0; }
static int fake_lint(int t, int i, unsigned long long v) { (void)t; (void)i; (void)v; return 0; }
static void log_quiet(const char *fmt, ...) { (void)fmt; }
static void log_warn(const char *fmt, ...)
{
    char line[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    pthread_mutex_lock(&warn_lock);
    strncat(warnings, line, sizeof(warnings) - strlen(warnings) - 2);
    strcat(warnings, "\n");
    pthread_mutex_unlock(&warn_lock);
}

static bool wait_for(atomic_int *counter, int at_least, int timeout_ms)
{
    for (int t = 0; t < timeout_ms; t += 10) {
        if (atomic_load(counter) >= at_least)
            return true;
        usleep(10000);
    }
    return false;
}

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

void setUp(void)
{
    long pid = (long)getpid();
    snprintf(ctl_path, sizeof(ctl_path), "/tmp/edog-relay-ctl-%ld.sock", pid);
    snprintf(data_path, sizeof(data_path), "/tmp/edog-relay-data-%ld.sock", pid);
    snprintf(session_path, sizeof(session_path), "/tmp/edog-relay-session-%ld.json", pid);
    snprintf(mapping_path, sizeof(mapping_path), "/tmp/edog-relay-map-%ld.json", pid);
    unlink(ctl_path);
    unlink(data_path);

    FILE *fp = fopen(session_path, "w");
    fprintf(fp, "{\"control\":\"unix:%s\",\"data\":\"unix\",\"busconfig\":\"/tmp/bus.json\"}", ctl_path);
    fclose(fp);
    fp = fopen(mapping_path, "w");
    fprintf(fp, "{\"version\":1,\"masters\":[{\"name\":\"m0\",\"entries\":[{\"slave\":1,"
                "\"index\":\"0x6000\",\"subindex\":1,\"iec_location\":\"%%IX0.0\"}]}]}");
    fclose(fp);
    setenv("ETHERDOG_SESSION_FILE", session_path, 1);

    struct sockaddr_un a;
    memset(&a, 0, sizeof(a));
    a.sun_family = AF_UNIX;
    snprintf(a.sun_path, sizeof(a.sun_path), "%s", ctl_path);
    listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    TEST_ASSERT_EQUAL_INT(0, bind(listen_fd, (struct sockaddr *)&a, sizeof(a)));
    TEST_ASSERT_EQUAL_INT(0, listen(listen_fd, 4));
    snprintf(a.sun_path, sizeof(a.sun_path), "%s", data_path);
    data_fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    TEST_ASSERT_EQUAL_INT(0, bind(data_fd, (struct sockaddr *)&a, sizeof(a)));

    atomic_store(&serving, true);
    atomic_store(&feeding, false);
    atomic_store(&have_client, false);
    atomic_store(&n_configure, 0);
    atomic_store(&n_start, 0);
    atomic_store(&n_stop, 0);
    atomic_store(&input_bit_writes, 0);
    warnings[0] = '\0';
    pthread_create(&ctl_thread, NULL, serve_control, NULL);
    pthread_create(&feed_thread, NULL, feed_inputs, NULL);

    memset(&args, 0, sizeof(args));
    for (int i = 0; i < BUF; i++)
        for (int b = 0; b < 8; b++)
            bool_ptrs[i][b] = &bool_vals[i][b];
    args.bool_input = bool_ptrs;
    args.bool_output = bool_ptrs;
    args.buffer_size = BUF;
    args.image_lock = noop_lock;
    args.image_unlock = noop_lock;
    args.journal_write_bool = fake_bool;
    args.journal_write_byte = fake_byte;
    args.journal_write_int = fake_int;
    args.journal_write_dint = fake_dint;
    args.journal_write_lint = fake_lint;
    args.log_info = log_quiet;
    args.log_debug = log_quiet;
    args.log_warn = log_warn;
    args.log_error = log_warn;
    snprintf(args.plugin_specific_config_file_path, sizeof(args.plugin_specific_config_file_path),
             "%s", mapping_path);
}

void tearDown(void)
{
    atomic_store(&serving, false);
    pthread_join(ctl_thread, NULL);
    pthread_join(feed_thread, NULL);
    close(listen_fd);
    close(data_fd);
    unlink(ctl_path);
    unlink(data_path);
    unlink(session_path);
    unlink(mapping_path);
}

void test_start_configures_starts_and_publishes_inputs(void)
{
    TEST_ASSERT_EQUAL_INT(0, init(&args));
    TEST_ASSERT_EQUAL_INT(0, start_loop());
    TEST_ASSERT_EQUAL_INT(1, atomic_load(&n_configure));
    TEST_ASSERT_EQUAL_INT(1, atomic_load(&n_start));
    TEST_ASSERT_TRUE(wait_for(&input_bit_writes, 3, 2000));

    double t0 = now_s();
    stop_loop();
    TEST_ASSERT_TRUE(now_s() - t0 < 1.0);
    TEST_ASSERT_EQUAL_INT(1, atomic_load(&n_stop));
}

void test_relay_reconnects_and_warns_when_etherdog_goes_quiet(void)
{
    TEST_ASSERT_EQUAL_INT(0, init(&args));
    TEST_ASSERT_EQUAL_INT(0, start_loop());
    TEST_ASSERT_TRUE(wait_for(&input_bit_writes, 1, 2000));

    atomic_store(&feeding, false); /* EtherDOG stops sending: the relay must notice */
    TEST_ASSERT_TRUE(wait_for(&n_start, 2, 4000));
    TEST_ASSERT_EQUAL_INT(2, atomic_load(&n_configure));
    int before = atomic_load(&input_bit_writes);
    TEST_ASSERT_TRUE(wait_for(&input_bit_writes, before + 3, 2000));

    stop_loop();
    pthread_mutex_lock(&warn_lock);
    TEST_ASSERT_NOT_NULL(strstr(warnings, "link to EtherDOG lost"));
    pthread_mutex_unlock(&warn_lock);
}

void test_start_without_etherdog_fails_cleanly(void)
{
    atomic_store(&serving, false);
    pthread_join(ctl_thread, NULL);
    close(listen_fd);
    listen_fd = socket(AF_UNIX, SOCK_STREAM, 0); /* tearDown closes it */
    unlink(ctl_path);
    atomic_store(&serving, true);
    pthread_create(&ctl_thread, NULL, serve_control, NULL);

    TEST_ASSERT_EQUAL_INT(0, init(&args));
    TEST_ASSERT_EQUAL_INT(-1, start_loop());
}
