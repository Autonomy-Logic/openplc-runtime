// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Runtime-side mirror of the strucpp ABI the loaded .so exposes.
// Every type below MUST match that layout: vtables, struct offsets,
// enum values. Mirrored from strucpp v0.4.5; update on an ABI bump.

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

struct RetainVarInfo;  // opaque; we never dereference

struct ProgramBase {
    virtual ~ProgramBase() = default;
    virtual void run() = 0;
    virtual const RetainVarInfo *getRetainVars() const { return nullptr; }
    virtual size_t getRetainCount() const { return 0; }
    // RESERVED vtable slots 4,5 (formerly sync_in/sync_out). Kept as
    // no-op base slots: renumbering the vtable would mis-dispatch
    // run()/located_range() across ABI versions in either direction.
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

struct ConfigurationInstance {
    virtual ~ConfigurationInstance() = default;
    virtual const char       *get_name() const   = 0;
    virtual ResourceInstance *get_resources()    = 0;
    virtual size_t            get_resource_count() const = 0;
};

}  // namespace strucpp

#endif  // OPENPLC_STRUCPP_ABI_HPP
