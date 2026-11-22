// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

// Host test of the HMI plugin's pure parts: wire protocol, per-viewer values, buffer <-> debug
// and wire conversion with the checks on viewers' values, and the MD5 gate.

#include "drivers/plugins/native/hmi/hmi_codec.h"
#include "drivers/plugins/native/hmi/hmi_values.h"
#include "drivers/plugins/native/hmi/hmi_wire.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <string>
#include <vector>

static int g_failures = 0;

#define CHECK(cond, what)                                                                          \
    do                                                                                             \
    {                                                                                              \
        if (!(cond))                                                                               \
        {                                                                                          \
            std::printf("  FAIL  %s (line %d)\n", what, __LINE__);                                \
            g_failures++;                                                                          \
        }                                                                                          \
    } while (0)

using Bytes = std::vector<uint8_t>;

static void put_u16(Bytes &b, uint16_t v)
{
    b.push_back((uint8_t)v);
    b.push_back((uint8_t)(v >> 8));
}

static void put_u32(Bytes &b, uint32_t v)
{
    for (int i = 0; i < 4; i++)
        b.push_back((uint8_t)(v >> (8 * i)));
}

static uint16_t get_u16(const uint8_t *p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}

static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static Bytes message(uint8_t type, const Bytes &payload)
{
    Bytes b;
    put_u32(b, (uint32_t)(payload.size() + 1));
    b.push_back(type);
    b.insert(b.end(), payload.begin(), payload.end());
    return b;
}

static Bytes open_payload(uint32_t viewer, uint8_t role, const std::string &name)
{
    Bytes p;
    put_u32(p, viewer);
    p.push_back(role);
    p.push_back((uint8_t)name.size());
    p.insert(p.end(), name.begin(), name.end());
    return p;
}

static Bytes write_payload(uint32_t viewer, uint16_t tag, const Bytes &value)
{
    Bytes p;
    put_u32(p, viewer);
    put_u16(p, tag);
    put_u16(p, (uint16_t)value.size());
    p.insert(p.end(), value.begin(), value.end());
    return p;
}

static hmi_wire_result_t decode(const Bytes &b, hmi_wire_msg_t *m, size_t *used)
{
    return hmi_wire_decode(b.data(), b.size(), m, used);
}

/* ---- wire ---------------------------------------------------------------- */

static void test_wire_decode()
{
    std::printf("wire: decode\n");
    hmi_wire_msg_t m;
    size_t used = 0;

    Bytes open = message(0x01, open_payload(7, 2, "alice"));
    CHECK(decode(open, &m, &used) == HMI_WIRE_DECODED && used == open.size(), "OPEN decoded");
    CHECK(m.type == HMI_MSG_OPEN && m.viewer == 7 && m.role == 2 && m.name_valid &&
              std::strcmp(m.name, "alice") == 0,
          "OPEN fields");

    Bytes bad_name = message(0x01, open_payload(8, 0, std::string("a\x01b")));
    CHECK(decode(bad_name, &m, &used) == HMI_WIRE_DECODED && !m.name_valid && m.name[0] == 0,
          "a control character makes the name invalid, not the message");

    Bytes close;
    put_u32(close, 7);
    CHECK(decode(message(0x02, close), &m, &used) == HMI_WIRE_DECODED && m.type == HMI_MSG_CLOSE &&
              m.viewer == 7,
          "CLOSE");

    Bytes show;
    put_u32(show, 7);
    put_u16(show, 3);
    CHECK(decode(message(0x03, show), &m, &used) == HMI_WIRE_DECODED && m.type == HMI_MSG_SHOW &&
              m.viewer == 7 && m.page == 3,
          "SHOW");

    Bytes write = message(0x04, write_payload(7, 2, {0x34, 0x12}));
    CHECK(decode(write, &m, &used) == HMI_WIRE_DECODED && m.type == HMI_MSG_WRITE && m.tag == 2 &&
              m.value_len == 2 && m.value[0] == 0x34 && m.value[1] == 0x12,
          "WRITE");
    Bytes empty = message(0x04, write_payload(7, 9, {}));
    CHECK(decode(empty, &m, &used) == HMI_WIRE_DECODED && m.value_len == 0,
          "a WRITE of an empty STRING");

    Bytes bit;
    put_u32(bit, 7);
    put_u16(bit, 4);
    bit.push_back(63);
    bit.push_back(1);
    CHECK(decode(message(0x05, bit), &m, &used) == HMI_WIRE_DECODED && m.type == HMI_MSG_BIT &&
              m.tag == 4 && m.bit == 63 && m.bit_value == 1,
          "BIT");

    Bytes two = open;
    two.insert(two.end(), write.begin(), write.end());
    CHECK(decode(two, &m, &used) == HMI_WIRE_DECODED && used == open.size(),
          "only the first message is taken");
    Bytes part(write.begin(), write.end() - 1);
    CHECK(decode(part, &m, &used) == HMI_WIRE_NEED_MORE, "a partial message needs more");
}

static void test_wire_malformed()
{
    std::printf("wire: malformed\n");
    hmi_wire_msg_t m;
    size_t used = 0;

    Bytes unknown = message(0x06, {0, 0, 0, 0});
    CHECK(decode(unknown, &m, &used) == HMI_WIRE_MALFORMED, "unknown type");
    Bytes old_frame = message(0x81, {0, 0, 0, 0});
    CHECK(decode(old_frame, &m, &used) == HMI_WIRE_MALFORMED, "a host message type");
    Bytes head = {6, 0, 0, 0, 0x06};
    CHECK(decode(head, &m, &used) == HMI_WIRE_MALFORMED, "an unknown type fails before its payload");

    Bytes zero = {0, 0, 0, 0};
    CHECK(decode(zero, &m, &used) == HMI_WIRE_MALFORMED, "zero length");
    Bytes huge;
    put_u32(huge, HMI_WIRE_MAX_INBOUND);
    huge.push_back(0x04);
    CHECK(decode(huge, &m, &used) == HMI_WIRE_MALFORMED, "longer than any web server message");

    CHECK(decode(message(0x01, open_payload(1, 4, "x")), &m, &used) == HMI_WIRE_MALFORMED,
          "role past viewer");
    Bytes long_name = open_payload(1, 0, std::string(65, 'n'));
    CHECK(decode(message(0x01, long_name), &m, &used) == HMI_WIRE_MALFORMED, "name over 64 bytes");
    Bytes short_open = open_payload(1, 0, "abc");
    short_open.pop_back();
    CHECK(decode(message(0x01, short_open), &m, &used) == HMI_WIRE_MALFORMED,
          "name length past the payload");

    Bytes show;
    put_u32(show, 1);
    put_u16(show, 0);
    show.push_back(0);
    CHECK(decode(message(0x03, show), &m, &used) == HMI_WIRE_MALFORMED, "SHOW with extra bytes");

    for (const Bytes &bad : {Bytes{1, 0, 0, 0, 4, 0, 64, 1}, Bytes{1, 0, 0, 0, 4, 0, 2, 2},
                             Bytes{1, 0, 0, 0, 4, 0, 2}})
        CHECK(decode(message(0x05, bad), &m, &used) == HMI_WIRE_MALFORMED,
              "BIT past bit 63, with a value other than 0 or 1, or cut short");

    Bytes lying = write_payload(1, 0, {1, 2, 3});
    lying[6] = 4; /* says 4 value bytes, carries 3 */
    CHECK(decode(message(0x04, lying), &m, &used) == HMI_WIRE_MALFORMED,
          "WRITE whose length does not match its value");
    Bytes big = write_payload(1, 0, Bytes(HMI_WIRE_VALUE_MAX + 1, 'x'));
    CHECK(decode(message(0x04, big), &m, &used) == HMI_WIRE_MALFORMED, "WRITE value over the limit");
}

static void test_wire_encode()
{
    std::printf("wire: encode\n");
    uint8_t out[32];
    CHECK(hmi_wire_encode_viewer(HMI_MSG_OPENED, 0x01020304, out) == 9 && get_u32(out) == 5 &&
              out[4] == 0x82 && get_u32(out + 5) == 0x01020304,
          "OPENED");
    CHECK(hmi_wire_encode_status(HMI_STATE_OUT_OF_DATE, out) == 6 && out[4] == 0x81 && out[5] == 1,
          "STATUS");
    CHECK(hmi_wire_encode_refused(9, HMI_REFUSED_WRITE, 0x0203, out) == 12 && get_u32(out) == 8 &&
              out[4] == 0x85 && get_u32(out + 5) == 9 && out[9] == 1 && get_u16(out + 10) == 0x0203,
          "REFUSED");
    CHECK(hmi_wire_values_header(5, 2, 7, 3, 20, out) && get_u32(out) == 33 && out[4] == 0x84 &&
              get_u32(out + 5) == 5 && get_u16(out + 9) == 2 && get_u32(out + 11) == 7 &&
              get_u16(out + 15) == 3,
          "VALUES header");
    CHECK(!hmi_wire_values_header(5, 2, 7, 3, HMI_WIRE_MAX_MESSAGE, out),
          "VALUES past the message limit refused");
}

/* ---- values -------------------------------------------------------------- */

/** The records of a VALUES message, as (tag, bytes). */
static std::vector<std::pair<uint16_t, Bytes>> records(const hmi_buf_t &msg, uint32_t *writes)
{
    std::vector<std::pair<uint16_t, Bytes>> out;
    if (msg.len < HMI_WIRE_VALUES_HEADER)
        return out;
    *writes            = get_u32(msg.data + 11);
    const uint16_t n   = get_u16(msg.data + 15);
    size_t off         = HMI_WIRE_VALUES_HEADER;
    for (uint16_t i = 0; i < n; i++)
    {
        const uint16_t tag = get_u16(msg.data + off);
        const uint16_t len = get_u16(msg.data + off + 2);
        out.emplace_back(tag, Bytes(msg.data + off + 4, msg.data + off + 4 + len));
        off += 4u + len;
    }
    return out;
}

static void test_values()
{
    std::printf("values: changes, snapshots and takes\n");
    const uint16_t max[3] = {1, 2, 20};
    hmi_values_layout_t layout;
    CHECK(hmi_values_layout_init(&layout, max, 3) && layout.total == 23, "layout");
    hmi_values_t v;
    CHECK(hmi_values_init(&v, &layout), "init");

    const uint8_t on = 1, off = 0;
    const uint8_t speed[2] = {5, 0};
    CHECK(hmi_values_offer(&v, &layout, 0, &on, 1), "a first value is a change");
    CHECK(hmi_values_offer(&v, &layout, 1, speed, 2), "a second tag too");
    CHECK(!hmi_values_offer(&v, &layout, 0, &on, 1), "the same value again is not");
    const uint8_t text[25] = {};
    CHECK(!hmi_values_offer(&v, &layout, 2, text, 25), "longer than the tag's most is refused");
    CHECK(!hmi_values_offer(&v, &layout, 3, &on, 1), "an unknown tag is refused");
    CHECK(hmi_values_encode(&v, &layout, 4, 0, 0), "encoded");

    hmi_buf_t out = {};
    uint32_t writes = 99;
    CHECK(hmi_values_take(&v, &layout, &out), "taken");
    auto r = records(out, &writes);
    CHECK(r.size() == 2 && r[0].first == 0 && r[0].second == Bytes{1} && r[1].first == 1 &&
              r[1].second == (Bytes{5, 0}) && writes == 0,
          "both changes in tag order");
    CHECK(!hmi_values_take(&v, &layout, &out), "nothing pending after a take");

    CHECK(hmi_values_offer(&v, &layout, 0, &off, 1), "a change after the take");
    CHECK(hmi_values_encode(&v, &layout, 4, 0, 1), "encoded");
    const uint8_t speed2[2] = {6, 0};
    CHECK(hmi_values_offer(&v, &layout, 1, speed2, 2), "another change before the writer took it");
    CHECK(hmi_values_encode(&v, &layout, 4, 0, 2), "the pending message rebuilt");
    CHECK(hmi_values_take(&v, &layout, &out), "taken");
    r = records(out, &writes);
    CHECK(r.size() == 2 && r[0].second == Bytes{0} && r[1].second == (Bytes{6, 0}) && writes == 2,
          "a message not yet taken keeps every change since the last take");

    hmi_values_forget(&v, &layout, 1);
    CHECK(hmi_values_offer(&v, &layout, 1, speed2, 2), "a forgotten tag is sent again unchanged");
    CHECK(hmi_values_encode(&v, &layout, 4, 0, 3), "encoded");
    hmi_values_forget_all(&v, &layout);
    CHECK(!hmi_values_take(&v, &layout, &out), "forgetting all drops the pending message");
    CHECK(hmi_values_offer(&v, &layout, 0, &off, 1) && hmi_values_offer(&v, &layout, 2, text, 0),
          "after forgetting all, every value is sent again, an empty STRING too");
    CHECK(hmi_values_encode(&v, &layout, 4, 1, 3) && hmi_values_take(&v, &layout, &out), "taken");
    r = records(out, &writes);
    CHECK(r.size() == 2 && r[0].first == 0 && r[1].first == 2 && r[1].second.empty() &&
              get_u16(out.data + 9) == 1,
          "the snapshot of the new page");

    hmi_buf_free(&out);
    hmi_values_free(&v);
    hmi_values_layout_free(&layout);
}

/* ---- codec --------------------------------------------------------------- */

static void test_codec_numbers()
{
    std::printf("codec: numbers\n");
    uint8_t buf[8];
    uint8_t wire[8];

    const uint8_t b1 = 7;
    CHECK(hmi_codec_from_debug(HMI_CODEC_BOOL, 0, &b1, 1, buf) && buf[0] == 1, "BOOL normalised");
    const uint8_t two = 2;
    CHECK(hmi_codec_to_debug(HMI_CODEC_BOOL, 0, &two, wire, 1, nullptr) && wire[0] == 1,
          "BOOL written as 1");

    const int16_t i = -1234;
    CHECK(hmi_codec_from_debug(HMI_CODEC_INT, 0, (const uint8_t *)&i, 2, buf), "INT read");
    CHECK(buf[0] == 0x2E && buf[1] == 0xFB, "INT little endian");
    CHECK(hmi_codec_to_debug(HMI_CODEC_INT, 0, buf, wire, 2, nullptr) &&
              std::memcmp(wire, &i, 2) == 0,
          "INT round trip");
    CHECK(!hmi_codec_from_debug(HMI_CODEC_INT, 0, (const uint8_t *)&i, 4, buf),
          "a debug size that does not match the type is refused");
    CHECK(!hmi_codec_to_debug(HMI_CODEC_DINT, 0, buf, wire, 2, nullptr), "write size checked too");

    const double d = -0.125;
    CHECK(hmi_codec_from_debug(HMI_CODEC_LREAL, 0, (const uint8_t *)&d, 8, buf) &&
              hmi_codec_to_debug(HMI_CODEC_LREAL, 0, buf, wire, 8, nullptr) &&
              std::memcmp(wire, &d, 8) == 0,
          "LREAL round trip");
    CHECK(!hmi_codec_from_debug(99, 0, &b1, 1, buf), "unknown type refused");

    const int64_t dt = 1795012327250000000LL;
    CHECK(hmi_codec_from_debug(HMI_CODEC_DT, 0, (const uint8_t *)&dt, 8, buf) &&
              hmi_codec_to_debug(HMI_CODEC_DT, 0, buf, wire, 8, nullptr) && std::memcmp(wire, &dt, 8) == 0,
          "DT round trip");

    struct Width
    {
        uint8_t type;
        size_t width;
    };
    const Width widths[] = {{HMI_CODEC_BOOL, 1},  {HMI_CODEC_SINT, 1},  {HMI_CODEC_INT, 2},
                            {HMI_CODEC_DINT, 4},  {HMI_CODEC_LINT, 8},  {HMI_CODEC_USINT, 1},
                            {HMI_CODEC_UINT, 2},  {HMI_CODEC_UDINT, 4}, {HMI_CODEC_ULINT, 8},
                            {HMI_CODEC_BYTE, 1},  {HMI_CODEC_WORD, 2},  {HMI_CODEC_DWORD, 4},
                            {HMI_CODEC_LWORD, 8}, {HMI_CODEC_REAL, 4},  {HMI_CODEC_LREAL, 8},
                            {HMI_CODEC_TIME, 8},  {HMI_CODEC_LTIME, 8}, {HMI_CODEC_DATE, 8},
                            {HMI_CODEC_TOD, 8},   {HMI_CODEC_DT, 8}};
    bool all = true;
    for (const Width &w : widths)
        all &= hmi_codec_width(w.type) == w.width && hmi_codec_buffer_size(w.type, 9) == w.width;
    CHECK(all, "IEC widths");
    CHECK(hmi_codec_buffer_size(HMI_CODEC_STRING, 80) == 81, "STRING buffer is max_len + 1");
}

static void test_codec_strings()
{
    std::printf("codec: STRING\n");
    uint8_t wire[127];
    char buf[200];

    std::memset(wire, 0xEE, sizeof wire);
    wire[0] = 5;
    std::memcpy(wire + 1, "hello", 5);
    CHECK(hmi_codec_from_debug(HMI_CODEC_STRING, 20, wire, sizeof wire, buf) &&
              std::strcmp(buf, "hello") == 0,
          "debug STRING to NUL-terminated text");
    CHECK(hmi_codec_from_debug(HMI_CODEC_STRING, 3, wire, sizeof wire, buf) &&
              std::strcmp(buf, "hel") == 0,
          "cut to the tag's max_len");
    wire[0] = 200;
    CHECK(hmi_codec_from_debug(HMI_CODEC_STRING, 190, wire, sizeof wire, buf) &&
              std::strlen(buf) == 126,
          "a length byte past the window is clamped to it");

    char text[30] = "abc";
    bool cut      = true;
    CHECK(hmi_codec_to_debug(HMI_CODEC_STRING, 29, text, wire, sizeof wire, &cut) && !cut &&
              wire[0] == 3 && std::memcmp(wire + 1, "abc", 3) == 0 && wire[4] == 0 &&
              wire[126] == 0,
          "text to the debug format, zero filled");
    char unterminated[4] = {'w', 'x', 'y', 'z'};
    CHECK(hmi_codec_to_debug(HMI_CODEC_STRING, 4, unterminated, wire, sizeof wire, &cut) &&
              wire[0] == 4,
          "never reads past max_len");
    std::string longer(200, 'q');
    CHECK(hmi_codec_to_debug(HMI_CODEC_STRING, 200, longer.c_str(), wire, sizeof wire, &cut) &&
              cut && wire[0] == 126,
          "a STRING longer than the debug window is cut and reported");
}

static void test_codec_format()
{
    std::printf("codec: write log values\n");
    char out[64];
    const uint8_t t = 1;
    hmi_codec_format(HMI_CODEC_BOOL, 0, &t, out, sizeof out);
    CHECK(std::strcmp(out, "TRUE") == 0, "BOOL");
    const uint8_t sint[1] = {0xFF};
    hmi_codec_format(HMI_CODEC_SINT, 0, sint, out, sizeof out);
    CHECK(std::strcmp(out, "-1") == 0, "SINT sign");
    const uint8_t udint[4] = {0xFF, 0xFF, 0xFF, 0xFF};
    hmi_codec_format(HMI_CODEC_UDINT, 0, udint, out, sizeof out);
    CHECK(std::strcmp(out, "4294967295") == 0, "UDINT");
    const uint8_t word[2] = {0x34, 0x12};
    hmi_codec_format(HMI_CODEC_WORD, 0, word, out, sizeof out);
    CHECK(std::strcmp(out, "16#1234") == 0, "WORD in hex");
    const float f = 1.5f;
    hmi_codec_format(HMI_CODEC_REAL, 0, &f, out, sizeof out);
    CHECK(std::strcmp(out, "1.5") == 0, "REAL");
    const char s[] = "a'b\\c\x01";
    hmi_codec_format(HMI_CODEC_STRING, 10, s, out, sizeof out);
    CHECK(std::strcmp(out, "'a\\'b\\\\c\\x01'") == 0, "STRING quoted and escaped");
    const int64_t time_ns = 1500000000LL;
    hmi_codec_format(HMI_CODEC_TIME, 0, &time_ns, out, sizeof out);
    CHECK(std::strcmp(out, "T#1500000000ns") == 0, "TIME in nanoseconds");
    hmi_codec_format(HMI_CODEC_LTIME, 0, &time_ns, out, sizeof out);
    CHECK(std::strcmp(out, "LT#1500000000ns") == 0, "LTIME in nanoseconds");
    const int64_t days = 20775;
    hmi_codec_format(HMI_CODEC_DATE, 0, &days, out, sizeof out);
    CHECK(std::strcmp(out, "D#2026-11-18") == 0, "DATE from days");
    const int64_t tod = 52327250000000LL;
    hmi_codec_format(HMI_CODEC_TOD, 0, &tod, out, sizeof out);
    CHECK(std::strcmp(out, "TOD#14:32:07.250") == 0, "TOD from nanoseconds");
    const int64_t dt = 1795012327250000000LL;
    hmi_codec_format(HMI_CODEC_DT, 0, &dt, out, sizeof out);
    CHECK(std::strcmp(out, "DT#2026-11-18-14:32:07.250") == 0, "DT from nanoseconds");
    const int64_t before_epoch = -1000000000LL;
    hmi_codec_format(HMI_CODEC_DT, 0, &before_epoch, out, sizeof out);
    CHECK(std::strcmp(out, "DT#1969-12-31-23:59:59.000") == 0, "DT before 1970");
    char small[8];
    const std::string big(50, 'z');
    hmi_codec_format(HMI_CODEC_STRING, 50, big.c_str(), small, sizeof small);
    CHECK(std::strlen(small) < sizeof small && small[0] == '\'', "bounded by the output size");
}


static void test_codec_wire()
{
    std::printf("codec: viewers' values\n");
    uint8_t buf[32];
    uint8_t out[32];

    const uint8_t t = 1, two = 2;
    CHECK(hmi_codec_from_wire(HMI_CODEC_BOOL, 0, &t, 1, buf) && buf[0] == 1, "BOOL 1");
    CHECK(!hmi_codec_from_wire(HMI_CODEC_BOOL, 0, &two, 1, buf), "BOOL 2 refused");
    const uint8_t i16[2] = {0x2E, 0xFB};
    CHECK(hmi_codec_from_wire(HMI_CODEC_INT, 0, i16, 2, buf) && std::memcmp(buf, i16, 2) == 0,
          "INT taken as it is");
    CHECK(!hmi_codec_from_wire(HMI_CODEC_INT, 0, i16, 1, buf), "INT with one byte refused");
    CHECK(!hmi_codec_from_wire(HMI_CODEC_DINT, 0, i16, 2, buf), "DINT with two bytes refused");
    const float nan = std::numeric_limits<float>::quiet_NaN();
    const float inf = std::numeric_limits<float>::infinity();
    const float ok  = 2.5f;
    CHECK(!hmi_codec_from_wire(HMI_CODEC_REAL, 0, (const uint8_t *)&nan, 4, buf), "REAL NaN refused");
    CHECK(!hmi_codec_from_wire(HMI_CODEC_REAL, 0, (const uint8_t *)&inf, 4, buf),
          "REAL infinity refused");
    CHECK(hmi_codec_from_wire(HMI_CODEC_REAL, 0, (const uint8_t *)&ok, 4, buf), "REAL taken");
    const double dinf = -std::numeric_limits<double>::infinity();
    CHECK(!hmi_codec_from_wire(HMI_CODEC_LREAL, 0, (const uint8_t *)&dinf, 8, buf),
          "LREAL infinity refused");
    const int64_t day = 86400000000000LL, last = 86399999999999LL, neg = -1;
    CHECK(!hmi_codec_from_wire(HMI_CODEC_TOD, 0, (const uint8_t *)&day, 8, buf), "TOD of 24 h refused");
    CHECK(!hmi_codec_from_wire(HMI_CODEC_TOD, 0, (const uint8_t *)&neg, 8, buf), "negative TOD refused");
    CHECK(hmi_codec_from_wire(HMI_CODEC_TOD, 0, (const uint8_t *)&last, 8, buf), "TOD within a day");
    CHECK(!hmi_codec_from_wire(99, 0, &t, 1, buf), "unknown type refused");

    CHECK(hmi_codec_from_wire(HMI_CODEC_STRING, 5, (const uint8_t *)"hello", 5, buf) &&
              std::strcmp((const char *)buf, "hello") == 0,
          "STRING up to its length, NUL terminated");
    CHECK(!hmi_codec_from_wire(HMI_CODEC_STRING, 4, (const uint8_t *)"hello", 5, buf),
          "STRING past its length refused");
    CHECK(!hmi_codec_from_wire(HMI_CODEC_STRING, 9, (const uint8_t *)"he\0lo", 5, buf),
          "STRING holding a NUL refused");
    CHECK(hmi_codec_from_wire(HMI_CODEC_STRING, 9, nullptr, 0, buf) && buf[0] == 0, "empty STRING");

    std::memcpy(buf, "abc\0zzz", 8);
    CHECK(hmi_codec_to_wire(HMI_CODEC_STRING, 7, buf, out) == 3 && std::memcmp(out, "abc", 3) == 0,
          "STRING to wire without its NUL");
    std::memcpy(buf, "abcdefgh", 8);
    CHECK(hmi_codec_to_wire(HMI_CODEC_STRING, 4, buf, out) == 4, "never past max_len");
    CHECK(hmi_codec_to_wire(HMI_CODEC_LREAL, 0, buf, out) == 8, "numbers keep their width");
}

static void test_codec_within()
{
    std::printf("codec: write limits\n");
    const int16_t i = 30000, top = 100, neg = -1;
    CHECK(!hmi_codec_within(HMI_CODEC_INT, 0, &i, 0.0, 100.0), "INT 30000 outside 0..100");
    CHECK(hmi_codec_within(HMI_CODEC_INT, 0, &top, 0.0, 100.0), "INT 100 inside 0..100");
    CHECK(!hmi_codec_within(HMI_CODEC_INT, 0, &neg, 0.0, 100.0), "INT -1 outside 0..100");
    const uint16_t big = 65535;
    CHECK(hmi_codec_within(HMI_CODEC_UINT, 0, &big, 0.0, 65535.0), "UINT read unsigned");
    const float tenth = 0.1f;
    CHECK(hmi_codec_within(HMI_CODEC_REAL, 0, &tenth, 0.0, (double)0.1f), "REAL at a float bound");
    CHECK(!hmi_codec_within(HMI_CODEC_REAL, 0, &tenth, 0.0, 0.1), "REAL 0.1f past the double 0.1");
    const double d = -2.5;
    CHECK(hmi_codec_within(HMI_CODEC_LREAL, 0, &d, -2.5, 0.0), "LREAL at its minimum");
    CHECK(hmi_codec_within(HMI_CODEC_STRING, 20, "hello", 0.0, 5.0), "STRING of 5 within 5");
    CHECK(!hmi_codec_within(HMI_CODEC_STRING, 20, "hello!", 0.0, 5.0), "STRING of 6 past 5");
    CHECK(!hmi_codec_within(99, 0, &i, -1e9, 1e9), "unknown type");
}

static void test_md5_gate()
{
    std::printf("MD5 gate\n");
    const char *a = "0123456789abcdef0123456789abcdef";
    CHECK(hmi_md5_live(a, a), "equal MD5s are live");
    CHECK(hmi_md5_live(a, "0123456789ABCDEF0123456789ABCDEF"), "hex case ignored");
    CHECK(!hmi_md5_live(a, "0123456789abcdef0123456789abcdee"), "a mismatch is out of date");
    CHECK(!hmi_md5_live(a, "0123456789abcdef"), "a prefix is out of date");
    CHECK(!hmi_md5_live("", a), "no program loaded is out of date");
    CHECK(!hmi_md5_live(a, ""), "screens built without a program are out of date");
    CHECK(!hmi_md5_live("", ""), "both empty is out of date");
    CHECK(!hmi_md5_live(nullptr, a), "NULL is out of date");
}

/* Formats v, given as a buffer-format (little-endian) 8-byte value, and compares with expected. */
static bool formats_as(uint8_t type, int64_t v, const char *expected)
{
    uint64_t u;
    std::memcpy(&u, &v, sizeof u);
    uint8_t buf[8];
    for (int i = 0; i < 8; i++)
        buf[i] = (uint8_t)(u >> (8 * i));
    char out[160];
    hmi_codec_format(type, 0, buf, out, sizeof out);
    if (std::strcmp(out, expected) != 0)
    {
        std::printf("  got '%s', expected '%s'\n", out, expected);
        return false;
    }
    return true;
}

static void test_codec_format_temporal_limits()
{
    std::printf("codec: write log values of TIME, DATE, TOD and DT at their limits\n");
    const int64_t MIN    = std::numeric_limits<int64_t>::min();
    const int64_t MAX    = std::numeric_limits<int64_t>::max();
    const int64_t NS_DAY = 86400000000000LL;
    const int64_t D40    = INT64_C(1) << 40;

    CHECK(formats_as(HMI_CODEC_TIME, MIN, "T#-9223372036854775808ns"), "TIME at INT64_MIN");
    CHECK(formats_as(HMI_CODEC_TIME, MAX, "T#9223372036854775807ns"), "TIME at INT64_MAX");
    CHECK(formats_as(HMI_CODEC_TIME, 0, "T#0ns"), "TIME 0");
    CHECK(formats_as(HMI_CODEC_TIME, -1, "T#-1ns"), "TIME -1");
    CHECK(formats_as(HMI_CODEC_LTIME, MIN, "LT#-9223372036854775808ns"), "LTIME at INT64_MIN");
    CHECK(formats_as(HMI_CODEC_LTIME, MAX, "LT#9223372036854775807ns"), "LTIME at INT64_MAX");
    CHECK(formats_as(HMI_CODEC_LTIME, 0, "LT#0ns"), "LTIME 0");
    CHECK(formats_as(HMI_CODEC_LTIME, -1, "LT#-1ns"), "LTIME -1");
    CHECK(formats_as(HMI_CODEC_LTIME, 3600000000000LL, "LT#3600000000000ns"), "LTIME of an hour");

    CHECK(formats_as(HMI_CODEC_DATE, 0, "D#1970-01-01"), "DATE 0");
    CHECK(formats_as(HMI_CODEC_DATE, -1, "D#1969-12-31"), "DATE -1");
    CHECK(formats_as(HMI_CODEC_DATE, 11016, "D#2000-02-29"), "DATE on a leap day");
    CHECK(formats_as(HMI_CODEC_DATE, -719528, "D#0000-01-01"), "DATE in year 0");
    CHECK(formats_as(HMI_CODEC_DATE, D40, "D#3010362559-12-15"), "DATE at +2^40 days");
    CHECK(formats_as(HMI_CODEC_DATE, -D40, "D#-3010358620-01-18"), "DATE at -2^40 days");
    CHECK(formats_as(HMI_CODEC_DATE, D40 + 1, "D#out-of-range(1099511627777 days)"),
          "DATE beyond +2^40 days");
    CHECK(formats_as(HMI_CODEC_DATE, -D40 - 1, "D#out-of-range(-1099511627777 days)"),
          "DATE beyond -2^40 days");
    CHECK(formats_as(HMI_CODEC_DATE, MAX, "D#out-of-range(9223372036854775807 days)"),
          "DATE at INT64_MAX");
    CHECK(formats_as(HMI_CODEC_DATE, MIN, "D#out-of-range(-9223372036854775808 days)"),
          "DATE at INT64_MIN");

    CHECK(formats_as(HMI_CODEC_TOD, 0, "TOD#00:00:00.000"), "TOD 0");
    CHECK(formats_as(HMI_CODEC_TOD, NS_DAY - 1, "TOD#23:59:59.999"), "TOD at the last nanosecond");
    CHECK(formats_as(HMI_CODEC_TOD, 45296789000000LL, "TOD#12:34:56.789"), "TOD at midday");
    CHECK(formats_as(HMI_CODEC_TOD, -1, "TOD#out-of-range(-1ns)"), "negative TOD");
    CHECK(formats_as(HMI_CODEC_TOD, -1000000, "TOD#out-of-range(-1000000ns)"),
          "negative TOD of a millisecond");
    CHECK(formats_as(HMI_CODEC_TOD, NS_DAY, "TOD#out-of-range(86400000000000ns)"), "TOD of 24 h");
    CHECK(formats_as(HMI_CODEC_TOD, MAX, "TOD#out-of-range(9223372036854775807ns)"),
          "TOD at INT64_MAX");
    CHECK(formats_as(HMI_CODEC_TOD, MIN, "TOD#out-of-range(-9223372036854775808ns)"),
          "TOD at INT64_MIN");

    CHECK(formats_as(HMI_CODEC_DT, 0, "DT#1970-01-01-00:00:00.000"), "DT 0");
    CHECK(formats_as(HMI_CODEC_DT, -1, "DT#1969-12-31-23:59:59.999"), "DT -1");
    CHECK(formats_as(HMI_CODEC_DT, -NS_DAY, "DT#1969-12-31-00:00:00.000"),
          "DT one day before 1970");
    CHECK(formats_as(HMI_CODEC_DT, 951782400123000000LL, "DT#2000-02-29-00:00:00.123"),
          "DT on a leap day");
    CHECK(formats_as(HMI_CODEC_DT, MAX, "DT#2262-04-11-23:47:16.854"), "DT at INT64_MAX");
    CHECK(formats_as(HMI_CODEC_DT, MIN, "DT#1677-09-21-00:12:43.145"), "DT at INT64_MIN");
}

static void test_codec_format_numbers_unchanged()
{
    std::printf("codec: write log values of numeric types\n");
    char out[160];
    const uint8_t int_bytes[2] = {0x00, 0x80};
    hmi_codec_format(HMI_CODEC_INT, 0, int_bytes, out, sizeof out);
    CHECK(std::strcmp(out, "-32768") == 0, "INT minimum");
    const uint8_t dint_bytes[4] = {0xFE, 0xFF, 0xFF, 0xFF};
    hmi_codec_format(HMI_CODEC_DINT, 0, dint_bytes, out, sizeof out);
    CHECK(std::strcmp(out, "-2") == 0, "DINT sign");
    const uint8_t sint_bytes[1] = {0x7F};
    hmi_codec_format(HMI_CODEC_SINT, 0, sint_bytes, out, sizeof out);
    CHECK(std::strcmp(out, "127") == 0, "SINT maximum");
    CHECK(formats_as(HMI_CODEC_LINT, std::numeric_limits<int64_t>::min(), "-9223372036854775808"),
          "LINT at INT64_MIN");
    CHECK(formats_as(HMI_CODEC_LINT, std::numeric_limits<int64_t>::max(), "9223372036854775807"),
          "LINT at INT64_MAX");
    CHECK(formats_as(HMI_CODEC_ULINT, -1, "18446744073709551615"), "ULINT maximum");
    CHECK(formats_as(HMI_CODEC_LWORD, 0x0123456789ABCDEFLL, "16#123456789ABCDEF"), "LWORD in hex");
    const uint8_t usint_bytes[1] = {0xC8};
    hmi_codec_format(HMI_CODEC_USINT, 0, usint_bytes, out, sizeof out);
    CHECK(std::strcmp(out, "200") == 0, "USINT");
    const uint8_t uint_bytes[2] = {0xE9, 0xFD};
    hmi_codec_format(HMI_CODEC_UINT, 0, uint_bytes, out, sizeof out);
    CHECK(std::strcmp(out, "65001") == 0, "UINT");
    const uint8_t byte_bytes[1] = {0x0A};
    hmi_codec_format(HMI_CODEC_BYTE, 0, byte_bytes, out, sizeof out);
    CHECK(std::strcmp(out, "16#A") == 0, "BYTE in hex");
    const uint8_t dword_bytes[4] = {0xEF, 0xBE, 0xAD, 0xDE};
    hmi_codec_format(HMI_CODEC_DWORD, 0, dword_bytes, out, sizeof out);
    CHECK(std::strcmp(out, "16#DEADBEEF") == 0, "DWORD in hex");
    const uint8_t real_bytes[4] = {0x00, 0x00, 0x28, 0xC1}; // -10.5f
    hmi_codec_format(HMI_CODEC_REAL, 0, real_bytes, out, sizeof out);
    CHECK(std::strcmp(out, "-10.5") == 0, "REAL negative");
    const uint8_t lreal_bytes[8] = {0x18, 0x2D, 0x44, 0x54, 0xFB, 0x21, 0x09, 0x40}; // pi
    hmi_codec_format(HMI_CODEC_LREAL, 0, lreal_bytes, out, sizeof out);
    CHECK(std::strcmp(out, "3.1415926535897931") == 0, "LREAL");
    const uint8_t f = 0;
    hmi_codec_format(HMI_CODEC_BOOL, 0, &f, out, sizeof out);
    CHECK(std::strcmp(out, "FALSE") == 0, "BOOL FALSE");
}

int main()
{
    test_wire_decode();
    test_wire_malformed();
    test_wire_encode();
    test_values();
    test_codec_numbers();
    test_codec_strings();
    test_codec_format();
    test_codec_format_temporal_limits();
    test_codec_format_numbers_unchanged();
    test_codec_wire();
    test_codec_within();
    test_md5_gate();

    if (g_failures == 0)
        std::printf("all cases passed\n");
    else
        std::printf("%d check(s) failed\n", g_failures);
    return g_failures == 0 ? 0 : 1;
}
