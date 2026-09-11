// runtime_v4_entry.cpp
//
// Static C-linkage shim compiled into every user .so. Identical for every
// project — no per-project codegen. Lives here in the runtime repo
// because the build (scripts/compile.sh) is the consumer; the editor's
// upload bundle does not ship this file.
//
// Responsibilities:
//
//   1. Instantiate strucpp::Configuration_CONFIG0 g_config — the actual
//      object the runtime walks. Must have external linkage so
//      generated_debug.cpp's compile-time address-of expressions resolve.
//   2. Export strucpp_get_config() — C-linkage entry the runtime dlsyms
//      to obtain a ConfigurationInstance* pointer.
//   3. Export strucpp_get_located_vars / strucpp_get_located_var_count
//      — re-expose strucpp::locatedVars[] (a per-project namespaced
//      symbol) under stable C linkage.
//   4. Activate STRUCPP_V4_DEBUG_EXPORTS_DEFINE — emits the C-linkage
//      strucpp_debug_* PDU helpers from debug_dispatch.hpp.
//   5. Export strucpp_advance_time() — bumps the per-.so
//      strucpp::__CURRENT_TIME_NS by the runtime-supplied tick. The
//      runtime owns the tick (computed from g_config); the shim just
//      provides the cross-DSO advance entry point.
//   6. Export strucpp_program_md5 — the project MD5, surfaced by FC 0x45
//      so the editor can verify it's debugging the matching source.

#define STRUCPP_V4_DEBUG_EXPORTS_DEFINE
#include "debug_dispatch.hpp"
#include "iec_located.hpp"
#include "iec_std_lib.hpp"   // ConfigurationInstance + __CURRENT_TIME_NS
#include "generated.hpp"

// Retain marshalling is conditional on the STruC++ that built this upload —
// see the block at the bottom of this file, and retain_probe.cpp for how the
// build decides. The include lives behind the same gate so a header set that
// predates the retain API is never even asked for it.
#ifdef STRUCPP_SHIM_HAS_RETAIN
#include "iec_retain.hpp"    // retain blob format + pack/unpack
#endif

#include <cstddef>
#include <cstdint>
#include <pthread.h>

// External linkage so generated_debug.cpp can reference &g_config.X.Y at
// compile time. Same constraint as the Arduino sketch's g_config.
strucpp::Configuration_CONFIG0 g_config;

extern "C" strucpp::ConfigurationInstance* strucpp_get_config(void) {
    return &g_config;
}

// strucpp::locatedVars / locatedVarsCount are top-level externs declared in
// iec_located.hpp and defined per-project by generated.cpp. The runtime
// (loaded once, sees many .so files) can't reach them by mangled name
// portably, so the shim re-exports them via C linkage. Same pattern as
// strucpp_get_config — the runtime walks via these accessors.

extern "C" const strucpp::LocatedVar *strucpp_get_located_vars(void) {
    return strucpp::locatedVars;
}

extern "C" uint32_t strucpp_get_located_var_count(void) {
    return strucpp::locatedVarsCount;
}

// Located-variable classifier for the unified external-write path.
//
// Given a debug (arr, elem) leaf, report whether it is a LOCATED variable
// and, if so, its image location (area / size / byte_index / bit_index).
// The runtime's runtime_external_write() uses this to route a write/force
// targeting a located var through the image journal (and the forced-slot
// bitmap) — copy_in would otherwise clobber a direct IECVar poke. Globals
// and program-internal leaves return 0 (applied straight to the IECVar via
// the debug-write journal).
//
// The match is by storage pointer: read_entry(arr,elem).ptr is the leaf's
// IECVar raw_ptr(), the same pointer recorded in locatedVars[].pointer — a
// pure pointer-identity check, no memory-layout assumption.
extern "C" int strucpp_debug_locate(uint8_t arr, uint16_t elem,
                                    uint8_t *area, uint8_t *size,
                                    uint16_t *byte_index, uint8_t *bit_index) {
    void *p = strucpp::debug::read_entry(arr, elem).ptr;
    if (p == nullptr) return 0;
    for (uint32_t i = 0; i < strucpp::locatedVarsCount; ++i) {
        if (strucpp::locatedVars[i].pointer == p) {
            const strucpp::LocatedVar &v = strucpp::locatedVars[i];
            if (area)       *area       = static_cast<uint8_t>(v.area);
            if (size)       *size       = static_cast<uint8_t>(v.size);
            if (byte_index) *byte_index = v.byte_index;
            if (bit_index)  *bit_index  = v.bit_index;
            return 1;
        }
    }
    return 0;
}

// Project MD5. Used by FC 0x45 to let the editor verify it's debugging
// the program it has the source for. The editor emits
// core/generated/defines.h next to generated.cpp during compile,
// defining PROGRAM_MD5 with the actual program hash. PROGRAM_MD5 is
// the same macro name the Arduino sketch's defines.h uses, keeping a
// single MD5 contract across targets.
//
// No fallback: a program loaded without defines.h is broken and must
// fail to compile (missing file) or link (undefined PROGRAM_MD5). The
// editor's v4 build path always emits defines.h.
#include "defines.h"

// Define as a non-const char array so:
//   1. The symbol has external linkage (in C++, namespace-scope
//      `const` gives INTERNAL linkage, which would hide the symbol
//      from dlsym → runtime sees NULL → FC 0x45 returns NOT_LOADED).
//   2. The symbol's address is the start of the string itself, not a
//      pointer variable. The runtime's symbols_init does
//      `*(void**)&ext_strucpp_program_md5 = dlsym(...)` and indexes
//      ext_strucpp_program_md5[i] directly — a `const char *foo = "..."`
//      definition would surface the raw pointer bytes as garbage.
//
// extern "C" block expresses C language linkage without the
// "extern initialized" g++ warning that the single-decl form triggers.
extern "C" {
char strucpp_program_md5[] = PROGRAM_MD5;
}

// Advances the strucpp runtime's scan-cycle clock by `tick_ns` on the CALLING
// thread. Retained for compatibility (and for any single-threaded host); the
// GCD master-tick dispatcher does NOT use it — under STRUCPP_THREADED
// __CURRENT_TIME_NS is thread_local, so a dispatcher-side increment would only
// bump the dispatcher's own (unused) copy. The dispatcher uses
// strucpp_set_current_time() on each worker instead.
extern "C" void strucpp_advance_time(uint64_t tick_ns) {
    strucpp::__CURRENT_TIME_NS += static_cast<int64_t>(tick_ns);
}

// Sets the IEC TIME() base for the CALLING thread. Under STRUCPP_THREADED
// __CURRENT_TIME_NS is thread_local (see iec_std_lib.hpp), so each task worker
// thread that calls this gets its own scan-stable time. The GCD master-tick
// dispatcher stamps each task's dispatch time and the worker calls this at the
// top of its scan, before run() — giving correct multi-rate IEC timing
// (TIME() constant within a scan, no cross-task interference, slow tasks keep
// their own snapshot while the master clock advances). Must be called ON the
// worker thread for the thread_local to land where the body reads it.
extern "C" void strucpp_set_current_time(int64_t ns) {
    strucpp::__CURRENT_TIME_NS = ns;
}

// NOTE: the runtime no longer probes a "threaded ABI" capability symbol. It
// compiles every .so itself with -DSTRUCPP_THREADED, so the threaded
// process-image model is the only one; there is nothing to detect.

// ---------------------------------------------------------------------------
// Retain marshalling.
//
// GATED ON THE UPLOAD'S STruC++ VERSION. `strucpp::retain` and
// `strucpp::debug::retain_layout_hash` arrived in STruC++ v0.6.5, and this file
// is compiled against the runtime headers the EDITOR shipped inside
// program.zip — which on every OpenPLC Editor released to date are older than
// that. Compiling this block unconditionally made an older editor's upload fail
// to build (runtime v4.2.0), so scripts/Makefile.strucpp probes the header set
// per upload and defines STRUCPP_SHIM_HAS_RETAIN only when the API is there.
//
// When it is not, these four exports are simply absent from the .so. That is a
// state the runtime already handles rather than a degraded one to apologise
// for: image_tables.cpp resolves all four as OPTIONAL symbols, and
// plc_retain_init() stands the store down and says so in the log. Retained
// variables then behave as NON_RETAIN, exactly as they did before these exports
// existed.
//
// Anything added here that touches strucpp::retain or retain_layout_hash MUST
// stay inside this #ifdef and be mirrored into retain_probe.cpp — a probe that
// checks less than the shim uses would pass for a header set that cannot
// actually build. Tests pin both halves
// (tests/pytest/compile/test_retain_capability_probe.py).
// ---------------------------------------------------------------------------
#ifdef STRUCPP_SHIM_HAS_RETAIN

// ---------------------------------------------------------------------------
// The WALK lives here, inside the .so, because that is where the debug tables
// and `handle_read` / `handle_write` are. The runtime is built once and loads
// many .so files, so it cannot reach `strucpp::retain` by mangled name — and
// re-implementing the blob format on its side would put two copies of a wire
// format in two repos, which is exactly the drift `iec_retain.hpp` exists to
// prevent.
//
// What the runtime DOES own is the write path, which is why unpack takes a
// callback instead of using `handle_write` directly: a retained variable may
// also be LOCATED (`VAR RETAIN x AT %MW10`), and poking such a leaf's IECVar
// is undone by the next copy-in from the process image. The runtime passes a
// thunk that routes through `runtime_external_write`, which knows to send a
// located leaf through the image journal. Reads need no such care — a read
// sees whatever the last copy-in left — so pack uses `handle_read` here.
// ---------------------------------------------------------------------------

static uint16_t retain_read_leaf(uint8_t arr, uint16_t elem, uint8_t* dest) {
    return strucpp::debug::handle_read(arr, elem, dest);
}

static uint16_t retain_size_leaf(uint8_t arr, uint16_t elem) {
    return strucpp::debug::handle_size(arr, elem);
}

/** Bytes a full blob occupies for this program; 0 when nothing is retained. */
extern "C" size_t strucpp_retain_blob_size(void) {
    return strucpp::retain::blob_size(retain_size_leaf);
}

/** Identity of the retain LAYOUT — reported so the runtime can log it. */
extern "C" uint32_t strucpp_retain_layout_hash(void) {
    return strucpp::debug::retain_layout_hash;
}

/** Serialise every retained leaf. Returns bytes written, 0 on failure. */
extern "C" size_t strucpp_retain_pack(uint8_t* out, size_t cap) {
    return strucpp::retain::pack(out, cap, retain_read_leaf, retain_size_leaf);
}

/**
 * Restore every retained leaf, writing through the runtime's own callback.
 *
 * Returns `strucpp::retain::LoadResult` as a byte. Anything but 0 (Ok) means
 * nothing was written and every variable keeps its declared initial value —
 * the correct outcome for a corrupt or stale store, since a machine starting
 * from its defaults is recoverable and one starting from plausible-looking
 * garbage is not.
 */
extern "C" uint8_t strucpp_retain_unpack(
    const uint8_t* blob,
    size_t len,
    uint8_t (*write_leaf)(uint8_t arr, uint16_t elem, const uint8_t* bytes, uint16_t n)) {
    return static_cast<uint8_t>(
        strucpp::retain::unpack(blob, len, write_leaf, retain_size_leaf));
}

#endif // STRUCPP_SHIM_HAS_RETAIN
