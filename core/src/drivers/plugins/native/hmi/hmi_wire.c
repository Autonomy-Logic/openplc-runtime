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

static void wr_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static void wr_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
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
        if (t < HMI_MSG_OPEN || t > HMI_MSG_BIT)
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
        if (plen < 6)
            return HMI_WIRE_MALFORMED;
        out->viewer   = rd_u32(p);
        out->role     = p[4];
        out->name_len = p[5];
        if (out->role > HMI_WIRE_ROLE_MAX)
            return HMI_WIRE_MALFORMED;
        if (out->name_len > HMI_WIRE_NAME_MAX || plen != 6u + out->name_len)
            return HMI_WIRE_MALFORMED;
        out->name_valid = hmi_wire_name_valid(p + 6, out->name_len);
        if (!out->name_valid)
            out->name_len = 0;
        memcpy(out->name, p + 6, out->name_len);
        out->name[out->name_len] = '\0';
        break;
    }
    case HMI_MSG_CLOSE:
        if (plen != 4)
            return HMI_WIRE_MALFORMED;
        out->viewer = rd_u32(p);
        break;
    case HMI_MSG_SHOW:
        if (plen != 6)
            return HMI_WIRE_MALFORMED;
        out->viewer = rd_u32(p);
        out->page   = rd_u16(p + 4);
        break;
    case HMI_MSG_WRITE:
        if (plen < 8)
            return HMI_WIRE_MALFORMED;
        out->viewer    = rd_u32(p);
        out->tag       = rd_u16(p + 4);
        out->value_len = rd_u16(p + 6);
        if (out->value_len > HMI_WIRE_VALUE_MAX || plen != 8u + out->value_len)
            return HMI_WIRE_MALFORMED;
        memcpy(out->value, p + 8, out->value_len);
        break;
    case HMI_MSG_BIT:
        if (plen != 8)
            return HMI_WIRE_MALFORMED;
        out->viewer    = rd_u32(p);
        out->tag       = rd_u16(p + 4);
        out->bit       = p[6];
        out->bit_value = p[7];
        if (out->bit > HMI_WIRE_BIT_MAX || out->bit_value > 1u)
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

size_t hmi_wire_encode_refused(uint32_t viewer, uint8_t what, uint16_t id, uint8_t *out)
{
    wr_u32(out, 8);
    out[4] = HMI_MSG_REFUSED;
    wr_u32(out + 5, viewer);
    out[9] = what;
    wr_u16(out + 10, id);
    return HMI_WIRE_REFUSED_MSG;
}

bool hmi_wire_values_header(uint32_t viewer, uint16_t page, uint32_t writes, uint16_t count,
                            size_t records_len, uint8_t *out)
{
    if (records_len > HMI_WIRE_MAX_MESSAGE - 13u)
        return false;
    wr_u32(out, (uint32_t)(records_len + 13u));
    out[4] = HMI_MSG_VALUES;
    wr_u32(out + 5, viewer);
    wr_u16(out + 9, page);
    wr_u32(out + 11, writes);
    wr_u16(out + 15, count);
    return true;
}

void hmi_wire_record_header(uint16_t tag, uint16_t len, uint8_t *out)
{
    wr_u16(out, tag);
    wr_u16(out + 2, len);
}
