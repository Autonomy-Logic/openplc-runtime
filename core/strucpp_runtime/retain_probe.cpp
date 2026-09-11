// retain_probe.cpp
//
// Build-time capability probe for the STruC++ runtime headers that arrive
// INSIDE the program upload (core/generated/strucpp_runtime/include). Compiled
// with -fsyntax-only by scripts/Makefile.strucpp and never linked into
// anything; only its exit status matters.
//
// WHY THIS EXISTS
//
// runtime_v4_entry.cpp is the one runtime source file compiled against
// editor-supplied headers, so its dependencies are only as new as the STruC++
// the uploading editor was built with. The retain exports it added in v4.2.0
// need `strucpp::retain` and `strucpp::debug::retain_layout_hash`, both of
// which landed in STruC++ v0.6.5 -- newer than the STruC++ pinned by every
// OpenPLC Editor released to date. Compiled unconditionally, those exports turn
// any older editor's upload into a build failure, which is exactly what
// happened on the v4.2.0 release.
//
// The runtime already runs correctly without the exports: image_tables.cpp
// resolves all four as OPTIONAL symbols and plc_retain_init() stands the store
// down when they are absent. So the only thing missing was a way to ask, per
// upload, whether the headers in front of us can supply the API.
//
// WHY A COMPILED PROBE AND NOT A GREP
//
// The question is literally "does this expression compile against these
// headers", and the compiler is the only thing that answers it exactly. A grep
// for `namespace retain` looks equivalent and is not: every STruC++ back to
// v0.5.5 ships an iec_retain.hpp, and up to v0.6.4 it held an unrelated
// retain-variable descriptor struct -- so the include resolves, a name-based
// check can be fooled by a comment, and the real failure surfaces later at the
// point of use.
//
// KEEP IN SYNC: every strucpp::retain / retain_layout_hash name the shim
// touches must be touched here too, or the probe will pass for a header set the
// shim cannot actually compile against. A test pins that
// (tests/pytest/compile/test_retain_capability_probe.py).

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
