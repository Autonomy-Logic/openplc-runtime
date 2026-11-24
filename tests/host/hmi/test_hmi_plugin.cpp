// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

// Host test of the HMI plugin over its Unix socket: links the real host and a hand-written
// manifest (fake_access/hmi_access.c), fakes the runtime's debug surface and logging, and plays
// the web server. Checks the values served per screen, writes applied and refused by role and
// screen, the request queue bound and the out-of-date gate.
// Built and run by tests/host/hmi/run.sh, which defines HMI_SOCKET_PATH.

#include "plugin_types.h"

#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

extern "C" {
int init(void *args);
int start_loop(void);
void stop_loop(void);
void cleanup(void);
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
static float g_temp = 21.5f;
static int32_t g_flags;

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
    case 5:
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
        std::memcpy(dest, &g_temp, 4);
        break;
    case 5:
        std::memcpy(dest, &g_flags, 4);
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
    else if (elem == 5)
        std::memcpy(&g_flags, bytes, 4);
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

template <typename Pred> static bool wait_for(Pred pred, int ms)
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

static std::vector<uint8_t> open_payload(uint32_t viewer, uint8_t role, const std::string &name)
{
    std::vector<uint8_t> p;
    put_u32(p, viewer);
    p.push_back(role);
    p.push_back((uint8_t)name.size());
    p.insert(p.end(), name.begin(), name.end());
    return p;
}

static void send_show(int fd, uint32_t viewer, uint16_t page)
{
    std::vector<uint8_t> p;
    put_u32(p, viewer);
    put_u16(p, page);
    send_msg(fd, 0x03, p);
}

static std::vector<uint8_t> write_message(uint32_t viewer, uint16_t tag, const std::vector<uint8_t> &value)
{
    std::vector<uint8_t> p;
    put_u32(p, viewer);
    put_u16(p, tag);
    put_u16(p, (uint16_t)value.size());
    p.insert(p.end(), value.begin(), value.end());
    std::vector<uint8_t> b;
    put_u32(b, (uint32_t)(p.size() + 1));
    b.push_back(0x04);
    b.insert(b.end(), p.begin(), p.end());
    return b;
}

static void send_bit(int fd, uint32_t viewer, uint16_t tag, uint8_t bit, uint8_t value)
{
    std::vector<uint8_t> p;
    put_u32(p, viewer);
    put_u16(p, tag);
    p.push_back(bit);
    p.push_back(value);
    send_msg(fd, 0x05, p);
}

static void send_write(int fd, uint32_t viewer, uint16_t tag, const std::vector<uint8_t> &value)
{
    send_raw(fd, write_message(viewer, tag, value));
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

static uint16_t get_u16(const uint8_t *p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}

/** A decoded VALUES message. */
struct Values
{
    bool ok = false;
    uint16_t page = 0;
    uint32_t writes = 0;
    std::map<uint16_t, std::vector<uint8_t>> tags;
};

static Values parse_values(const Msg &m)
{
    Values v;
    if (m.type != 0x84 || m.payload.size() < 12)
        return v;
    const uint8_t *p = m.payload.data();
    v.page           = get_u16(p + 4);
    v.writes         = get_u32(p + 6);
    const uint16_t n = get_u16(p + 10);
    size_t off       = 12;
    for (uint16_t i = 0; i < n; i++)
    {
        if (m.payload.size() - off < 4)
            return v;
        const uint16_t tag = get_u16(p + off);
        const uint16_t len = get_u16(p + off + 2);
        if (m.payload.size() - off - 4 < len)
            return v;
        v.tags[tag] = std::vector<uint8_t>(p + off + 4, p + off + 4 + len);
        off += 4u + len;
    }
    v.ok = off == m.payload.size();
    return v;
}

/** The next VALUES of viewer that satisfies pred, within timeout_ms. */
template <typename Pred> static Values values_until(int fd, uint32_t viewer, Pred pred, int timeout_ms = 1500)
{
    const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < end)
    {
        Msg m = read_until(fd, 0x84, viewer, 300);
        if (m.type == 0)
            continue;
        Values v = parse_values(m);
        if (v.ok && pred(v))
            return v;
    }
    return Values{};
}

/** The next REFUSED of viewer, as (what, id); (-1, -1) when none comes. */
static std::pair<int, int> refused(int fd, uint32_t viewer, int timeout_ms = 1500)
{
    Msg m = read_until(fd, 0x85, viewer, timeout_ms);
    if (m.type != 0x85 || m.payload.size() != 7)
        return {-1, -1};
    return {m.payload[4], get_u16(m.payload.data() + 5)};
}

static size_t write_count()
{
    std::lock_guard<std::mutex> lk(g_mu);
    return g_writes.size();
}

static std::vector<uint8_t> f32(float f)
{
    std::vector<uint8_t> b(4);
    std::memcpy(b.data(), &f, 4);
    return b;
}

static const char *MD5 = "0123456789ABCDEF0123456789ABCDEF";

static int open_viewer(int fd, uint32_t viewer, uint8_t role, const char *name)
{
    send_msg(fd, 0x01, open_payload(viewer, role, name));
    return read_until(fd, 0x82, viewer).type == 0x82;
}

static void test_values_and_writes()
{
    std::printf("values and writes\n");
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_lamp  = 0;
        g_speed = 40;
        g_label = "pump";
        g_temp  = 21.5f;
        g_flags = 0;
        g_writes.clear();
        g_logs.clear();
    }
    plugin_runtime_args_t args = make_args(MD5);
    CHECK(init(&args) == 0, "init");
    CHECK(start_loop() == 0, "start_loop");
    struct stat st;
    CHECK(stat(HMI_SOCKET_DIR, &st) == 0 && (st.st_mode & 0777) == 0700, "socket folder is 0700");
    int fd = connect_host();
    CHECK(fd >= 0, "web server connects");
    Msg s = read_msg(fd);
    CHECK(s.type == 0x81 && s.payload.size() == 1 && s.payload[0] == 0, "STATUS live on connect");

    CHECK(open_viewer(fd, 1, 2, "alice"), "an operator's OPEN answered with OPENED");
    CHECK(logged("viewer 1 opened by alice (operator)"), "the open is logged with account and role");
    CHECK(read_msg(fd, 200).type == 0, "no values before a screen is shown");

    send_show(fd, 1, 0);
    Values v = values_until(fd, 1, [](const Values &x) { return x.tags.size() == 5; });
    CHECK(v.ok && v.page == 0 && v.writes == 0, "SHOW of a granted screen sends its snapshot");
    CHECK(v.tags[0] == std::vector<uint8_t>{0} && v.tags[1] == (std::vector<uint8_t>{40, 0}) &&
              v.tags[2] == (std::vector<uint8_t>{'p', 'u', 'm', 'p'}) && v.tags[3] == f32(21.5f),
          "every tag the screen reads, in wire format");

    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_temp = 30.0f;
    }
    v = values_until(fd, 1, [](const Values &x) { return x.tags.count(3) != 0; });
    CHECK(v.ok && v.tags.size() == 1 && v.tags[3] == f32(30.0f), "only the changed value follows");
    CHECK(read_until(fd, 0x84, 1, 200).type == 0, "nothing while no value changes");

    send_write(fd, 1, 0, {1});
    v = values_until(fd, 1, [](const Values &x) { return x.writes == 1; });
    CHECK(v.ok && v.tags[0] == std::vector<uint8_t>{1}, "a write is applied and its value sent back");
    CHECK(g_lamp == 1 && logged("write by alice: CONFIG0.RES0.INSTANCE0.MAIN.LAMP = TRUE"),
          "soft-written through the debugger and logged with the account");
    send_write(fd, 1, 2, {'v', 'a', 'l', 'v', 'e'});
    v = values_until(fd, 1, [](const Values &x) { return x.writes == 2; });
    CHECK(v.ok && g_label == "valve", "a STRING write");

    /* Bit assignments: applied to the PLC's current value, only the bits the screen sets. */
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_flags = 32; /* the PLC set bit 5 after the browser last saw flags */
    }
    send_bit(fd, 1, 4, 2, 1);
    v = values_until(fd, 1, [](const Values &x) { return x.writes == 3; });
    CHECK(v.ok && g_flags == 36, "a bit assignment keeps the bit the PLC set meanwhile (32 | 4)");
    CHECK(v.tags[4] == (std::vector<uint8_t>{36, 0, 0, 0}), "and the resulting value is sent back");
    CHECK(logged("write by alice: CONFIG0.RES0.INSTANCE0.MAIN.FLAGS.2 = TRUE (now 36)"),
          "the bit write is logged with the bit and the resulting value");
    send_bit(fd, 1, 4, 2, 0);
    v = values_until(fd, 1, [](const Values &x) { return x.writes == 4; });
    CHECK(v.ok && g_flags == 32, "clearing the bit leaves the PLC's bit 5");
    const size_t bits_applied = write_count();
    send_write(fd, 1, 4, {0xFF, 0xFF, 0xFF, 0xFF});
    CHECK(refused(fd, 1) == std::make_pair(1, 4), "a whole write of a tag the screen only sets bits of is refused");
    CHECK(logged("FLAGS refused: the screen shown only sets single bits of it"), "and logged");
    send_bit(fd, 1, 4, 3, 1);
    CHECK(refused(fd, 1) == std::make_pair(1, 4), "a bit the screen does not set is refused");
    send_bit(fd, 1, 1, 0, 1);
    CHECK(refused(fd, 1) == std::make_pair(1, 1), "a bit of a tag the screen writes only whole is refused");
    v = values_until(fd, 1, [](const Values &x) { return x.writes == 7; });
    CHECK(v.ok && write_count() == bits_applied && g_flags == 32, "no refused bit or forged word reached the PLC");

    /* Refused writes: none reaches the debugger, each is answered and counted. */
    const size_t applied = write_count();
    send_write(fd, 1, 3, f32(99.0f));
    CHECK(refused(fd, 1) == std::make_pair(1, 3), "a tag the screen only reads is refused");
    CHECK(logged("of CONFIG0.RES0.INSTANCE0.MAIN.TEMP refused: the screen shown does not write it"),
          "and logged with the reason");
    send_write(fd, 1, 0, {2});
    CHECK(refused(fd, 1) == std::make_pair(1, 0), "a BOOL other than 0 or 1 is refused");
    send_write(fd, 1, 1, {1});
    CHECK(refused(fd, 1) == std::make_pair(1, 1), "an INT of one byte is refused");
    send_write(fd, 1, 2, std::vector<uint8_t>(21, 'x'));
    CHECK(refused(fd, 1) == std::make_pair(1, 2), "a STRING past its length is refused");
    send_write(fd, 1, 2, {'a', 0, 'b'});
    CHECK(refused(fd, 1) == std::make_pair(1, 2), "a STRING holding a NUL is refused");
    send_write(fd, 1, 99, {1});
    CHECK(refused(fd, 1) == std::make_pair(1, 99), "an unknown tag is refused");
    send_write(fd, 1, 1, {0x30, 0x75});
    CHECK(refused(fd, 1) == std::make_pair(1, 1), "speed 30000 is refused: the screen's slider stops at 100");
    CHECK(logged("SPEED refused: the value is outside what the screen shown can set"), "and logged");
    send_write(fd, 1, 1, {101, 0});
    CHECK(refused(fd, 1) == std::make_pair(1, 1), "speed 101 is refused");
    send_write(fd, 1, 1, {0xFF, 0xFF});
    CHECK(refused(fd, 1) == std::make_pair(1, 1), "speed -1 is refused");
    send_write(fd, 1, 2, std::vector<uint8_t>(11, 'x'));
    CHECK(refused(fd, 1) == std::make_pair(1, 2), "a label of 11 bytes is refused: at most 10");
    v = values_until(fd, 1, [](const Values &x) { return x.writes == 17; });
    CHECK(v.ok, "refused writes are counted too");
    CHECK(write_count() == applied && g_temp == 30.0f && g_lamp == 1 && g_speed == 40,
          "no refused write reached the PLC");
    send_write(fd, 1, 1, {100, 0});
    v = values_until(fd, 1, [](const Values &x) { return x.writes == 18; });
    CHECK(v.ok && g_speed == 100, "speed 100, the slider's maximum, is applied");
    send_write(fd, 1, 2, std::vector<uint8_t>(10, 'y'));
    v = values_until(fd, 1, [](const Values &x) { return x.writes == 19; });
    CHECK(v.ok && g_label == std::string(10, 'y'), "a label of 10 bytes is applied");

    /* From one granted screen to another: the old screen's tags are no longer sent. */
    send_show(fd, 1, 1);
    v = values_until(fd, 1, [](const Values &x) { return x.page == 1; });
    CHECK(v.ok && v.tags.size() == 1 && v.tags.count(3) == 1, "the Status snapshot holds temp only");
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_lamp  = 0;
        g_speed = 55;
        g_label = "moved";
        g_flags = 1;
    }
    bool leaked    = false;
    const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(300);
    while (std::chrono::steady_clock::now() < end)
    {
        Msg m = read_until(fd, 0x84, 1, 50);
        Values x = parse_values(m);
        for (uint16_t tag : {0, 1, 2, 4})
            leaked |= x.ok && x.tags.count(tag) != 0;
    }
    CHECK(!leaked, "lamp, speed, label and flags changed on the PLC are not sent once Status shows");
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_temp = 31.0f;
    }
    v = values_until(fd, 1, [](const Values &x) { return x.tags.count(3) != 0; });
    CHECK(v.ok && v.tags.size() == 1 && v.tags[3] == f32(31.0f), "temp, which Status reads, still is");
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_lamp  = 1;
        g_speed = 100;
        g_label = std::string(10, 'y');
        g_flags = 32;
        g_temp  = 30.0f;
    }

    /* A screen the role may not open: refused, and nothing is served or written afterwards. */
    send_show(fd, 1, 2);
    CHECK(refused(fd, 1) == std::make_pair(0, 2), "an operator's SHOW of an admin screen is refused");
    CHECK(logged("screen 2 refused for alice (operator)"), "the refused SHOW is logged");
    v = values_until(fd, 1, [](const Values &x) { return x.page == 0xFFFF; });
    CHECK(v.ok && v.tags.empty(), "the viewer is served no tag");
    send_write(fd, 1, 1, {7, 0});
    CHECK(refused(fd, 1) == std::make_pair(1, 1), "and its writes are refused");
    CHECK(g_speed == 100, "speed unchanged");
    send_show(fd, 1, 9);
    CHECK(refused(fd, 1) == std::make_pair(0, 9), "a screen past the last is refused");

    /* Negative control: the same requests from an admin pass. */
    CHECK(open_viewer(fd, 2, 0, "root"), "an admin opens");
    send_show(fd, 2, 2);
    v = values_until(fd, 2, [](const Values &x) { return x.page == 2; });
    CHECK(v.ok && v.tags.size() == 1 && v.tags.count(1) == 1, "the admin screen serves speed only");
    send_write(fd, 2, 1, {7, 0});
    v = values_until(fd, 2, [](const Values &x) { return x.writes == 1; });
    CHECK(v.ok && g_speed == 7 && logged("write by root: CONFIG0.RES0.INSTANCE0.MAIN.SPEED = 7"),
          "the admin's write of speed is applied");

    /* A viewer role: the status screen reads temp and writes nothing. */
    CHECK(open_viewer(fd, 3, 3, "eve"), "a viewer opens");
    send_show(fd, 3, 0);
    CHECK(refused(fd, 3) == std::make_pair(0, 0), "a viewer may not open the control screen");
    send_show(fd, 3, 1);
    v = values_until(fd, 3, [](const Values &x) { return x.page == 1; });
    CHECK(v.ok && v.tags.size() == 1 && v.tags[3] == f32(30.0f), "the status screen serves temp");
    send_write(fd, 3, 0, {0});
    CHECK(refused(fd, 3) == std::make_pair(1, 0), "a write from the read-only screen is refused");
    CHECK(g_lamp == 1, "lamp unchanged");

    /* CLOSE frees the viewer; requests for it are ignored. */
    std::vector<uint8_t> close_p;
    put_u32(close_p, 3);
    send_msg(fd, 0x02, close_p);
    CHECK(wait_for([] { return logged("viewer 3 closed"); }, 1000), "CLOSE is logged");
    send_write(fd, 3, 0, {0});
    CHECK(read_until(fd, 0x85, 3, 300).type == 0, "a closed viewer gets nothing");

    /* More requests than the queue holds: the viewer is dropped with BUSY, others keep going. */
    std::vector<uint8_t> burst;
    for (int i = 0; i < 400; i++)
    {
        const std::vector<uint8_t> m = write_message(1, 3, f32(1.0f));
        burst.insert(burst.end(), m.begin(), m.end());
    }
    send_raw(fd, burst);
    CHECK(read_until(fd, 0x83, 1, 2000).type == 0x83, "a flooding viewer is dropped with BUSY");
    CHECK(logged("viewer 1 dropped: its request queue overflowed"), "and logged");
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_speed = 8;
    }
    v = values_until(fd, 2, [](const Values &x) { return x.tags.count(1) != 0; });
    CHECK(v.ok && v.tags[1] == (std::vector<uint8_t>{8, 0}), "the other viewer is still served");

    /* A second OPEN of an open viewer, and an unknown message, break the protocol. */
    send_msg(fd, 0x01, open_payload(2, 0, "root"));
    CHECK(closed_by_host(fd), "a repeated OPEN closes the connection");
    CHECK(wait_for([] { return logged("viewer(s) dropped"); }, 1000), "the connection close is logged");
    close(fd);

    fd = connect_host();
    read_msg(fd);
    send_msg(fd, 0x09, {0, 0, 0, 0});
    CHECK(closed_by_host(fd), "an unknown message type closes the connection");
    close(fd);

    stop_loop();
    cleanup();
    CHECK(access(HMI_SOCKET_PATH, F_OK) != 0, "the socket is removed on stop");
}

static void test_out_of_date()
{
    std::printf("out of date\n");
    for (const char *md5 : {"ffffffffffffffffffffffffffffffff", ""})
    {
        plugin_runtime_args_t args = make_args(md5);
        CHECK(init(&args) == 0, "init");
        CHECK(start_loop() == 0, "start_loop");
        const int reads      = g_reads.load();
        const size_t writes  = write_count();
        int fd = connect_host();
        Msg s  = read_msg(fd);
        CHECK(s.type == 0x81 && s.payload[0] == 1, "STATUS out_of_date on connect");
        send_msg(fd, 0x01, open_payload(1, 0, "alice"));
        Msg r = read_msg(fd);
        CHECK(r.type == 0x81 && r.payload[0] == 1, "OPEN answered with STATUS out_of_date");
        send_show(fd, 1, 0);
        send_write(fd, 1, 0, {1});
        CHECK(read_msg(fd, 300).type == 0, "no OPENED, no VALUES");
        CHECK(g_reads.load() == reads && write_count() == writes, "no debug read or write");
        close(fd);
        stop_loop();
        cleanup();
    }
}

int main()
{
    unlink(HMI_SOCKET_PATH);
    test_values_and_writes();
    test_out_of_date();
    if (g_failures == 0)
        std::printf("all cases passed\n");
    else
        std::printf("%d case(s) failed\n", g_failures);
    return g_failures == 0 ? 0 : 1;
}
