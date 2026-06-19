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
