// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file hmi_slot.h
 * @brief Latest-frame slot of one viewer.
 *
 * The frame thread offers each rendered frame; the slot keeps only the latest one, encoded as a
 * complete FRAME message, and skips a frame whose hash equals the previous one. The writer takes
 * the pending frame by swapping buffers, so no copy and no lock is held while it sends.
 * The slot itself is not locked: callers serialise access.
 */

#ifndef HMI_SLOT_H
#define HMI_SLOT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C"
{
#endif

    /** A growable byte buffer with one owner. */
    typedef struct
    {
        uint8_t *data;
        size_t len;
        size_t cap;
    } hmi_buf_t;

    /** Frees b's storage and empties it. */
    void hmi_buf_free(hmi_buf_t *b);

    typedef struct
    {
        hmi_buf_t msg; /**< the pending FRAME message when pending */
        bool pending;
        bool has_hash;
        uint64_t last_hash;
    } hmi_slot_t;

    typedef enum
    {
        HMI_SLOT_FAILED    = -1, /**< no memory, or the frame exceeds the message limit */
        HMI_SLOT_UNCHANGED = 0,  /**< same hash as the previous frame: nothing to send */
        HMI_SLOT_STORED    = 1
    } hmi_slot_result_t;

    /**
     * Offers a frame of viewer's display list, replacing any frame not yet taken.
     */
    hmi_slot_result_t hmi_slot_offer(hmi_slot_t *slot, uint32_t viewer, const uint8_t *dl,
                                     size_t dl_len, uint64_t hash);

    /** Makes the next offer store its frame whatever its hash. */
    void hmi_slot_invalidate(hmi_slot_t *slot);

    /**
     * Takes the pending frame: swaps the slot's message with *out, so *out holds the FRAME message
     * and the slot keeps the caller's old buffer for reuse.
     *
     * @return false when no frame is pending.
     */
    bool hmi_slot_take(hmi_slot_t *slot, hmi_buf_t *out);

    void hmi_slot_free(hmi_slot_t *slot);

#ifdef __cplusplus
}
#endif

#endif /* HMI_SLOT_H */
