// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#include "hmi_wire.h"

#include <string.h>

static uint32_t rd_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint16_t rd_u16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static int32_t rd_i32(const uint8_t *p)
{
    uint32_t u = rd_u32(p);
    int32_t v;
    memcpy(&v, &u, sizeof v);
    return v;
}

static void wr_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static bool viewport_valid(uint16_t w, uint16_t h, uint16_t d)
{
    return w >= HMI_WIRE_SIZE_MIN && w <= HMI_WIRE_SIZE_MAX && h >= HMI_WIRE_SIZE_MIN &&
           h <= HMI_WIRE_SIZE_MAX && d >= HMI_WIRE_DENSITY_MIN && d <= HMI_WIRE_DENSITY_MAX;
}

bool hmi_wire_name_valid(const uint8_t *s, size_t len)
{
    size_t i = 0;
    while (i < len)
    {
        uint8_t c = s[i];
        if (c < 0x80)
        {
            if (c < 0x20 || c == 0x7F)
                return false;
            i++;
            continue;
        }
        size_t n;
        uint32_t cp;
        if (c >= 0xC2 && c <= 0xDF)
        {
            n  = 1;
            cp = c & 0x1Fu;
        }
        else if (c >= 0xE0 && c <= 0xEF)
        {
            n  = 2;
            cp = c & 0x0Fu;
        }
        else if (c >= 0xF0 && c <= 0xF4)
        {
            n  = 3;
            cp = c & 0x07u;
        }
        else
        {
            return false;
        }
        if (len - i - 1 < n)
            return false;
        for (size_t k = 1; k <= n; k++)
        {
            uint8_t cc = s[i + k];
            if ((cc & 0xC0u) != 0x80u)
                return false;
            cp = (cp << 6) | (cc & 0x3Fu);
        }
        /* Overlong forms, surrogates, code points past U+10FFFF and C1 controls. */
        if ((n == 2 && cp < 0x800u) || (n == 3 && cp < 0x10000u) || cp > 0x10FFFFu ||
            (cp >= 0xD800u && cp <= 0xDFFFu) || (cp >= 0x80u && cp <= 0x9Fu))
            return false;
        i += n + 1;
    }
    return true;
}

hmi_wire_result_t hmi_wire_decode(const uint8_t *buf, size_t len, hmi_wire_msg_t *out,
                                  size_t *consumed)
{
    if (!out || !consumed)
        return HMI_WIRE_MALFORMED;
    if (len < HMI_WIRE_PREFIX)
        return HMI_WIRE_NEED_MORE;
    if (!buf)
        return HMI_WIRE_MALFORMED;

    const uint32_t mlen = rd_u32(buf);
    if (mlen == 0 || mlen > HMI_WIRE_MAX_INBOUND - HMI_WIRE_PREFIX)
        return HMI_WIRE_MALFORMED;

    /* The type is known before the payload arrives, so an unknown one fails at once. */
    if (len > HMI_WIRE_PREFIX)
    {
        const uint8_t t = buf[HMI_WIRE_PREFIX];
        if (t < HMI_MSG_OPEN || t > HMI_MSG_KEY)
            return HMI_WIRE_MALFORMED;
    }
    if (len < HMI_WIRE_PREFIX + (size_t)mlen)
        return HMI_WIRE_NEED_MORE;

    const uint8_t type = buf[HMI_WIRE_PREFIX];
    const uint8_t *p   = buf + HMI_WIRE_PREFIX + 1;
    const size_t plen  = (size_t)mlen - 1;

    memset(out, 0, sizeof *out);
    out->type = type;

    switch (type)
    {
    case HMI_MSG_OPEN:
    {
        if (plen < 12)
            return HMI_WIRE_MALFORMED;
        out->viewer   = rd_u32(p);
        out->role     = p[4];
        out->width    = rd_u16(p + 5);
        out->height   = rd_u16(p + 7);
        out->density  = rd_u16(p + 9);
        out->name_len = p[11];
        if (out->role > 3 || !viewport_valid(out->width, out->height, out->density))
            return HMI_WIRE_MALFORMED;
        if (out->name_len > HMI_WIRE_NAME_MAX || plen != 12u + out->name_len)
            return HMI_WIRE_MALFORMED;
        out->name_valid = hmi_wire_name_valid(p + 12, out->name_len);
        if (!out->name_valid)
            out->name_len = 0;
        memcpy(out->name, p + 12, out->name_len);
        out->name[out->name_len] = '\0';
        break;
    }
    case HMI_MSG_CLOSE:
        if (plen != 4)
            return HMI_WIRE_MALFORMED;
        out->viewer = rd_u32(p);
        break;
    case HMI_MSG_VIEWPORT:
        if (plen != 10)
            return HMI_WIRE_MALFORMED;
        out->viewer  = rd_u32(p);
        out->width   = rd_u16(p + 4);
        out->height  = rd_u16(p + 6);
        out->density = rd_u16(p + 8);
        if (!viewport_valid(out->width, out->height, out->density))
            return HMI_WIRE_MALFORMED;
        break;
    case HMI_MSG_POINTER:
        if (plen != 13)
            return HMI_WIRE_MALFORMED;
        out->viewer = rd_u32(p);
        out->kind   = p[4];
        out->x100   = rd_i32(p + 5);
        out->y100   = rd_i32(p + 9);
        if (out->kind > 2)
            return HMI_WIRE_MALFORMED;
        break;
    case HMI_MSG_KEY:
        if (plen != 8)
            return HMI_WIRE_MALFORMED;
        out->viewer = rd_u32(p);
        out->code   = rd_u32(p + 4);
        if (out->code == 0 || out->code > HMI_WIRE_KEY_MAX)
            return HMI_WIRE_MALFORMED;
        break;
    default:
        return HMI_WIRE_MALFORMED;
    }

    *consumed = HMI_WIRE_PREFIX + (size_t)mlen;
    return HMI_WIRE_DECODED;
}

size_t hmi_wire_encode_viewer(uint8_t type, uint32_t viewer, uint8_t *out)
{
    wr_u32(out, 5);
    out[4] = type;
    wr_u32(out + 5, viewer);
    return HMI_WIRE_VIEWER_MSG;
}

size_t hmi_wire_encode_status(uint8_t state, uint8_t *out)
{
    wr_u32(out, 2);
    out[4] = HMI_MSG_STATUS;
    out[5] = state;
    return HMI_WIRE_STATUS_MSG;
}

bool hmi_wire_frame_header(uint32_t viewer, size_t dl_len, uint8_t *out)
{
    if (dl_len > HMI_WIRE_MAX_MESSAGE - 5u)
        return false;
    wr_u32(out, (uint32_t)(dl_len + 5u));
    out[4] = HMI_MSG_FRAME;
    wr_u32(out + 5, viewer);
    return true;
}
