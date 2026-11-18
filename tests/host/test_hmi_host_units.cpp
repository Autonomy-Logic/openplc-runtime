// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

// Host test of the HMI plugin's pure parts: wire protocol, input queue and coalescing,
// latest-frame slot, buffer <-> debug conversion and the MD5 gate.

#include "drivers/plugins/native/hmi/hmi_codec.h"
#include "drivers/plugins/native/hmi/hmi_input.h"
#include "drivers/plugins/native/hmi/hmi_slot.h"
#include "drivers/plugins/native/hmi/hmi_wire.h"

#include <cstdio>
#include <cstring>
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

static Bytes message(uint8_t type, const Bytes &payload)
{
    Bytes b;
    put_u32(b, (uint32_t)(payload.size() + 1));
    b.push_back(type);
    b.insert(b.end(), payload.begin(), payload.end());
    return b;
}

static Bytes open_payload(uint32_t viewer, uint8_t role, uint16_t w, uint16_t h, uint16_t d,
                          const std::string &name)
{
    Bytes p;
    put_u32(p, viewer);
    p.push_back(role);
    put_u16(p, w);
    put_u16(p, h);
    put_u16(p, d);
    p.push_back((uint8_t)name.size());
    p.insert(p.end(), name.begin(), name.end());
    return p;
}

static hmi_wire_result_t decode(const Bytes &b, hmi_wire_msg_t *m, size_t *used)
{
    return hmi_wire_decode(b.data(), b.size(), m, used);
}

/* ---- wire ---------------------------------------------------------------- */

static void test_wire_decode()
{
    std::printf("wire decoder\n");
    hmi_wire_msg_t m;
    size_t used = 0;

    Bytes open = message(0x01, open_payload(42, 2, 1280, 800, 150, "Jos\xC3\xA9"));
    CHECK(decode(open, &m, &used) == HMI_WIRE_DECODED, "OPEN decodes");
    CHECK(used == open.size(), "OPEN consumes the whole message");
    CHECK(m.viewer == 42 && m.role == 2 && m.width == 1280 && m.height == 800 && m.density == 150,
          "OPEN fields");
    CHECK(m.name_valid && m.name_len == 5 && std::strcmp(m.name, "Jos\xC3\xA9") == 0,
          "OPEN name, UTF-8");

    for (size_t cut = 0; cut < open.size(); cut++)
    {
        Bytes part(open.begin(), open.begin() + (long)cut);
        if (decode(part, &m, &used) != HMI_WIRE_NEED_MORE)
        {
            CHECK(false, "a prefix of a valid message needs more");
            break;
        }
    }

    Bytes two = message(0x02, {7, 0, 0, 0});
    Bytes key = message(0x05, {7, 0, 0, 0, 65, 0, 0, 0});
    two.insert(two.end(), key.begin(), key.end());
    CHECK(decode(two, &m, &used) == HMI_WIRE_DECODED && m.type == 0x02 && m.viewer == 7 &&
              used == 9,
          "first of two messages");
    Bytes rest(two.begin() + (long)used, two.end());
    CHECK(decode(rest, &m, &used) == HMI_WIRE_DECODED && m.type == 0x05 && m.code == 65,
          "second of two messages");

    Bytes vp;
    put_u32(vp, 3);
    put_u16(vp, 8192);
    put_u16(vp, 1);
    put_u16(vp, 800);
    CHECK(decode(message(0x03, vp), &m, &used) == HMI_WIRE_DECODED && m.width == 8192 &&
              m.height == 1 && m.density == 800,
          "VIEWPORT at the bounds");

    Bytes ptr;
    put_u32(ptr, 3);
    ptr.push_back(1);
    put_u32(ptr, (uint32_t)-1250);
    put_u32(ptr, 99999);
    CHECK(decode(message(0x04, ptr), &m, &used) == HMI_WIRE_DECODED && m.kind == 1 &&
              m.x100 == -1250 && m.y100 == 99999,
          "POINTER with a negative coordinate");
}

static void test_wire_malformed()
{
    std::printf("wire decoder refuses\n");
    hmi_wire_msg_t m;
    size_t used = 0;
    struct Case
    {
        const char *what;
        Bytes bytes;
    };
    Bytes vp_small;
    put_u32(vp_small, 1);
    put_u16(vp_small, 0);
    put_u16(vp_small, 10);
    put_u16(vp_small, 100);
    Bytes vp_density;
    put_u32(vp_density, 1);
    put_u16(vp_density, 10);
    put_u16(vp_density, 10);
    put_u16(vp_density, 24);
    Bytes vp_big;
    put_u32(vp_big, 1);
    put_u16(vp_big, 8193);
    put_u16(vp_big, 10);
    put_u16(vp_big, 100);
    Bytes ptr_kind;
    put_u32(ptr_kind, 1);
    ptr_kind.push_back(3);
    put_u32(ptr_kind, 0);
    put_u32(ptr_kind, 0);
    Bytes open_len = open_payload(1, 0, 10, 10, 100, "abc");
    open_len.push_back('x'); // one byte past the name
    Bytes long_name = open_payload(1, 0, 10, 10, 100, std::string(65, 'a'));
    const std::vector<Case> cases = {
        {"zero length", {0, 0, 0, 0}},
        {"length past the largest message", {0xFF, 0, 0, 0, 0x01}},
        {"4 MiB length", {0, 0, 0x40, 0}},
        {"unknown type, before its payload", {3, 0, 0, 0, 0x06}},
        {"host type sent by the web server", {2, 0, 0, 0, 0x82, 0}},
        {"FAULT sent by the web server", message(0x85, {1, 0, 0, 0})},
        {"CLOSE too short", message(0x02, {1, 0, 0})},
        {"CLOSE too long", message(0x02, {1, 0, 0, 0, 0})},
        {"KEY code 0", message(0x05, {1, 0, 0, 0, 0, 0, 0, 0})},
        {"KEY past U+10FFFF", message(0x05, {1, 0, 0, 0, 0, 0, 0x11, 0})},
        {"OPEN role 4", message(0x01, open_payload(1, 4, 10, 10, 100, "a"))},
        {"OPEN width 0", message(0x01, open_payload(1, 0, 0, 10, 100, "a"))},
        {"OPEN density 801", message(0x01, open_payload(1, 0, 10, 10, 801, "a"))},
        {"OPEN name length disagrees", message(0x01, open_len)},
        {"OPEN name of 65 bytes", message(0x01, long_name)},
        {"VIEWPORT width 0", message(0x03, vp_small)},
        {"VIEWPORT density 24", message(0x03, vp_density)},
        {"VIEWPORT width 8193", message(0x03, vp_big)},
        {"POINTER kind 3", message(0x04, ptr_kind)},
    };
    for (const Case &c : cases)
        CHECK(decode(c.bytes, &m, &used) == HMI_WIRE_MALFORMED, c.what);

    /* A bad name refuses that viewer only: the message decodes, flagged, with no name. */
    const Case refused[] = {
        {"OPEN name with a control character",
         message(0x01, open_payload(1, 0, 10, 10, 100, "a\nb"))},
        {"OPEN name with a C1 control",
         message(0x01, open_payload(1, 0, 10, 10, 100, "a\xC2\x85"))},
        {"OPEN name with invalid UTF-8", message(0x01, open_payload(1, 0, 10, 10, 100, "a\xC3"))},
        {"OPEN name with an overlong form",
         message(0x01, open_payload(1, 0, 10, 10, 100, "\xC0\xAF"))},
        {"OPEN name with a surrogate",
         message(0x01, open_payload(1, 0, 10, 10, 100, "\xED\xA0\x80"))},
    };
    for (const Case &c : refused)
        CHECK(decode(c.bytes, &m, &used) == HMI_WIRE_DECODED && !m.name_valid && m.name_len == 0 &&
                  m.name[0] == '\0',
              c.what);
}

static void test_wire_encode()
{
    std::printf("wire encoders\n");
    uint8_t b[16];
    CHECK(hmi_wire_encode_viewer(HMI_MSG_OPENED, 0x01020304u, b) == 9, "OPENED length");
    const uint8_t opened[] = {5, 0, 0, 0, 0x83, 4, 3, 2, 1};
    CHECK(std::memcmp(b, opened, 9) == 0, "OPENED bytes");
    CHECK(hmi_wire_encode_viewer(HMI_MSG_BUSY, 9, b) == 9 && b[4] == 0x84, "BUSY");
    CHECK(HMI_MSG_FAULT == 0x85, "FAULT is type 0x85");
    CHECK(hmi_wire_encode_viewer(HMI_MSG_FAULT, 0x0A0B0C0Du, b) == HMI_WIRE_VIEWER_MSG,
          "FAULT length");
    const uint8_t fault[] = {5, 0, 0, 0, 0x85, 0x0D, 0x0C, 0x0B, 0x0A};
    CHECK(std::memcmp(b, fault, 9) == 0, "FAULT bytes");
    CHECK(hmi_wire_encode_status(HMI_STATE_OUT_OF_DATE, b) == 6, "STATUS length");
    const uint8_t status[] = {2, 0, 0, 0, 0x82, 1};
    CHECK(std::memcmp(b, status, 6) == 0, "STATUS bytes");
    CHECK(hmi_wire_frame_header(7, 100, b), "FRAME header");
    const uint8_t frame[] = {105, 0, 0, 0, 0x81, 7, 0, 0, 0};
    CHECK(std::memcmp(b, frame, 9) == 0, "FRAME header bytes");
    CHECK(hmi_wire_frame_header(7, HMI_WIRE_MAX_MESSAGE - 5u, b), "largest FRAME");
    CHECK(!hmi_wire_frame_header(7, HMI_WIRE_MAX_MESSAGE - 4u, b), "FRAME past 4 MiB refused");
}

/* ---- input queue --------------------------------------------------------- */

static hmi_input_event_t ptr(uint8_t kind, int32_t x)
{
    hmi_input_event_t e{};
    e.kind     = HMI_INPUT_POINTER;
    e.ptr_kind = kind;
    e.x_q4     = x;
    e.y_q4     = -x;
    return e;
}

static hmi_input_event_t key(uint32_t code)
{
    hmi_input_event_t e{};
    e.kind = HMI_INPUT_KEY;
    e.code = code;
    return e;
}

static void test_input_order_and_frames()
{
    std::printf("input queue: order and frames\n");
    hmi_input_queue_t q;
    hmi_input_reset(&q);
    hmi_input_event_t e = ptr(HMI_INPUT_MOVE, 1);
    hmi_input_push(&q, &e);
    e = ptr(HMI_INPUT_DOWN, 2);
    hmi_input_push(&q, &e);
    e = key('a');
    hmi_input_push(&q, &e);
    e = ptr(HMI_INPUT_UP, 3);
    hmi_input_push(&q, &e);
    e = ptr(HMI_INPUT_MOVE, 4);
    hmi_input_push(&q, &e);

    hmi_input_event_t out[HMI_INPUT_QUEUE_CAP];
    size_t n = hmi_input_take_frame(&q, out, HMI_INPUT_QUEUE_CAP);
    CHECK(n == 2 && out[0].ptr_kind == HMI_INPUT_MOVE && out[1].ptr_kind == HMI_INPUT_DOWN,
          "a frame takes events up to the first press");
    n = hmi_input_take_frame(&q, out, HMI_INPUT_QUEUE_CAP);
    CHECK(n == 2 && out[0].kind == HMI_INPUT_KEY && out[1].ptr_kind == HMI_INPUT_UP,
          "then up to the release, keys in order");
    n = hmi_input_take_frame(&q, out, HMI_INPUT_QUEUE_CAP);
    CHECK(n == 1 && out[0].x_q4 == 4, "then the trailing move");
    CHECK(hmi_input_take_frame(&q, out, HMI_INPUT_QUEUE_CAP) == 0, "then nothing");

    for (uint32_t i = 1; i <= HMI_INPUT_KEYS_PER_FRAME + 3; i++)
    {
        e = key(i);
        hmi_input_push(&q, &e);
    }
    n = hmi_input_take_frame(&q, out, HMI_INPUT_QUEUE_CAP);
    CHECK(n == HMI_INPUT_KEYS_PER_FRAME && out[0].code == 1, "keys per frame are capped");
    n = hmi_input_take_frame(&q, out, HMI_INPUT_QUEUE_CAP);
    CHECK(n == 3 && out[0].code == HMI_INPUT_KEYS_PER_FRAME + 1, "the rest follow next frame");
}

static void test_input_coalescing()
{
    std::printf("input queue: coalescing\n");
    hmi_input_queue_t q;
    hmi_input_reset(&q);
    hmi_input_event_t e;

    /* Fill with moves around one press and one release, then overflow with a press. */
    for (int i = 0; i < 60; i++)
    {
        e = ptr(HMI_INPUT_MOVE, i);
        CHECK(hmi_input_push(&q, &e), "move queued");
    }
    e = ptr(HMI_INPUT_DOWN, 100);
    hmi_input_push(&q, &e);
    for (int i = 0; i < 60; i++)
    {
        e = ptr(HMI_INPUT_MOVE, 200 + i);
        hmi_input_push(&q, &e);
    }
    e = ptr(HMI_INPUT_UP, 300);
    hmi_input_push(&q, &e);
    for (int i = 0; (size_t)q.count < HMI_INPUT_QUEUE_CAP; i++)
    {
        e = ptr(HMI_INPUT_MOVE, 400 + i);
        hmi_input_push(&q, &e);
    }
    CHECK(q.count == HMI_INPUT_QUEUE_CAP, "queue full");
    e = ptr(HMI_INPUT_DOWN, 500);
    CHECK(hmi_input_push(&q, &e), "a press into a full queue coalesces moves");

    hmi_input_event_t out[HMI_INPUT_QUEUE_CAP];
    std::vector<hmi_input_event_t> all;
    size_t n;
    while ((n = hmi_input_take_frame(&q, out, HMI_INPUT_QUEUE_CAP)) > 0)
        all.insert(all.end(), out, out + n);
    int presses = 0, releases = 0;
    for (const auto &x : all)
    {
        presses += x.ptr_kind == HMI_INPUT_DOWN;
        releases += x.ptr_kind == HMI_INPUT_UP;
    }
    CHECK(presses == 2 && releases == 1, "no press or release lost");
    CHECK(all.size() >= 4 && all[0].ptr_kind == HMI_INPUT_DOWN && all[0].x_q4 == 100 &&
              all[1].ptr_kind == HMI_INPUT_UP && all[1].x_q4 == 300 &&
              all.back().ptr_kind == HMI_INPUT_DOWN && all.back().x_q4 == 500,
          "presses and releases keep their order");
    bool latest_move_kept = false;
    for (const auto &x : all)
        latest_move_kept |= x.ptr_kind == HMI_INPUT_MOVE && x.x_q4 >= 400;
    CHECK(latest_move_kept, "the latest move before the press survives");

    /* A queue of presses only: a move is coalesced away, a press is refused. */
    hmi_input_reset(&q);
    for (size_t i = 0; i < HMI_INPUT_QUEUE_CAP; i++)
    {
        e = ptr(i % 2 ? HMI_INPUT_UP : HMI_INPUT_DOWN, (int32_t)i);
        hmi_input_push(&q, &e);
    }
    e = ptr(HMI_INPUT_MOVE, 7);
    CHECK(hmi_input_push(&q, &e) && q.count == HMI_INPUT_QUEUE_CAP,
          "a move into a queue of presses is coalesced away");
    e = ptr(HMI_INPUT_DOWN, 8);
    CHECK(!hmi_input_push(&q, &e), "a press that cannot fit is reported, never dropped silently");
    e = key('k');
    CHECK(!hmi_input_push(&q, &e), "a key that cannot fit is reported");

    /* Full with one trailing move: an incoming move replaces it. */
    hmi_input_reset(&q);
    for (size_t i = 0; i + 1 < HMI_INPUT_QUEUE_CAP; i++)
    {
        e = key((uint32_t)i + 1);
        hmi_input_push(&q, &e);
    }
    e = ptr(HMI_INPUT_MOVE, 1);
    hmi_input_push(&q, &e);
    e = ptr(HMI_INPUT_MOVE, 2);
    CHECK(hmi_input_push(&q, &e) && q.count == HMI_INPUT_QUEUE_CAP, "moves merge when full");
    bool found = false;
    for (size_t i = 0; i < q.count; i++)
    {
        const hmi_input_event_t &x = q.ev[(q.head + i) % HMI_INPUT_QUEUE_CAP];
        if (x.kind == HMI_INPUT_POINTER)
            found = x.x_q4 == 2;
    }
    CHECK(found, "the newest move wins");
}

/* ---- latest-frame slot --------------------------------------------------- */

static void test_slot()
{
    std::printf("latest-frame slot\n");
    hmi_slot_t slot{};
    hmi_buf_t out{};
    const uint8_t dl1[] = {1, 2, 3};
    const uint8_t dl2[] = {4, 5, 6, 7};

    CHECK(!hmi_slot_take(&slot, &out), "empty slot gives nothing");
    CHECK(hmi_slot_offer(&slot, 5, dl1, sizeof dl1, 111) == HMI_SLOT_STORED, "first frame stored");
    CHECK(hmi_slot_offer(&slot, 5, dl1, sizeof dl1, 111) == HMI_SLOT_UNCHANGED,
          "same hash not stored");
    CHECK(hmi_slot_offer(&slot, 5, dl2, sizeof dl2, 222) == HMI_SLOT_STORED,
          "a newer frame replaces the pending one");
    CHECK(hmi_slot_take(&slot, &out), "pending frame taken");
    CHECK(out.len == HMI_WIRE_FRAME_HEADER + sizeof dl2 && out.data[4] == 0x81 && out.data[5] == 5 &&
              std::memcmp(out.data + HMI_WIRE_FRAME_HEADER, dl2, sizeof dl2) == 0,
          "only the latest frame, as a FRAME message");
    CHECK(!hmi_slot_take(&slot, &out), "taken once");
    CHECK(hmi_slot_offer(&slot, 5, dl2, sizeof dl2, 222) == HMI_SLOT_UNCHANGED,
          "an unchanged frame after a send is skipped");
    hmi_slot_invalidate(&slot);
    CHECK(hmi_slot_offer(&slot, 5, dl2, sizeof dl2, 222) == HMI_SLOT_STORED,
          "invalidate forces the next frame");
    hmi_buf_t again{};
    CHECK(hmi_slot_take(&slot, &again) && again.data != out.data, "buffers swap, no sharing");
    hmi_buf_free(&out);
    hmi_buf_free(&again);
    hmi_slot_free(&slot);
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

int main()
{
    test_wire_decode();
    test_wire_malformed();
    test_wire_encode();
    test_input_order_and_frames();
    test_input_coalescing();
    test_slot();
    test_codec_numbers();
    test_codec_strings();
    test_codec_format();
    test_md5_gate();

    if (g_failures == 0)
        std::printf("all cases passed\n");
    else
        std::printf("%d check(s) failed\n", g_failures);
    return g_failures == 0 ? 0 : 1;
}
