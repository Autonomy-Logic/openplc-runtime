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

// Advances the strucpp runtime's scan-cycle clock. Called by the runtime
// once per cycle (the fastest task's housekeeping window invokes it via
// plc_run_io_cycle_post). CODESYS semantics: TIME() returns the same
// value for the duration of a cycle. The tick is supplied by the runtime
// because base_tick_ns is owned runtime-side now.
extern "C" void strucpp_advance_time(uint64_t tick_ns) {
    strucpp::__CURRENT_TIME_NS += static_cast<int64_t>(tick_ns);
}

// NOTE: the runtime no longer probes a "threaded ABI" capability symbol. It
// compiles every .so itself with -DSTRUCPP_THREADED, so the threaded
// process-image model is the only one; there is nothing to detect.
