// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file hmi_wire.h
 * @brief Binary protocol between the web server and the HMI plugin (docs/HMI.md, "Wire protocol").
 *
 * Every message is `u32 length (LE) | u8 type | payload`, `length` counting type and payload.
 * Integers are little endian. The decoder is strict: a wrong length, an unknown type or a field
 * out of range is malformed, and the caller closes the connection.
 */

#ifndef HMI_WIRE_H
#define HMI_WIRE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C"
{
#endif

/** Most bytes of `length`: type and payload of any message. */
#define HMI_WIRE_MAX_MESSAGE (4u * 1024u * 1024u)
/** Bytes of the length prefix. */
#define HMI_WIRE_PREFIX 4u
/** Most bytes of an account name in OPEN. */
#define HMI_WIRE_NAME_MAX 64u
/** Longest web server message (OPEN with the longest name), prefix included. */
#define HMI_WIRE_MAX_INBOUND (HMI_WIRE_PREFIX + 1u + 12u + HMI_WIRE_NAME_MAX)
/** Bytes before the display list in a FRAME message: prefix, type and viewer. */
#define HMI_WIRE_FRAME_HEADER (HMI_WIRE_PREFIX + 1u + 4u)
/** Bytes of an OPENED, BUSY or FAULT message. */
#define HMI_WIRE_VIEWER_MSG (HMI_WIRE_PREFIX + 1u + 4u)
/** Bytes of a STATUS message. */
#define HMI_WIRE_STATUS_MSG (HMI_WIRE_PREFIX + 1u + 1u)

/** Viewport bounds, in CSS pixels and density x100 (density is only validated). */
#define HMI_WIRE_SIZE_MIN 1u
#define HMI_WIRE_SIZE_MAX 8192u
#define HMI_WIRE_DENSITY_MIN 25u
#define HMI_WIRE_DENSITY_MAX 800u
/** Highest key code: the last Unicode code point. */
#define HMI_WIRE_KEY_MAX 0x10FFFFu

    typedef enum
    {
        HMI_MSG_OPEN     = 0x01,
        HMI_MSG_CLOSE    = 0x02,
        HMI_MSG_VIEWPORT = 0x03,
        HMI_MSG_POINTER  = 0x04,
        HMI_MSG_KEY      = 0x05,
        HMI_MSG_FRAME    = 0x81,
        HMI_MSG_STATUS   = 0x82,
        HMI_MSG_OPENED   = 0x83,
        HMI_MSG_BUSY     = 0x84,
        HMI_MSG_FAULT    = 0x85
    } hmi_msg_type_t;

    typedef enum
    {
        HMI_STATE_LIVE        = 0,
        HMI_STATE_OUT_OF_DATE = 1
    } hmi_state_t;

    /** One decoded web server message; only the fields of its type are set. */
    typedef struct
    {
        uint8_t type;
        uint32_t viewer;
        uint8_t role;
        uint16_t width;
        uint16_t height;
        uint16_t density;
        uint8_t kind;
        int32_t x100;
        int32_t y100;
        uint32_t code;
        uint8_t name_len;
        char name[HMI_WIRE_NAME_MAX + 1];
        /** OPEN: whether the name is well-formed UTF-8 without control characters; when not, name
         * is empty. */
        bool name_valid;
    } hmi_wire_msg_t;

    typedef enum
    {
        HMI_WIRE_MALFORMED = -1,
        HMI_WIRE_NEED_MORE = 0,
        HMI_WIRE_DECODED   = 1
    } hmi_wire_result_t;

    /**
     * Decodes the first message in buf.
     *
     * @param buf      received bytes, starting at a message boundary
     * @param len      number of bytes in buf
     * @param out      the message, when one is decoded
     * @param consumed bytes of buf the message took, when one is decoded
     * @return HMI_WIRE_DECODED, HMI_WIRE_NEED_MORE when buf holds only part of a valid message, or
     *         HMI_WIRE_MALFORMED as soon as the bytes cannot start a valid message.
     */
    hmi_wire_result_t hmi_wire_decode(const uint8_t *buf, size_t len, hmi_wire_msg_t *out,
                                      size_t *consumed);

    /** Whether s[0..len) is well-formed UTF-8 without control characters. */
    bool hmi_wire_name_valid(const uint8_t *s, size_t len);

    /** Writes OPENED, BUSY or FAULT into out (HMI_WIRE_VIEWER_MSG bytes); returns its length. */
    size_t hmi_wire_encode_viewer(uint8_t type, uint32_t viewer, uint8_t *out);

    /** Writes a STATUS message into out (HMI_WIRE_STATUS_MSG bytes); returns its length. */
    size_t hmi_wire_encode_status(uint8_t state, uint8_t *out);

    /**
     * Writes the header of a FRAME carrying dl_len display list bytes into out
     * (HMI_WIRE_FRAME_HEADER bytes).
     *
     * @return false when the message would exceed HMI_WIRE_MAX_MESSAGE.
     */
    bool hmi_wire_frame_header(uint32_t viewer, size_t dl_len, uint8_t *out);

#ifdef __cplusplus
}
#endif

#endif /* HMI_WIRE_H */
