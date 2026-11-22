// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#include "hmi_codec.h"

#include <ctype.h>
#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

static bool target_is_little_endian(void)
{
#if defined(__BYTE_ORDER__) && defined(__ORDER_LITTLE_ENDIAN__)
    return __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__;
#else
    const uint16_t probe = 1;
    uint8_t first;
    memcpy(&first, &probe, 1);
    return first == 1;
#endif
}

/** Copies n bytes between little-endian and target order. */
static void copy_ordered(uint8_t *dst, const uint8_t *src, size_t n)
{
    if (target_is_little_endian())
    {
        memcpy(dst, src, n);
        return;
    }
    for (size_t i = 0; i < n; i++)
        dst[i] = src[n - 1 - i];
}

size_t hmi_codec_width(uint8_t type)
{
    switch (type)
    {
    case HMI_CODEC_BOOL:
    case HMI_CODEC_SINT:
    case HMI_CODEC_USINT:
    case HMI_CODEC_BYTE:
        return 1;
    case HMI_CODEC_INT:
    case HMI_CODEC_UINT:
    case HMI_CODEC_WORD:
        return 2;
    case HMI_CODEC_DINT:
    case HMI_CODEC_UDINT:
    case HMI_CODEC_DWORD:
    case HMI_CODEC_REAL:
        return 4;
    case HMI_CODEC_LINT:
    case HMI_CODEC_ULINT:
    case HMI_CODEC_LWORD:
    case HMI_CODEC_LREAL:
    case HMI_CODEC_TIME:
    case HMI_CODEC_LTIME:
    case HMI_CODEC_DATE:
    case HMI_CODEC_TOD:
    case HMI_CODEC_DT:
        return 8;
    default:
        return 0;
    }
}

size_t hmi_codec_buffer_size(uint8_t type, uint16_t max_len)
{
    if (type == HMI_CODEC_STRING)
        return (size_t)max_len + 1u;
    return hmi_codec_width(type);
}

bool hmi_codec_from_debug(uint8_t type, uint16_t max_len, const uint8_t *wire, size_t wire_len,
                          void *dest)
{
    if (!wire || !dest || wire_len == 0 || wire_len > HMI_CODEC_DEBUG_MAX)
        return false;
    uint8_t *out = dest;

    if (type == HMI_CODEC_STRING)
    {
        size_t n = wire[0];
        if (n > wire_len - 1u)
            n = wire_len - 1u;
        if (n > max_len)
            n = max_len;
        memcpy(out, wire + 1, n);
        out[n] = '\0';
        return true;
    }

    const size_t width = hmi_codec_width(type);
    if (width == 0 || wire_len != width)
        return false;
    if (type == HMI_CODEC_BOOL)
    {
        out[0] = wire[0] != 0 ? 1u : 0u;
        return true;
    }
    copy_ordered(out, wire, width);
    return true;
}

bool hmi_codec_to_debug(uint8_t type, uint16_t max_len, const void *src, uint8_t *wire,
                        size_t wire_len, bool *truncated)
{
    if (truncated)
        *truncated = false;
    if (!src || !wire || wire_len == 0 || wire_len > HMI_CODEC_DEBUG_MAX)
        return false;
    const uint8_t *in = src;

    if (type == HMI_CODEC_STRING)
    {
        const size_t window = wire_len - 1u;
        if (window > UINT8_MAX)
            return false;
        size_t n = 0;
        while (n < max_len && in[n] != '\0')
            n++;
        if (n > window)
        {
            n = window;
            if (truncated)
                *truncated = true;
        }
        memset(wire, 0, wire_len);
        wire[0] = (uint8_t)n;
        memcpy(wire + 1, in, n);
        return true;
    }

    const size_t width = hmi_codec_width(type);
    if (width == 0 || wire_len != width)
        return false;
    if (type == HMI_CODEC_BOOL)
    {
        wire[0] = in[0] != 0 ? 1u : 0u;
        return true;
    }
    copy_ordered(wire, in, width);
    return true;
}

static uint64_t le_unsigned(const uint8_t *p, size_t width)
{
    uint64_t v = 0;
    for (size_t i = width; i > 0; i--)
        v = (v << 8) | p[i - 1];
    return v;
}

static int64_t le_signed(const uint8_t *p, size_t width)
{
    uint64_t v = le_unsigned(p, width);
    if (width < 8 && (v & (UINT64_C(1) << (width * 8 - 1))))
        v |= ~UINT64_C(0) << (width * 8);
    int64_t s;
    memcpy(&s, &v, sizeof s);
    return s;
}

static void format_string(const uint8_t *s, uint16_t max_len, char *out, size_t cap)
{
    size_t o = 0;
    out[o++] = '\'';
    for (size_t i = 0; i < max_len && s[i] != '\0'; i++)
    {
        char tmp[5];
        const uint8_t c = s[i];
        if (c == '\'' || c == '\\')
            snprintf(tmp, sizeof tmp, "\\%c", (char)c);
        else if (c < 0x20 || c == 0x7F)
            snprintf(tmp, sizeof tmp, "\\x%02X", c);
        else
        {
            tmp[0] = (char)c;
            tmp[1] = '\0';
        }
        const size_t n = strlen(tmp);
        if (o + n + 2 > cap)
            break;
        memcpy(out + o, tmp, n);
        o += n;
    }
    out[o++] = '\'';
    out[o]   = '\0';
}

/* Calendar date of a day count since 1970-01-01 (proleptic Gregorian). */
static void civil_from_days(int64_t z, int64_t *y, unsigned *m, unsigned *d)
{
    z += 719468;
    const int64_t era  = (z >= 0 ? z : z - 146096) / 146097;
    const unsigned doe = (unsigned)(z - era * 146097);
    const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const unsigned mp  = (5 * doy + 2) / 153;
    *d                 = doy - (153 * mp + 2) / 5 + 1;
    *m                 = mp < 10 ? mp + 3 : mp - 9;
    *y                 = (int64_t)yoe + era * 400 + (*m <= 2);
}

#define NS_PER_DAY INT64_C(86400000000000)

/* Largest day count magnitude a DATE is shown as a calendar date for: 2^40 days. */
#define DATE_DAYS_MAX (INT64_C(1) << 40)

/* Splits ns into whole days (rounded down) and the nanoseconds left, 0 <= *rem < NS_PER_DAY. */
static void split_days(int64_t ns, int64_t *day, int64_t *rem)
{
    int64_t q = ns / NS_PER_DAY;
    int64_t r = ns % NS_PER_DAY;
    if (r < 0)
    {
        r += NS_PER_DAY;
        q -= 1;
    }
    *day = q;
    *rem = r;
}

/* hh:mm:ss.fff of a nanosecond count within a day. */
static void format_clock(int64_t ns, char *out, size_t cap)
{
    const int64_t ms = ns / 1000000;
    snprintf(out, cap, "%02d:%02d:%02d.%03d", (int)(ms / 3600000), (int)(ms / 60000 % 60),
             (int)(ms / 1000 % 60), (int)(ms % 1000));
}

void hmi_codec_format(uint8_t type, uint16_t max_len, const void *buf, char *out, size_t cap)
{
    if (!out || cap == 0)
        return;
    out[0] = '\0';
    if (!buf)
        return;
    const uint8_t *p = buf;

    switch (type)
    {
    case HMI_CODEC_BOOL:
        snprintf(out, cap, "%s", p[0] ? "TRUE" : "FALSE");
        return;
    case HMI_CODEC_SINT:
    case HMI_CODEC_INT:
    case HMI_CODEC_DINT:
    case HMI_CODEC_LINT:
        snprintf(out, cap, "%" PRId64, le_signed(p, hmi_codec_width(type)));
        return;
    case HMI_CODEC_USINT:
    case HMI_CODEC_UINT:
    case HMI_CODEC_UDINT:
    case HMI_CODEC_ULINT:
        snprintf(out, cap, "%" PRIu64, le_unsigned(p, hmi_codec_width(type)));
        return;
    case HMI_CODEC_BYTE:
    case HMI_CODEC_WORD:
    case HMI_CODEC_DWORD:
    case HMI_CODEC_LWORD:
        snprintf(out, cap, "16#%" PRIX64, le_unsigned(p, hmi_codec_width(type)));
        return;
    case HMI_CODEC_REAL:
    {
        uint32_t u = (uint32_t)le_unsigned(p, 4);
        float f;
        memcpy(&f, &u, sizeof f);
        snprintf(out, cap, "%.9g", (double)f);
        return;
    }
    case HMI_CODEC_LREAL:
    {
        uint64_t u = le_unsigned(p, 8);
        double d;
        memcpy(&d, &u, sizeof d);
        snprintf(out, cap, "%.17g", d);
        return;
    }
    case HMI_CODEC_STRING:
        if (cap >= 3)
            format_string(p, max_len, out, cap);
        return;
    case HMI_CODEC_TIME:
    case HMI_CODEC_LTIME:
        snprintf(out, cap, "%s#%" PRId64 "ns", type == HMI_CODEC_TIME ? "T" : "LT",
                 le_signed(p, 8));
        return;
    case HMI_CODEC_DATE:
    {
        const int64_t days = le_signed(p, 8);
        if (days < -DATE_DAYS_MAX || days > DATE_DAYS_MAX)
        {
            snprintf(out, cap, "D#out-of-range(%" PRId64 " days)", days);
            return;
        }
        int64_t y;
        unsigned m, d;
        civil_from_days(days, &y, &m, &d);
        snprintf(out, cap, "D#%04" PRId64 "-%02u-%02u", y, m, d);
        return;
    }
    case HMI_CODEC_TOD:
    {
        const int64_t ns = le_signed(p, 8);
        if (ns < 0 || ns >= NS_PER_DAY)
        {
            snprintf(out, cap, "TOD#out-of-range(%" PRId64 "ns)", ns);
            return;
        }
        char clock[24];
        format_clock(ns, clock, sizeof clock);
        snprintf(out, cap, "TOD#%s", clock);
        return;
    }
    case HMI_CODEC_DT:
    {
        int64_t day, rem;
        split_days(le_signed(p, 8), &day, &rem);
        int64_t y;
        unsigned m, d;
        char clock[24];
        civil_from_days(day, &y, &m, &d);
        format_clock(rem, clock, sizeof clock);
        snprintf(out, cap, "DT#%04" PRId64 "-%02u-%02u-%s", y, m, d, clock);
        return;
    }
    default:
        snprintf(out, cap, "?");
        return;
    }
}

bool hmi_md5_live(const char *program_md5, const char *unit_md5)
{
    if (!program_md5 || !unit_md5 || program_md5[0] == '\0' || unit_md5[0] == '\0')
        return false;
    size_t i = 0;
    for (; program_md5[i] != '\0' && unit_md5[i] != '\0'; i++)
    {
        if (tolower((unsigned char)program_md5[i]) != tolower((unsigned char)unit_md5[i]))
            return false;
    }
    return program_md5[i] == '\0' && unit_md5[i] == '\0';
}

size_t hmi_codec_to_wire(uint8_t type, uint16_t max_len, const void *buf, uint8_t *out)
{
    const uint8_t *in = buf;
    if (type == HMI_CODEC_STRING)
    {
        size_t n = 0;
        while (n < max_len && in[n] != '\0')
            n++;
        memcpy(out, in, n);
        return n;
    }
    const size_t width = hmi_codec_width(type);
    memcpy(out, in, width);
    return width;
}

bool hmi_codec_from_wire(uint8_t type, uint16_t max_len, const uint8_t *value, size_t len,
                         void *dest)
{
    uint8_t *out = dest;
    if (!out || (len > 0 && !value))
        return false;
    if (type == HMI_CODEC_STRING)
    {
        if (len > max_len || (len > 0 && memchr(value, 0, len) != NULL))
            return false;
        if (len > 0)
            memcpy(out, value, len);
        out[len] = '\0';
        return true;
    }
    const size_t width = hmi_codec_width(type);
    if (width == 0 || len != width)
        return false;
    switch (type)
    {
    case HMI_CODEC_BOOL:
        if (value[0] > 1u)
            return false;
        break;
    case HMI_CODEC_REAL:
    {
        const uint32_t u = (uint32_t)le_unsigned(value, 4);
        float f;
        memcpy(&f, &u, sizeof f);
        if (!isfinite(f))
            return false;
        break;
    }
    case HMI_CODEC_LREAL:
    {
        const uint64_t u = le_unsigned(value, 8);
        double d;
        memcpy(&d, &u, sizeof d);
        if (!isfinite(d))
            return false;
        break;
    }
    case HMI_CODEC_TOD:
    {
        const int64_t ns = le_signed(value, 8);
        if (ns < 0 || ns >= NS_PER_DAY)
            return false;
        break;
    }
    default:
        break;
    }
    memcpy(out, value, width);
    return true;
}

bool hmi_codec_within(uint8_t type, uint16_t max_len, const void *buf, double min, double max)
{
    const uint8_t *p = buf;
    double v;
    switch (type)
    {
    case HMI_CODEC_BOOL:
        v = p[0] ? 1.0 : 0.0;
        break;
    case HMI_CODEC_SINT:
    case HMI_CODEC_INT:
    case HMI_CODEC_DINT:
    case HMI_CODEC_LINT:
    case HMI_CODEC_TIME:
    case HMI_CODEC_LTIME:
    case HMI_CODEC_DATE:
    case HMI_CODEC_TOD:
    case HMI_CODEC_DT:
        v = (double)le_signed(p, hmi_codec_width(type));
        break;
    case HMI_CODEC_USINT:
    case HMI_CODEC_UINT:
    case HMI_CODEC_UDINT:
    case HMI_CODEC_ULINT:
    case HMI_CODEC_BYTE:
    case HMI_CODEC_WORD:
    case HMI_CODEC_DWORD:
    case HMI_CODEC_LWORD:
        v = (double)le_unsigned(p, hmi_codec_width(type));
        break;
    case HMI_CODEC_REAL:
    {
        const uint32_t u = (uint32_t)le_unsigned(p, 4);
        float f;
        memcpy(&f, &u, sizeof f);
        v = (double)f;
        break;
    }
    case HMI_CODEC_LREAL:
    {
        const uint64_t u = le_unsigned(p, 8);
        memcpy(&v, &u, sizeof v);
        break;
    }
    case HMI_CODEC_STRING:
    {
        size_t n = 0;
        while (n < max_len && p[n] != '\0')
            n++;
        v = (double)n;
        break;
    }
    default:
        return false;
    }
    return v >= min && v <= max;
}
