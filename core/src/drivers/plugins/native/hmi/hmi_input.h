// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file hmi_input.h
 * @brief Bounded per-viewer input queue of the HMI host.
 *
 * Events are kept in arrival order. When the queue is full, pointer moves are coalesced to make
 * room; a press, a release or a key is never dropped. hmi_input_take_frame hands the frame thread
 * what one core frame applies: events up to and including the first press or release.
 */

#ifndef HMI_INPUT_H
#define HMI_INPUT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C"
{
#endif

/** Events one viewer may have queued. */
#define HMI_INPUT_QUEUE_CAP 128u
/** Most keys handed to the core in one frame. */
#define HMI_INPUT_KEYS_PER_FRAME 16u

    typedef enum
    {
        HMI_INPUT_POINTER = 0,
        HMI_INPUT_KEY     = 1
    } hmi_input_kind_t;

    /** Pointer kinds, the values of hmi_ptr_kind in hmi_core.h. */
    typedef enum
    {
        HMI_INPUT_DOWN = 0,
        HMI_INPUT_MOVE = 1,
        HMI_INPUT_UP   = 2
    } hmi_input_ptr_t;

    typedef struct
    {
        uint8_t kind;     /**< ::hmi_input_kind_t */
        uint8_t ptr_kind; /**< ::hmi_input_ptr_t, pointer events only */
        int32_t x_q4;     /**< Q4 logical units, pointer events only */
        int32_t y_q4;
        uint32_t code; /**< key events only */
    } hmi_input_event_t;

    typedef struct
    {
        hmi_input_event_t ev[HMI_INPUT_QUEUE_CAP];
        size_t head;
        size_t count;
    } hmi_input_queue_t;

    void hmi_input_reset(hmi_input_queue_t *q);

    /**
     * Queues an event. A full queue first drops every move that a later pointer event supersedes,
     * then replaces the remaining move with an incoming one.
     *
     * @return false when a press, release or key does not fit even after coalescing (the viewer
     *         sends faster than frames apply it); true otherwise, including for a move that is
     *         coalesced away.
     */
    bool hmi_input_push(hmi_input_queue_t *q, const hmi_input_event_t *ev);

    /**
     * Removes the events one frame applies, in order, into out: up to and including the first press
     * or release, at most HMI_INPUT_KEYS_PER_FRAME keys and at most max events.
     *
     * @return the number of events written.
     */
    size_t hmi_input_take_frame(hmi_input_queue_t *q, hmi_input_event_t *out, size_t max);

#ifdef __cplusplus
}
#endif

#endif /* HMI_INPUT_H */
