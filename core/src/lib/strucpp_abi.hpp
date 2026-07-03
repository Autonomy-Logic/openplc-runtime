// strucpp_abi.hpp — runtime-side mirror of the strucpp ABI we walk.
//
// The runtime executable is built ONCE; the .so it loads at runtime
// carries the actual strucpp runtime headers (shipped with the user
// program upload, used by scripts/compile.sh to build the .so). The
// runtime itself does NOT vendor strucpp headers — only this minimal
// set of layout-compatible mirror declarations.
//
// CONTRACT: every type below MUST match the layout strucpp's vendored
// headers expose. The .so's vtables, struct offsets, and enum values
// are all assumed identical. ABI consistency between the runtime and
// the strucpp version a user .so was built against is maintained as
// part of the development cycle — not enforced here. When strucpp's
// ABI version bumps in a breaking way, update this file.
//
// Mirrored from strucpp v0.4.5 (iec_located.hpp + iec_std_lib.hpp).

#ifndef OPENPLC_STRUCPP_ABI_HPP
#define OPENPLC_STRUCPP_ABI_HPP

#include <cstddef>
#include <cstdint>

namespace strucpp {

// ---------------------------------------------------------------------------
// LocatedVar (mirror of strucpp::LocatedVar, iec_located.hpp)
// ---------------------------------------------------------------------------

enum class LocatedArea : uint8_t {
    Input  = 0,  // %I
    Output = 1,  // %Q
    Memory = 2   // %M
};

enum class LocatedSize : uint8_t {
    Bit   = 0,
    Byte  = 1,
    Word  = 2,
    DWord = 3,
    LWord = 4
};

struct LocatedVar {
    LocatedArea area;
    LocatedSize size;
    uint16_t    byte_index;
    uint8_t     bit_index;
    uint8_t     _reserved[3];
    void       *pointer;
};

// ---------------------------------------------------------------------------
// ProgramBase (mirror of strucpp::ProgramBase, iec_std_lib.hpp)
//
// Polymorphic base. The runtime calls ->run() through a pointer; the
// vtable resolves into the .so's address space (where the actual
// derived class lives). Any extra virtual methods strucpp adds AFTER
// run() are fine — the runtime only calls run() so it doesn't need
// them in the mirror, but we keep them to preserve the vtable slot
// indices.
//
// strucpp v0.4.5 ProgramBase virtuals, in order:
//   0: ~ProgramBase()
//   1: run()
//   2: getRetainVars() const
//   3: getRetainCount() const
// ---------------------------------------------------------------------------

struct RetainVarInfo;  // opaque; we never dereference

struct ProgramBase {
    virtual ~ProgramBase() = default;
    virtual void run() = 0;
    virtual const RetainVarInfo *getRetainVars() const { return nullptr; }
    virtual size_t getRetainCount() const { return 0; }
    // RESERVED vtable slots 4,5 (formerly sync_in / sync_out). The shared-global
    // model moved from runtime-orchestrated per-task copy-in/out to per-global
    // mutexes owned by strucpp's GlobalVar<V>, so the runtime no longer calls
    // these and current strucpp no longer overrides them. They are KEPT as
    // no-op base slots — never renumber the vtable, or every program built
    // against an older ABI would mis-dispatch run()/located_range() on a newer
    // runtime (and vice versa).
    virtual void sync_in() {}
    virtual void sync_out() {}
    virtual void located_range(uint32_t *offset, uint32_t *count) const {
        *offset = 0;
        *count = 0;
    }
};

// ---------------------------------------------------------------------------
// TaskInstance (mirror of strucpp::TaskInstance, iec_std_lib.hpp)
// ---------------------------------------------------------------------------

struct TaskInstance {
    const char   *name;
    int64_t       interval_ns;
    int32_t       priority;
    ProgramBase **programs;
    size_t        program_count;
};

// ---------------------------------------------------------------------------
// ResourceInstance (mirror of strucpp::ResourceInstance, iec_std_lib.hpp)
// ---------------------------------------------------------------------------

struct ResourceInstance {
    const char   *name;
    const char   *processor;
    TaskInstance *tasks;
    size_t        task_count;
};

// ---------------------------------------------------------------------------
// ConfigurationInstance (mirror of strucpp::ConfigurationInstance,
// iec_std_lib.hpp)
//
// Polymorphic. The runtime obtains a ConfigurationInstance* via the
// shim's strucpp_get_config() and walks resources/tasks/programs by
// virtual dispatch. vtable slots, in order:
//   0: ~ConfigurationInstance()
//   1: get_name() const
//   2: get_resources()
//   3: get_resource_count() const
// ---------------------------------------------------------------------------

struct ConfigurationInstance {
    virtual ~ConfigurationInstance() = default;
    virtual const char       *get_name() const   = 0;
    virtual ResourceInstance *get_resources()    = 0;
    virtual size_t            get_resource_count() const = 0;
};

}  // namespace strucpp

#endif  // OPENPLC_STRUCPP_ABI_HPP
