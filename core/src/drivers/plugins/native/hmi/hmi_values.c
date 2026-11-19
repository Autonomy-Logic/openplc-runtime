// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#include "hmi_values.h"

#include "hmi_wire.h"

#include <stdlib.h>
#include <string.h>

/** The viewer was sent a value of the tag. */
#define HMI_VALUES_KNOWN 0x01u
/** The tag changed since the last message was taken. */
#define HMI_VALUES_UNSENT 0x02u

bool hmi_buf_reserve(hmi_buf_t *b, size_t need)
{
    if (b->cap >= need)
        return true;
    size_t cap = b->cap ? b->cap : 256u;
    while (cap < need)
        cap *= 2u;
    uint8_t *p = realloc(b->data, cap);
    if (!p)
        return false;
    b->data = p;
    b->cap  = cap;
    return true;
}

bool hmi_buf_append(hmi_buf_t *b, const void *data, size_t len)
{
    if (!hmi_buf_reserve(b, b->len + len))
        return false;
    if (len > 0)
        memcpy(b->data + b->len, data, len);
    b->len += len;
    return true;
}

void hmi_buf_free(hmi_buf_t *b)
{
    free(b->data);
    b->data = NULL;
    b->len  = 0;
    b->cap  = 0;
}

bool hmi_values_layout_init(hmi_values_layout_t *layout, const uint16_t *max_bytes, int count)
{
    memset(layout, 0, sizeof *layout);
    const size_t n = count > 0 ? (size_t)count : 1u;
    layout->offset = calloc(n, sizeof *layout->offset);
    layout->max    = calloc(n, sizeof *layout->max);
    if (!layout->offset || !layout->max)
    {
        hmi_values_layout_free(layout);
        return false;
    }
    for (int i = 0; i < count; i++)
    {
        layout->offset[i] = layout->total;
        layout->max[i]    = max_bytes[i];
        layout->total += max_bytes[i];
    }
    layout->count = count > 0 ? count : 0;
    return true;
}

void hmi_values_layout_free(hmi_values_layout_t *layout)
{
    free(layout->offset);
    free(layout->max);
    memset(layout, 0, sizeof *layout);
}

bool hmi_values_init(hmi_values_t *v, const hmi_values_layout_t *layout)
{
    memset(v, 0, sizeof *v);
    const size_t n = layout->count > 0 ? (size_t)layout->count : 1u;
    v->data        = calloc(layout->total > 0 ? layout->total : 1u, 1);
    v->len         = calloc(n, sizeof *v->len);
    v->flags       = calloc(n, 1);
    if (!v->data || !v->len || !v->flags)
    {
        hmi_values_free(v);
        return false;
    }
    return true;
}

void hmi_values_free(hmi_values_t *v)
{
    free(v->data);
    free(v->len);
    free(v->flags);
    hmi_buf_free(&v->msg);
    memset(v, 0, sizeof *v);
}

void hmi_values_forget_all(hmi_values_t *v, const hmi_values_layout_t *layout)
{
    for (int i = 0; i < layout->count; i++)
        v->flags[i] = 0;
    v->msg.len = 0;
    v->pending = false;
}

void hmi_values_forget(hmi_values_t *v, const hmi_values_layout_t *layout, int tag)
{
    if (tag >= 0 && tag < layout->count)
        v->flags[tag] &= (uint8_t)~HMI_VALUES_KNOWN;
}

bool hmi_values_offer(hmi_values_t *v, const hmi_values_layout_t *layout, int tag,
                      const uint8_t *value, uint16_t len)
{
    if (tag < 0 || tag >= layout->count || len > layout->max[tag])
        return false;
    uint8_t *stored = v->data + layout->offset[tag];
    if ((v->flags[tag] & HMI_VALUES_KNOWN) && v->len[tag] == len &&
        (len == 0 || memcmp(stored, value, len) == 0))
        return false;
    if (len > 0)
        memcpy(stored, value, len);
    v->len[tag] = len;
    v->flags[tag] |= HMI_VALUES_KNOWN | HMI_VALUES_UNSENT;
    return true;
}

bool hmi_values_encode(hmi_values_t *v, const hmi_values_layout_t *layout, uint32_t viewer,
                       uint16_t page, uint32_t writes)
{
    size_t records = 0;
    size_t count   = 0;
    for (int i = 0; i < layout->count; i++)
    {
        if (v->flags[i] & HMI_VALUES_UNSENT)
        {
            records += HMI_WIRE_RECORD_HEADER + v->len[i];
            count++;
        }
    }
    if (count > UINT16_MAX)
        return false;
    uint8_t header[HMI_WIRE_VALUES_HEADER];
    if (!hmi_wire_values_header(viewer, page, writes, (uint16_t)count, records, header))
        return false;
    if (!hmi_buf_reserve(&v->msg, sizeof header + records))
        return false;
    memcpy(v->msg.data, header, sizeof header);
    size_t off = sizeof header;
    for (int i = 0; i < layout->count; i++)
    {
        if (!(v->flags[i] & HMI_VALUES_UNSENT))
            continue;
        hmi_wire_record_header((uint16_t)i, v->len[i], v->msg.data + off);
        off += HMI_WIRE_RECORD_HEADER;
        if (v->len[i] > 0)
            memcpy(v->msg.data + off, v->data + layout->offset[i], v->len[i]);
        off += v->len[i];
    }
    v->msg.len = off;
    v->pending = true;
    return true;
}

bool hmi_values_take(hmi_values_t *v, const hmi_values_layout_t *layout, hmi_buf_t *out)
{
    if (!v->pending)
        return false;
    hmi_buf_t tmp = *out;
    *out          = v->msg;
    v->msg        = tmp;
    v->msg.len    = 0;
    v->pending    = false;
    for (int i = 0; i < layout->count; i++)
        v->flags[i] &= (uint8_t)~HMI_VALUES_UNSENT;
    return true;
}
