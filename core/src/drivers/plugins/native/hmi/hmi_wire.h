// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file hmi_wire.h
 * @brief Binary protocol between the web server and the HMI plugin (docs/HMI.md, "Wire protocol").
 *
 * Every message is `u32 length (LE) | u8 type | payload`, `length` counting type and payload.
 * Integers are little endian. The decoder is strict: a wrong length, an unknown type or a field
 * out of range is malformed, and the caller closes the connection. It checks the shape of a
 * message only; whether a viewer may show a screen or write a tag is the host's decision.
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
/** Most bytes of a value in WRITE. */
#define HMI_WIRE_VALUE_MAX 1024u
/** Longest web server message (a WRITE with the longest value), prefix included. */
#define HMI_WIRE_MAX_INBOUND (HMI_WIRE_PREFIX + 1u + 8u + HMI_WIRE_VALUE_MAX)
/** Bytes of an OPENED or BUSY message. */
#define HMI_WIRE_VIEWER_MSG (HMI_WIRE_PREFIX + 1u + 4u)
/** Bytes of a STATUS message. */
#define HMI_WIRE_STATUS_MSG (HMI_WIRE_PREFIX + 1u + 1u)
/** Bytes of a REFUSED message. */
#define HMI_WIRE_REFUSED_MSG (HMI_WIRE_PREFIX + 1u + 7u)
/** Bytes of a VALUES message before its records: prefix, type, viewer, page, writes, count. */
#define HMI_WIRE_VALUES_HEADER (HMI_WIRE_PREFIX + 1u + 12u)
/** Bytes before each value in a VALUES record: tag and length. */
#define HMI_WIRE_RECORD_HEADER 4u
/** Highest bit index a BIT message may name (bit 63 of a 64-bit tag). */
#define HMI_WIRE_BIT_MAX 63u
/** Highest role code: 0 admin, 1 engineer, 2 operator, 3 viewer. */
#define HMI_WIRE_ROLE_MAX 3u

    typedef enum
    {
        HMI_MSG_OPEN    = 0x01,
        HMI_MSG_CLOSE   = 0x02,
        HMI_MSG_SHOW    = 0x03,
        HMI_MSG_WRITE   = 0x04,
        HMI_MSG_BIT     = 0x05,
        HMI_MSG_STATUS  = 0x81,
        HMI_MSG_OPENED  = 0x82,
        HMI_MSG_BUSY    = 0x83,
        HMI_MSG_VALUES  = 0x84,
        HMI_MSG_REFUSED = 0x85
    } hmi_msg_type_t;

    typedef enum
    {
        HMI_STATE_LIVE        = 0,
        HMI_STATE_OUT_OF_DATE = 1
    } hmi_state_t;

    /** What a REFUSED message refuses. */
    typedef enum
    {
        HMI_REFUSED_SHOW  = 0,
        HMI_REFUSED_WRITE = 1
    } hmi_refused_t;

    /** One decoded web server message; only the fields of its type are set. */
    typedef struct
    {
        uint8_t type;
        uint32_t viewer;
        uint8_t role;
        uint16_t page;
        uint16_t tag;
        uint8_t bit;       /**< BIT: the bit index */
        uint8_t bit_value; /**< BIT: 0 or 1 */
        uint16_t value_len;
        uint8_t value[HMI_WIRE_VALUE_MAX];
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

    /** Whether s[0..len) is well-formed UTF-8 without control characters: overlong forms,
     *  surrogates, code points past U+10FFFF, C0, DEL and C1 controls are refused. */
    bool hmi_wire_name_valid(const uint8_t *s, size_t len);

    /** Writes OPENED or BUSY into out (HMI_WIRE_VIEWER_MSG bytes); returns its length. */
    size_t hmi_wire_encode_viewer(uint8_t type, uint32_t viewer, uint8_t *out);

    /** Writes a STATUS message into out (HMI_WIRE_STATUS_MSG bytes); returns its length. */
    size_t hmi_wire_encode_status(uint8_t state, uint8_t *out);

    /** Writes a REFUSED message into out (HMI_WIRE_REFUSED_MSG bytes); returns its length. */
    size_t hmi_wire_encode_refused(uint32_t viewer, uint8_t what, uint16_t id, uint8_t *out);

    /**
     * Writes the header of a VALUES message whose records take records_len bytes into out
     * (HMI_WIRE_VALUES_HEADER bytes).
     *
     * @return false when the message would exceed HMI_WIRE_MAX_MESSAGE.
     */
    bool hmi_wire_values_header(uint32_t viewer, uint16_t page, uint32_t writes, uint16_t count,
                                size_t records_len, uint8_t *out);

    /** Writes a record header (tag, value length) into out (HMI_WIRE_RECORD_HEADER bytes). */
    void hmi_wire_record_header(uint16_t tag, uint16_t len, uint8_t *out);

#ifdef __cplusplus
}
#endif

#endif /* HMI_WIRE_H */
