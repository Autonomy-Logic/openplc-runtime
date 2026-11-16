// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#include "hmi_input.h"

#include <string.h>

static hmi_input_event_t *at(hmi_input_queue_t *q, size_t i)
{
    return &q->ev[(q->head + i) % HMI_INPUT_QUEUE_CAP];
}

static bool is_move(const hmi_input_event_t *e)
{
    return e->kind == HMI_INPUT_POINTER && e->ptr_kind == HMI_INPUT_MOVE;
}

static bool is_press_or_release(const hmi_input_event_t *e)
{
    return e->kind == HMI_INPUT_POINTER && e->ptr_kind != HMI_INPUT_MOVE;
}

void hmi_input_reset(hmi_input_queue_t *q)
{
    q->head  = 0;
    q->count = 0;
}

static void append(hmi_input_queue_t *q, const hmi_input_event_t *ev)
{
    *at(q, q->count) = *ev;
    q->count++;
}

/** Drops every move followed by a later pointer event; returns the index of the move left, or
 *  count when none is. */
static size_t compact(hmi_input_queue_t *q)
{
    size_t last_ptr = q->count;
    for (size_t i = q->count; i > 0; i--)
    {
        if (at(q, i - 1)->kind == HMI_INPUT_POINTER)
        {
            last_ptr = i - 1;
            break;
        }
    }

    hmi_input_event_t kept[HMI_INPUT_QUEUE_CAP];
    size_t n         = 0;
    size_t move_left = HMI_INPUT_QUEUE_CAP;
    for (size_t i = 0; i < q->count; i++)
    {
        const hmi_input_event_t *e = at(q, i);
        if (is_move(e) && i != last_ptr)
            continue;
        if (is_move(e))
            move_left = n;
        kept[n++] = *e;
    }
    memcpy(q->ev, kept, n * sizeof kept[0]);
    q->head  = 0;
    q->count = n;
    return move_left == HMI_INPUT_QUEUE_CAP ? n : move_left;
}

bool hmi_input_push(hmi_input_queue_t *q, const hmi_input_event_t *ev)
{
    if (q->count < HMI_INPUT_QUEUE_CAP)
    {
        append(q, ev);
        return true;
    }

    const size_t move = compact(q);
    if (q->count < HMI_INPUT_QUEUE_CAP)
    {
        append(q, ev);
        return true;
    }
    if (!is_move(ev))
        return false;

    /* Full of presses, releases and keys plus at most one move, the last pointer event: the
     * incoming move supersedes it. With none left, the incoming move is coalesced away. */
    if (move < q->count)
    {
        for (size_t i = move; i + 1 < q->count; i++)
            *at(q, i) = *at(q, i + 1);
        q->count--;
        append(q, ev);
    }
    return true;
}

size_t hmi_input_take_frame(hmi_input_queue_t *q, hmi_input_event_t *out, size_t max)
{
    size_t n    = 0;
    size_t keys = 0;
    while (q->count > 0 && n < max)
    {
        hmi_input_event_t *e = at(q, 0);
        if (e->kind == HMI_INPUT_KEY)
        {
            if (keys == HMI_INPUT_KEYS_PER_FRAME)
                break;
            keys++;
        }
        out[n++] = *e;
        q->head  = (q->head + 1) % HMI_INPUT_QUEUE_CAP;
        q->count--;
        if (is_press_or_release(&out[n - 1]))
            break;
    }
    if (q->count == 0)
        q->head = 0;
    return n;
}
