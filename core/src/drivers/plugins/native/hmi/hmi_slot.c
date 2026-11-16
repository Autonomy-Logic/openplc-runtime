// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#include "hmi_slot.h"

#include "hmi_wire.h"

#include <stdlib.h>
#include <string.h>

void hmi_buf_free(hmi_buf_t *b)
{
    free(b->data);
    b->data = NULL;
    b->len  = 0;
    b->cap  = 0;
}

static bool buf_reserve(hmi_buf_t *b, size_t need)
{
    if (b->cap >= need)
        return true;
    size_t cap = b->cap ? b->cap : 4096u;
    while (cap < need)
        cap *= 2u;
    uint8_t *p = realloc(b->data, cap);
    if (!p)
        return false;
    b->data = p;
    b->cap  = cap;
    return true;
}

hmi_slot_result_t hmi_slot_offer(hmi_slot_t *slot, uint32_t viewer, const uint8_t *dl,
                                 size_t dl_len, uint64_t hash)
{
    if (slot->has_hash && slot->last_hash == hash)
        return HMI_SLOT_UNCHANGED;

    uint8_t header[HMI_WIRE_FRAME_HEADER];
    if (!hmi_wire_frame_header(viewer, dl_len, header))
        return HMI_SLOT_FAILED;
    if (!buf_reserve(&slot->msg, sizeof header + dl_len))
        return HMI_SLOT_FAILED;

    memcpy(slot->msg.data, header, sizeof header);
    if (dl_len > 0)
        memcpy(slot->msg.data + sizeof header, dl, dl_len);
    slot->msg.len   = sizeof header + dl_len;
    slot->pending   = true;
    slot->has_hash  = true;
    slot->last_hash = hash;
    return HMI_SLOT_STORED;
}

void hmi_slot_invalidate(hmi_slot_t *slot)
{
    slot->has_hash = false;
}

bool hmi_slot_take(hmi_slot_t *slot, hmi_buf_t *out)
{
    if (!slot->pending)
        return false;
    hmi_buf_t tmp = *out;
    *out          = slot->msg;
    slot->msg     = tmp;
    slot->msg.len = 0;
    slot->pending = false;
    return true;
}

void hmi_slot_free(hmi_slot_t *slot)
{
    hmi_buf_free(&slot->msg);
    slot->pending  = false;
    slot->has_hash = false;
}
