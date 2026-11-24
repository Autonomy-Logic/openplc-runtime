// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

// Host test of the manifest checks: linked with one fake_access/hmi_access_bad.c variant (HMI_BAD).
// A bad manifest must leave the HMI out of date with no debug read or write; the valid base
// (HMI_BAD 0) must be live, as the negative control. Built and run by tests/host/hmi/run.sh.

#include "plugin_types.h"

#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>

extern "C" {
int init(void *args);
int start_loop(void);
void stop_loop(void);
void cleanup(void);
}

static std::atomic<int> g_access{0};

static uint16_t dbg_size(uint8_t, uint16_t)
{
    g_access++;
    return 2;
}

static uint16_t dbg_read(uint8_t, uint16_t, uint8_t *dest)
{
    g_access++;
    dest[0] = dest[1] = 0;
    return 2;
}

static uint8_t dbg_write(uint8_t, uint16_t, const uint8_t *, uint16_t)
{
    g_access++;
    return 0x7E;
}

static void log_any(const char *, ...) {}

static int connect_host()
{
    for (int i = 0; i < 200; i++)
    {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        std::snprintf(addr.sun_path, sizeof addr.sun_path, "%s", HMI_SOCKET_PATH);
        if (connect(fd, (sockaddr *)&addr, sizeof addr) == 0)
            return fd;
        close(fd);
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    return -1;
}

/** The type and first payload byte of the next message, or -1 on timeout. */
static int next_message(int fd, uint8_t *first)
{
    uint8_t buf[64];
    size_t got = 0;
    while (got < 6)
    {
        pollfd pfd{fd, POLLIN, 0};
        if (poll(&pfd, 1, 500) <= 0)
            return -1;
        ssize_t r = read(fd, buf + got, sizeof buf - got);
        if (r <= 0)
            return -1;
        got += (size_t)r;
    }
    *first = buf[5];
    return buf[4];
}

int main()
{
    unlink(HMI_SOCKET_PATH);
    plugin_runtime_args_t a;
    std::memset(&a, 0, sizeof a);
    a.debug_size  = dbg_size;
    a.debug_read  = dbg_read;
    a.debug_write = dbg_write;
    a.log_info = a.log_debug = a.log_warn = a.log_error = log_any;
    std::snprintf(a.program_md5, sizeof a.program_md5, "%s", "0123456789abcdef0123456789abcdef");
    if (init(&a) != 0 || start_loop() != 0)
    {
        std::printf("  FAIL  HMI_BAD %d: the plugin did not start\n", HMI_BAD);
        return 1;
    }
    int fd = connect_host();
    uint8_t state = 0xFF;
    const int type = next_message(fd, &state);
    const bool live = type == 0x81 && state == 0;
    std::vector<uint8_t> open = {7, 0, 0, 0, 0x01, 1, 0, 0, 0, 0, 0};
    (void)!::write(fd, open.data(), open.size());
    /* SHOW 0 and WRITE speed 1 for viewer 1: served and applied only when live. */
    std::vector<uint8_t> show  = {7, 0, 0, 0, 0x03, 1, 0, 0, 0, 0, 0};
    std::vector<uint8_t> write_speed = {11, 0, 0, 0, 0x04, 1, 0, 0, 0, 1, 0, 2, 0, 1, 0};
    (void)!::write(fd, show.data(), show.size());
    (void)!::write(fd, write_speed.data(), write_speed.size());
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    const int touched = g_access.load();
    close(fd);
    stop_loop();
    cleanup();
    const bool ok = HMI_BAD == 0 ? live && touched > 0 : !live && touched == 0;
    std::printf("  %s  HMI_BAD %d: %s, %d debug call(s)\n", ok ? "ok  " : "FAIL", HMI_BAD,
                live ? "live" : "out of date", touched);
    return ok ? 0 : 1;
}
