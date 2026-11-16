// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

// Host test of the HMI plugin over its Unix socket: links the real host, the editor's HMI core
// and the fake unit, fakes the runtime's debug surface and logging, and plays the web server.
// Built and run by tests/host/hmi/run.sh, which defines HMI_SOCKET_PATH.

#include "plugin_types.h"

#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

extern "C" {
int init(void *args);
int start_loop(void);
void stop_loop(void);
void cleanup(void);
void hmi_fake_fail_creates(int n);
void hmi_fake_last_press(int32_t *x, int32_t *y);
}

static int g_failures = 0;

#define CHECK(cond, what)                                                                          \
    do                                                                                             \
    {                                                                                              \
        if (!(cond))                                                                               \
        {                                                                                          \
            std::printf("  FAIL  %s (line %d)\n", what, __LINE__);                                \
            g_failures++;                                                                          \
        }                                                                                          \
        else                                                                                       \
        {                                                                                          \
            std::printf("  ok    %s\n", what);                                                     \
        }                                                                                          \
    } while (0)

/* ---- fake runtime -------------------------------------------------------- */

static std::mutex g_mu;
static uint8_t g_lamp;
static int16_t g_speed;
static std::string g_label;
static std::vector<std::string> g_logs;
static std::vector<std::vector<uint8_t>> g_writes; // elem, then the debug bytes
static std::atomic<int> g_reads{0};

static const uint16_t STRING_WIDTH = 127; // STruC++ DEBUG_STRING_WIDTH

static uint16_t dbg_size(uint8_t arr, uint16_t elem)
{
    if (arr != 0)
        return 0;
    switch (elem)
    {
    case 1:
        return 1;
    case 2:
        return 2;
    case 3:
        return STRING_WIDTH;
    case 4:
        return 4;
    default:
        return 0;
    }
}

static uint16_t dbg_read(uint8_t arr, uint16_t elem, uint8_t *dest)
{
    g_reads++;
    std::lock_guard<std::mutex> lk(g_mu);
    const uint16_t n = dbg_size(arr, elem);
    switch (elem)
    {
    case 1:
        dest[0] = g_lamp;
        break;
    case 2:
        std::memcpy(dest, &g_speed, 2);
        break;
    case 3:
        std::memset(dest, 0, STRING_WIDTH);
        dest[0] = (uint8_t)g_label.size();
        std::memcpy(dest + 1, g_label.data(), g_label.size());
        break;
    case 4:
    {
        const float t = 21.5f;
        std::memcpy(dest, &t, 4);
        break;
    }
    default:
        return 0;
    }
    return n;
}

static uint8_t dbg_write(uint8_t arr, uint16_t elem, const uint8_t *bytes, uint16_t len)
{
    std::lock_guard<std::mutex> lk(g_mu);
    if (len != dbg_size(arr, elem))
        return 0x81;
    std::vector<uint8_t> rec;
    rec.push_back((uint8_t)elem);
    rec.insert(rec.end(), bytes, bytes + len);
    g_writes.push_back(rec);
    if (elem == 1)
        g_lamp = bytes[0];
    else if (elem == 2)
        std::memcpy(&g_speed, bytes, 2);
    else if (elem == 3)
        g_label.assign((const char *)bytes + 1, bytes[0]);
    return 0x7E;
}

static void log_any(const char *fmt, ...)
{
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    std::vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    std::lock_guard<std::mutex> lk(g_mu);
    g_logs.push_back(buf);
}

static bool logged(const char *needle)
{
    std::lock_guard<std::mutex> lk(g_mu);
    for (const std::string &l : g_logs)
        if (l.find(needle) != std::string::npos)
            return true;
    return false;
}

static size_t count_logged(const char *needle)
{
    std::lock_guard<std::mutex> lk(g_mu);
    size_t n = 0;
    for (const std::string &l : g_logs)
        if (l.find(needle) != std::string::npos)
            n++;
    return n;
}

static bool wait_for(bool (*pred)(), int ms)
{
    for (int i = 0; i < ms / 5; i++)
    {
        if (pred())
            return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    return pred();
}

static plugin_runtime_args_t make_args(const char *md5)
{
    plugin_runtime_args_t a;
    std::memset(&a, 0, sizeof a);
    a.debug_size  = dbg_size;
    a.debug_read  = dbg_read;
    a.debug_write = dbg_write;
    a.log_info    = log_any;
    a.log_debug   = log_any;
    a.log_warn    = log_any;
    a.log_error   = log_any;
    std::snprintf(a.program_md5, sizeof a.program_md5, "%s", md5);
    return a;
}

/* ---- web server side ----------------------------------------------------- */

struct Msg
{
    uint8_t type = 0;
    std::vector<uint8_t> payload;
};

static void put_u16(std::vector<uint8_t> &b, uint16_t v)
{
    b.push_back((uint8_t)v);
    b.push_back((uint8_t)(v >> 8));
}

static void put_u32(std::vector<uint8_t> &b, uint32_t v)
{
    for (int i = 0; i < 4; i++)
        b.push_back((uint8_t)(v >> (8 * i)));
}

static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

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

static void send_raw(int fd, const std::vector<uint8_t> &b)
{
    size_t off = 0;
    while (off < b.size())
    {
        ssize_t n = write(fd, b.data() + off, b.size() - off);
        if (n <= 0)
            return;
        off += (size_t)n;
    }
}

static void send_msg(int fd, uint8_t type, const std::vector<uint8_t> &payload)
{
    std::vector<uint8_t> b;
    put_u32(b, (uint32_t)(payload.size() + 1));
    b.push_back(type);
    b.insert(b.end(), payload.begin(), payload.end());
    send_raw(fd, b);
}

static std::vector<uint8_t> open_payload(uint32_t viewer, uint8_t role, uint16_t w, uint16_t h,
                                         uint16_t d, const std::string &name)
{
    std::vector<uint8_t> p;
    put_u32(p, viewer);
    p.push_back(role);
    put_u16(p, w);
    put_u16(p, h);
    put_u16(p, d);
    p.push_back((uint8_t)name.size());
    p.insert(p.end(), name.begin(), name.end());
    return p;
}

static void send_key(int fd, uint32_t viewer, uint32_t code)
{
    std::vector<uint8_t> p;
    put_u32(p, viewer);
    put_u32(p, code);
    send_msg(fd, 0x05, p);
}

static void send_pointer(int fd, uint32_t viewer, uint8_t kind, int32_t x100, int32_t y100)
{
    std::vector<uint8_t> p;
    put_u32(p, viewer);
    p.push_back(kind);
    put_u32(p, (uint32_t)x100);
    put_u32(p, (uint32_t)y100);
    send_msg(fd, 0x04, p);
}

static bool read_exact(int fd, uint8_t *buf, size_t n, int timeout_ms)
{
    size_t got = 0;
    while (got < n)
    {
        pollfd pfd{fd, POLLIN, 0};
        if (poll(&pfd, 1, timeout_ms) <= 0)
            return false;
        ssize_t r = read(fd, buf + got, n - got);
        if (r <= 0)
            return false;
        got += (size_t)r;
    }
    return true;
}

/** Next message, or type 0 on timeout or a closed socket. */
static Msg read_msg(int fd, int timeout_ms = 1000)
{
    Msg m;
    uint8_t hdr[5];
    if (!read_exact(fd, hdr, 5, timeout_ms))
        return m;
    const uint32_t len = get_u32(hdr);
    m.payload.resize(len - 1);
    if (len > 1 && !read_exact(fd, m.payload.data(), len - 1, timeout_ms))
        return Msg{};
    m.type = hdr[4];
    return m;
}

/** Reads until a message of `type` (for `viewer` when given) arrives; type 0 when none does. */
static Msg read_until(int fd, uint8_t type, int64_t viewer = -1, int timeout_ms = 1500)
{
    const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < end)
    {
        Msg m = read_msg(fd, 200);
        if (m.type == 0)
            continue;
        if (m.type != type)
            continue;
        if (viewer >= 0 && (m.payload.size() < 4 || get_u32(m.payload.data()) != (uint32_t)viewer))
            continue;
        return m;
    }
    return Msg{};
}

static bool closed_by_host(int fd)
{
    const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(1500);
    uint8_t buf[4096];
    while (std::chrono::steady_clock::now() < end)
    {
        pollfd pfd{fd, POLLIN, 0};
        if (poll(&pfd, 1, 200) <= 0)
            continue;
        ssize_t r = read(fd, buf, sizeof buf);
        if (r <= 0)
            return true;
    }
    return false;
}

static size_t count_frames(int fd, uint32_t viewer, int ms)
{
    size_t n       = 0;
    const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(ms);
    while (std::chrono::steady_clock::now() < end)
    {
        Msg m = read_msg(fd, 50);
        if (m.type == 0x81 && get_u32(m.payload.data()) == viewer)
            n++;
    }
    return n;
}

static int32_t dl_width(const Msg &frame)
{
    // FRAME payload: u32 viewer, then the display list header "HMDL", u16, u16, i32 width.
    int32_t w;
    std::memcpy(&w, frame.payload.data() + 4 + 8, 4);
    return w;
}

/* ---- cases --------------------------------------------------------------- */

static const char *MD5 = "0123456789abcdef0123456789abcdef";
static const uint32_t FAULT_KEY = 0xE000u; // HMI_FAKE_FAULT_KEY
static const uint32_t SPIN_KEY  = 0xE001u; // HMI_FAKE_SPIN_KEY

static bool press_at(int32_t x, int32_t y)
{
    int32_t px = 0, py = 0;
    hmi_fake_last_press(&px, &py);
    return px == x && py == y;
}

static bool lamp_written()
{
    std::lock_guard<std::mutex> lk(g_mu);
    for (const auto &w : g_writes)
        if (w[0] == 1)
            return true;
    return false;
}

static void test_live_session()
{
    std::printf("live session\n");
    plugin_runtime_args_t args = make_args("0123456789ABCDEF0123456789ABCDEF");
    CHECK(init(&args) == 0, "init");
    CHECK(start_loop() == 0, "start_loop");
    struct stat st;
    CHECK(stat(HMI_SOCKET_DIR, &st) == 0 && S_ISDIR(st.st_mode) && (st.st_mode & 0777) == 0700,
          "the socket's directory is owner-only");
    CHECK(stat(HMI_SOCKET_PATH, &st) == 0 && S_ISSOCK(st.st_mode), "socket created in it");

    int fd = connect_host();
    CHECK(fd >= 0, "connects");
    Msg s = read_msg(fd);
    CHECK(s.type == 0x82 && s.payload.size() == 1 && s.payload[0] == 0, "STATUS live on connect");

    send_msg(fd, 0x01, open_payload(7, 2, 800, 600, 100, "alice"));
    Msg o = read_until(fd, 0x83, 7);
    CHECK(o.type == 0x83, "OPENED 7");
    Msg f = read_until(fd, 0x81, 7);
    CHECK(f.type == 0x81 && f.payload.size() > 24 && std::memcmp(f.payload.data() + 4, "HMDL", 4) == 0,
          "first FRAME carries a display list");
    CHECK(f.type == 0x81 && dl_width(f) == 800, "800 CSS px at density 1.00 is 800 lu");
    CHECK(logged("viewer 7 opened by alice (operator): 1 viewer(s)"), "open logged with count");

    CHECK(count_frames(fd, 7, 200) == 0, "unchanged frames are not sent");

    send_pointer(fd, 7, 0, 1000, 1000);
    send_pointer(fd, 7, 2, 1000, 1000);
    CHECK(wait_for(lamp_written, 1000), "a press writes LAMP through debug_write");
    CHECK(wait_for([] { return press_at(10, 10); }, 1000),
          "a press at 10 CSS px lands at 10 lu");
    CHECK(logged("write by alice: CONFIG0.RES0.INST0.MAIN.LAMP = TRUE"), "write logged with account");
    CHECK(logged("write by alice: CONFIG0.RES0.INST0.MAIN.LABEL = 'pressed'"), "STRING write logged");
    {
        std::lock_guard<std::mutex> lk(g_mu);
        bool label_ok = false;
        for (const auto &w : g_writes)
            if (w[0] == 3 && w.size() == 1u + STRING_WIDTH && w[1] == 7 &&
                std::memcmp(&w[2], "pressed", 7) == 0 && w[9] == 0 && w[STRING_WIDTH] == 0)
                label_ok = true;
        CHECK(label_ok, "STRING written in the debug format: length byte, text, zero fill");
    }
    CHECK(read_until(fd, 0x81, 7).type == 0x81, "a changed screen sends a new FRAME");

    std::vector<uint8_t> key;
    put_u32(key, 7);
    put_u32(key, 65);
    send_msg(fd, 0x05, key);
    CHECK(wait_for([] {
              std::lock_guard<std::mutex> lk(g_mu);
              return g_speed == 65;
          },
                   1000),
          "a key writes SPEED");
    CHECK(logged("CONFIG0.RES0.INST0.MAIN.SPEED = 65"), "INT write logged");

    std::vector<uint8_t> vp;
    put_u32(vp, 7);
    put_u16(vp, 1024);
    put_u16(vp, 768);
    put_u16(vp, 200);
    send_msg(fd, 0x03, vp);
    Msg r = read_until(fd, 0x81, 7);
    CHECK(r.type == 0x81 && dl_width(r) == 1024, "VIEWPORT 1024 px at density 2.00 is 1024 lu");
    send_pointer(fd, 7, 0, 5050, 2500);
    send_pointer(fd, 7, 2, 5050, 2500);
    CHECK(wait_for([] { return press_at(50, 25); }, 1000),
          "density does not scale pointers: 50.5 x 25 CSS px lands at 50 x 25 lu");

    send_msg(fd, 0x01, open_payload(8, 3, 320, 240, 100, "bob"));
    CHECK(read_until(fd, 0x83, 8).type == 0x83, "a second viewer opens");
    CHECK(wait_for([] { return logged("viewer 8 opened by bob (viewer): 2 viewer(s)"); }, 1000),
          "second open logged with count 2");
    std::vector<uint8_t> close8;
    put_u32(close8, 8);
    send_msg(fd, 0x02, close8);
    CHECK(wait_for([] { return logged("viewer 8 closed: 1 viewer(s)"); }, 1000),
          "close logged with count 1");

    hmi_fake_fail_creates(1);
    send_msg(fd, 0x01, open_payload(9, 0, 320, 240, 100, "carol"));
    CHECK(read_until(fd, 0x84, 9).type == 0x84, "allocation failure answers BUSY");

    /* Far more presses than frames can apply: the viewer is dropped with BUSY, none is lost. */
    send_msg(fd, 0x01, open_payload(10, 0, 320, 240, 100, "dave"));
    CHECK(read_until(fd, 0x83, 10).type == 0x83, "viewer 10 opens");
    for (int i = 0; i < 200; i++)
    {
        send_pointer(fd, 10, 0, 100, 100);
        send_pointer(fd, 10, 2, 100, 100);
    }
    CHECK(read_until(fd, 0x84, 10).type == 0x84, "input overflow drops the viewer with BUSY");
    CHECK(wait_for([] { return logged("viewer 10 dropped: its input queue overflowed"); }, 1000),
          "overflow logged");

    /* An IEC fault in one viewer's screens closes that viewer only. */
    send_msg(fd, 0x01, open_payload(20, 2, 320, 240, 100, "grace"));
    CHECK(read_until(fd, 0x83, 20).type == 0x83, "viewer 20 opens");
    send_msg(fd, 0x01, open_payload(21, 2, 320, 240, 100, "heidi"));
    CHECK(read_until(fd, 0x83, 21).type == 0x83, "viewer 21 opens");
    send_key(fd, 20, FAULT_KEY);
    Msg fault = read_until(fd, 0x85, 20);
    CHECK(fault.type == 0x85 && fault.payload.size() == 4, "a screen fault answers FAULT 20");
    CHECK(wait_for([] { return logged("viewer 20 closed: "); }, 1000),
          "the faulted viewer is freed");
    CHECK(count_logged("screen fault for grace") == 1, "the fault is logged once with the account");
    send_key(fd, 20, 4321);
    send_pointer(fd, 20, 0, 100, 100);
    std::vector<uint8_t> vp20;
    put_u32(vp20, 20);
    put_u16(vp20, 640);
    put_u16(vp20, 480);
    put_u16(vp20, 100);
    send_msg(fd, 0x03, vp20);
    send_key(fd, 21, 77);
    CHECK(wait_for([] {
              std::lock_guard<std::mutex> lk(g_mu);
              return g_speed == 77;
          },
                   1000),
          "input after the fault is ignored for 20 and applied for 21");
    {
        size_t f20 = 0, f21 = 0;
        const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(300);
        while (std::chrono::steady_clock::now() < end)
        {
            Msg m = read_msg(fd, 50);
            if (m.type == 0x81 && get_u32(m.payload.data()) == 20)
                f20++;
            if (m.type == 0x81 && get_u32(m.payload.data()) == 21)
                f21++;
        }
        CHECK(f20 == 0 && f21 > 0, "viewer 21 keeps getting frames, 20 gets none");
    }
    CHECK(count_logged("SPEED = 4321") == 0, "the key sent to the faulted viewer wrote nothing");
    send_msg(fd, 0x01, open_payload(20, 2, 320, 240, 100, "grace"));
    CHECK(read_until(fd, 0x83, 20).type == 0x83, "a new OPEN for the faulted id opens it again");
    CHECK(read_until(fd, 0x81, 20).type == 0x81, "the reopened viewer gets frames");
    CHECK(count_logged("screen fault for grace") == 1, "no fault after the reopen");

    send_msg(fd, 0x01, open_payload(7, 2, 800, 600, 100, "alice"));
    CHECK(closed_by_host(fd), "OPEN of an open viewer closes the connection");
    close(fd);
    CHECK(wait_for([] { return logged("malformed message"); }, 1000), "closing logged");

    fd = connect_host();
    CHECK(read_msg(fd).type == 0x82, "reconnect gets STATUS");
    send_raw(fd, {0, 0, 0, 0});
    CHECK(closed_by_host(fd), "a zero length closes the connection");
    close(fd);

    fd = connect_host();
    read_msg(fd);
    send_raw(fd, {2, 0, 0, 0, 0x7F, 0});
    CHECK(closed_by_host(fd), "an unknown type closes the connection");
    close(fd);

    fd = connect_host();
    read_msg(fd);
    send_raw(fd, {0, 0, 0x40, 0, 0x01});
    CHECK(closed_by_host(fd), "an oversized length closes the connection");
    close(fd);

    fd = connect_host();
    read_msg(fd);
    send_msg(fd, 0x01, open_payload(11, 0, 0, 240, 100, "erin"));
    CHECK(closed_by_host(fd), "a zero width closes the connection");
    close(fd);

    fd = connect_host();
    read_msg(fd);
    send_msg(fd, 0x01, open_payload(12, 0, 320, 240, 100, std::string("bad\x01name")));
    CHECK(read_until(fd, 0x84, 12).type == 0x84,
          "a control character in the name refuses that viewer");
    send_msg(fd, 0x01, open_payload(14, 0, 320, 240, 100, "gina"));
    CHECK(read_until(fd, 0x83, 14).type == 0x83,
          "... and the connection still opens other viewers");
    close(fd);

    /* A web server that stops reading never stalls the frame thread. */
    fd = connect_host();
    read_msg(fd);
    send_msg(fd, 0x01, open_payload(13, 0, 320, 240, 100, "frank"));
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    const int reads_before = g_reads.load();
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    CHECK(g_reads.load() > reads_before, "frames keep rendering while the reader is idle");
    close(fd);

    stop_loop();
    CHECK(stat(HMI_SOCKET_PATH, &st) != 0, "stop_loop removes the socket");
    cleanup();
}

static void test_out_of_date()
{
    std::printf("out of date\n");
    for (const char *md5 : {"ffffffffffffffffffffffffffffffff", ""})
    {
        plugin_runtime_args_t args = make_args(md5);
        CHECK(init(&args) == 0, "init");
        CHECK(start_loop() == 0, "start_loop");
        const int reads     = g_reads.load();
        const size_t writes = [] {
            std::lock_guard<std::mutex> lk(g_mu);
            return g_writes.size();
        }();
        int fd = connect_host();
        Msg s  = read_msg(fd);
        CHECK(s.type == 0x82 && s.payload[0] == 1, "STATUS out_of_date on connect");
        send_msg(fd, 0x01, open_payload(1, 0, 320, 240, 100, "alice"));
        Msg r = read_msg(fd);
        CHECK(r.type == 0x82 && r.payload[0] == 1, "OPEN answered with STATUS out_of_date");
        send_pointer(fd, 1, 0, 100, 100);
        CHECK(read_msg(fd, 300).type == 0, "no OPENED and no FRAME");
        const size_t writes_after = [] {
            std::lock_guard<std::mutex> lk(g_mu);
            return g_writes.size();
        }();
        CHECK(g_reads.load() == reads && writes_after == writes, "no debug read or write");
        close(fd);
        stop_loop();
        cleanup();
    }
    (void)MD5;
}

/** Screen code that never returns: stop_loop ends the process past its timeout instead of hanging.
 */
static void test_stuck_screen()
{
    std::printf("stuck screen code\n");
    std::fflush(stdout);
    const pid_t child = fork();
    if (child == 0)
    {
        plugin_runtime_args_t args = make_args("0123456789ABCDEF0123456789ABCDEF");
        if (init(&args) != 0 || start_loop() != 0)
            _exit(2);
        int fd = connect_host();
        read_msg(fd);
        send_msg(fd, 0x01, open_payload(1, 0, 320, 240, 100, "ivan"));
        if (read_until(fd, 0x83, 1).type != 0x83)
            _exit(3);
        send_key(fd, 1, SPIN_KEY);
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
        stop_loop();
        _exit(0);
    }
    CHECK(child > 0, "fork");
    int status       = 0;
    const auto start = std::chrono::steady_clock::now();
    CHECK(waitpid(child, &status, 0) == child, "the child ends");
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::steady_clock::now() - start)
                        .count();
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 43, "stop_loop ends plc_main with status 43");
    CHECK(ms < 5000, "within the stop timeout");
    unlink(HMI_SOCKET_PATH);
}

int main()
{
    unlink(HMI_SOCKET_PATH);
    test_live_session();
    test_out_of_date();
    test_stuck_screen();
    if (g_failures == 0)
        std::printf("all cases passed\n");
    else
        std::printf("%d case(s) failed\n", g_failures);
    return g_failures == 0 ? 0 : 1;
}
