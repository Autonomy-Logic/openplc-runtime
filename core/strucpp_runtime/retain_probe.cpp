// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Build-time capability probe for the strucpp retain API in the
// upload's headers. -fsyntax-only; only exit status matters. KEEP IN
// SYNC with every strucpp::retain / retain_layout_hash name the shim
// touches, else the probe passes for headers the shim will not build. */

#include <cstddef>
#include <cstdint>

#include "debug_table.hpp"
#include "iec_retain.hpp"

// Stand-ins for the shim's leaf accessors. Same signatures, so the probe
// exercises the real template argument deduction rather than just the name.
static uint16_t probe_read_leaf(uint8_t, uint16_t, uint8_t *) { return 0; }
static uint16_t probe_size_leaf(uint8_t, uint16_t) { return 0; }
static uint8_t probe_write_leaf(uint8_t, uint16_t, const uint8_t *, uint16_t) { return 0; }

size_t strucpp_retain_probe(uint8_t *out, size_t cap, const uint8_t *blob, size_t len)
{
    return strucpp::retain::blob_size(probe_size_leaf) +
           strucpp::retain::pack(out, cap, probe_read_leaf, probe_size_leaf) +
           static_cast<size_t>(
               strucpp::retain::unpack(blob, len, probe_write_leaf, probe_size_leaf)) +
           static_cast<size_t>(strucpp::debug::retain_layout_hash);
}
