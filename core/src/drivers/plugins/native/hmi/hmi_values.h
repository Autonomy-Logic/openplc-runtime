// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file hmi_values.h
 * @brief Per-viewer value tracking of the HMI host: what each viewer was sent, which tags changed
 *        since its last VALUES message was handed to the writer, and the VALUES message itself
 *        (docs/HMI.md, "Wire protocol"). Values are kept in wire format. Not locked: callers
 *        serialise access.
 */

#ifndef HMI_VALUES_H
#define HMI_VALUES_H

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

    /** Grows b to hold at least need bytes; false when memory is short. */
    bool hmi_buf_reserve(hmi_buf_t *b, size_t need);
    /** Appends len bytes; false when memory is short. */
    bool hmi_buf_append(hmi_buf_t *b, const void *data, size_t len);
    /** Frees b's storage and empties it. */
    void hmi_buf_free(hmi_buf_t *b);

    /** Where each tag's value sits in a viewer's value store, shared by every viewer. */
    typedef struct
    {
        size_t *offset; /**< per tag */
        uint16_t *max;  /**< most wire bytes per tag */
        size_t total;   /**< bytes of a store */
        int count;      /**< tags */
    } hmi_values_layout_t;

    /** Builds the layout from each tag's most wire bytes; false when memory is short. */
    bool hmi_values_layout_init(hmi_values_layout_t *layout, const uint16_t *max_bytes, int count);
    void hmi_values_layout_free(hmi_values_layout_t *layout);

    /** One viewer's values. */
    typedef struct
    {
        uint8_t *data;  /**< last value sent per tag, at its layout offset */
        uint16_t *len;  /**< its length */
        uint8_t *flags; /**< HMI_VALUES_* per tag */
        hmi_buf_t msg;  /**< the VALUES message not yet taken, when pending */
        bool pending;
    } hmi_values_t;

    bool hmi_values_init(hmi_values_t *v, const hmi_values_layout_t *layout);
    void hmi_values_free(hmi_values_t *v);

    /**
     * Forgets every value sent and drops the pending message and the changes not yet taken, so
     * each tag offered next is sent again: what a viewer showing another screen needs.
     */
    void hmi_values_forget_all(hmi_values_t *v, const hmi_values_layout_t *layout);
    /** Forgets the value sent of one tag, so it is sent again when next offered. */
    void hmi_values_forget(hmi_values_t *v, const hmi_values_layout_t *layout, int tag);

    /**
     * Offers a tag's current value (wire format, at most its layout max); returns true when it
     * differs from what the viewer was sent, or was never sent, and so goes in the next message.
     */
    bool hmi_values_offer(hmi_values_t *v, const hmi_values_layout_t *layout, int tag,
                          const uint8_t *value, uint16_t len);

    /**
     * Rebuilds the pending VALUES message from every tag offered since the last take; false when
     * memory is short or the message would exceed the protocol's limit.
     */
    bool hmi_values_encode(hmi_values_t *v, const hmi_values_layout_t *layout, uint32_t viewer,
                           uint16_t page, uint32_t writes);

    /**
     * Takes the pending message: swaps it with *out, so *out holds it and the store keeps the
     * caller's old buffer, and starts collecting changes anew. False when none is pending.
     */
    bool hmi_values_take(hmi_values_t *v, const hmi_values_layout_t *layout, hmi_buf_t *out);

#ifdef __cplusplus
}
#endif

#endif /* HMI_VALUES_H */
