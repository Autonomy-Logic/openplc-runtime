// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file hmi_codec.h
 * @brief Conversion between an HMI tag's buffer format and the STruC++ debug format.
 *
 * Buffer format (hmi_unit.h): the tag's IEC width, little endian; a STRING is max_len + 1 bytes,
 * NUL terminated. Debug format (debug_dispatch.hpp, debug_string_ops.hpp): the variable's bytes
 * in target byte order, BOOL as one byte; a STRING as `u8 length` followed by a fixed payload
 * window, the window being debug_size - 1 bytes.
 */

#ifndef HMI_CODEC_H
#define HMI_CODEC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C"
{
#endif

    /** Tag types, the values of hmi_tag_type in hmi_unit.h. */
    typedef enum
    {
        HMI_CODEC_BOOL = 1,
        HMI_CODEC_SINT,
        HMI_CODEC_INT,
        HMI_CODEC_DINT,
        HMI_CODEC_LINT,
        HMI_CODEC_USINT,
        HMI_CODEC_UINT,
        HMI_CODEC_UDINT,
        HMI_CODEC_ULINT,
        HMI_CODEC_BYTE,
        HMI_CODEC_WORD,
        HMI_CODEC_DWORD,
        HMI_CODEC_LWORD,
        HMI_CODEC_REAL,
        HMI_CODEC_LREAL,
        HMI_CODEC_STRING,
        HMI_CODEC_TIME,
        HMI_CODEC_LTIME,
        HMI_CODEC_DATE,
        HMI_CODEC_TOD,
        HMI_CODEC_DT
    } hmi_codec_type_t;

/** Largest debug value the host converts: a STRING's length byte and its payload window. */
#define HMI_CODEC_DEBUG_MAX 512u

    /** Bytes of a numeric or BOOL type; 0 for STRING and unknown types. */
    size_t hmi_codec_width(uint8_t type);

    /** Bytes of a tag in buffer format: its width, or max_len + 1 for a STRING; 0 when unknown. */
    size_t hmi_codec_buffer_size(uint8_t type, uint16_t max_len);

    /**
     * Converts a debug_read result into buffer format.
     *
     * @param wire     the bytes debug_read wrote
     * @param wire_len debug_size of the variable
     * @param dest     hmi_codec_buffer_size bytes
     * @return false when wire_len does not fit the type.
     */
    bool hmi_codec_from_debug(uint8_t type, uint16_t max_len, const uint8_t *wire, size_t wire_len,
                              void *dest);

    /**
     * Converts a value in buffer format into the debug_write format of a variable whose debug_size
     * is wire_len. A STRING longer than the debug window is cut to it (*truncated set).
     *
     * @param truncated optional
     * @return false when wire_len does not fit the type.
     */
    bool hmi_codec_to_debug(uint8_t type, uint16_t max_len, const void *src, uint8_t *wire,
                            size_t wire_len, bool *truncated);

    /**
     * Writes a value in buffer format as text for the write log: numbers in decimal, BOOL as
     * TRUE/FALSE, a STRING quoted with control characters, quotes and backslashes escaped.
     * Always NUL terminates out when cap > 0.
     */
    void hmi_codec_format(uint8_t type, uint16_t max_len, const void *buf, char *out, size_t cap);

    /** Whether the HMI is live: both MD5s non-empty and equal, ignoring hex case. */
    bool hmi_md5_live(const char *program_md5, const char *unit_md5);

#ifdef __cplusplus
}
#endif

#endif /* HMI_CODEC_H */
