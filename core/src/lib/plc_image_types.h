// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Cell types of the PLC image tables, shared with plugins through plugin_types.h.
// One type per located-variable size; names follow IEC 61131-3, widths come from <stdint.h>.

#ifndef PLC_IMAGE_TYPES_H
#define PLC_IMAGE_TYPES_H

#include <stdint.h>

typedef uint8_t IEC_BOOL;   /* %IX %QX %MX: one bit, held in a byte */
typedef uint8_t IEC_BYTE;   /* %IB %QB */
typedef uint16_t IEC_UINT;  /* %IW %QW %MW */
typedef uint32_t IEC_UDINT; /* %ID %QD %MD */
typedef uint64_t IEC_ULINT; /* %IL %QL %ML */

#endif /* PLC_IMAGE_TYPES_H */
