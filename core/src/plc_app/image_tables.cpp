// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Resolves the strucpp .so's exported symbols and binds image-table
// buffer pointers by walking strucpp::locatedVars[]. Plugins read/write
// through the buffer pointers under the image-tables mutex.

#include <cstdint>
#include <cstdlib>
#include <cstring>

#include <pthread.h>

extern "C" {
#include "include/iec_python.h"
#include "located_globals.h"
}

// Layout-compatible mirror of the strucpp ABI. The real headers ship
// with each user program upload; the runtime walks ConfigurationInstance
// and LocatedVar through these mirrors.
#include "../lib/strucpp_abi.hpp"

#include "image_tables.h"
#include "journal_buffer.h"
#include "plcapp_manager.h"
#include "utils/log.h"
#include "utils/utils.h"

// ---------------------------------------------------------------------------
// Image-table storage
// ---------------------------------------------------------------------------
IEC_BOOL *bool_input[BUFFER_SIZE][8];
IEC_BOOL *bool_output[BUFFER_SIZE][8];

IEC_BYTE *byte_input[BUFFER_SIZE];
IEC_BYTE *byte_output[BUFFER_SIZE];

IEC_UINT *int_input[BUFFER_SIZE];
IEC_UINT *int_output[BUFFER_SIZE];

IEC_UDINT *dint_input[BUFFER_SIZE];
IEC_UDINT *dint_output[BUFFER_SIZE];

IEC_ULINT *lint_input[BUFFER_SIZE];
IEC_ULINT *lint_output[BUFFER_SIZE];

IEC_UINT  *int_memory[BUFFER_SIZE];
IEC_UDINT *dint_memory[BUFFER_SIZE];
IEC_ULINT *lint_memory[BUFFER_SIZE];
IEC_BOOL  *bool_memory[BUFFER_SIZE][8];

namespace {
    using GetLocatedVarsFn  = const strucpp::LocatedVar *(*)(void);
    using GetLocatedCountFn = uint32_t (*)(void);
    // Located CONFIGURATION VAR_GLOBALs. Optional: older programs do
    // not emit the accessors (see image_tables_bind_located_vars).
    using GetLocatedGlobalsFn      = void *const *(*)(void);
    using GetLocatedGlobalCountFn  = uint32_t (*)(void);

    GetLocatedVarsFn  ext_strucpp_get_located_vars      = nullptr;
    GetLocatedCountFn ext_strucpp_get_located_var_count = nullptr;

    GetLocatedGlobalsFn     ext_strucpp_get_located_globals      = nullptr;
    GetLocatedGlobalCountFn ext_strucpp_get_located_global_count = nullptr;
}

// ---------------------------------------------------------------------------
// Resolved .so symbols
// ---------------------------------------------------------------------------
void (*ext_strucpp_advance_time)(uint64_t) = nullptr;
void (*ext_strucpp_set_current_time)(int64_t) = nullptr;

uint8_t  (*ext_strucpp_debug_array_count)(void)                          = nullptr;
uint16_t (*ext_strucpp_debug_elem_count) (uint8_t)                       = nullptr;
uint16_t (*ext_strucpp_debug_size)       (uint8_t, uint16_t)             = nullptr;
uint8_t  (*ext_strucpp_debug_set)        (uint8_t, uint16_t, bool,
                                          const uint8_t *, uint16_t)     = nullptr;
uint16_t (*ext_strucpp_debug_read)       (uint8_t, uint16_t, uint8_t *)  = nullptr;
size_t   (*ext_strucpp_retain_blob_size)  (void)                       = nullptr;
uint32_t (*ext_strucpp_retain_layout_hash)(void)                       = nullptr;
size_t   (*ext_strucpp_retain_pack)       (uint8_t *, size_t)          = nullptr;
uint8_t  (*ext_strucpp_retain_unpack)     (const uint8_t *, size_t,
                                           uint8_t (*)(uint8_t, uint16_t,
                                                       const uint8_t *, uint16_t)) = nullptr;
uint8_t  (*ext_strucpp_debug_write)      (uint8_t, uint16_t,
                                          const uint8_t *, uint16_t)     = nullptr;
int      (*ext_strucpp_debug_locate)     (uint8_t, uint16_t, uint8_t *,
                                          uint8_t *, uint16_t *, uint8_t *) = nullptr;

namespace {
    using GetConfigFn = strucpp::ConfigurationInstance *(*)(void);

    GetConfigFn ext_strucpp_get_config = nullptr;

    strucpp::ConfigurationInstance *g_config_ptr = nullptr;

    pthread_mutex_t g_image_tables_mutex;
    bool            g_locks_initialized = false;
    // Per-located-var value snapshot taken at copy-in, used by copy-out to
    // commit only changed outputs (dirty-diff). Sized to locatedVarsCount.
    uint64_t       *g_located_snapshot = nullptr;
    uint32_t        g_located_count    = 0;

    // Indices of VAR_GLOBAL ... AT entries in locatedVars[], built at
    // load by joining against the .so's locatedGlobals[] on POINTER
    // IDENTITY. Not covered by any program's located_range(); the
    // dispatcher copies them at the quiescent frame boundary.
    uint32_t       *g_located_globals_idx = nullptr;
    uint32_t        g_located_globals_n   = 0;

    int init_recursive_pi_mutex(pthread_mutex_t *m)
    {
        pthread_mutexattr_t attr;
        if (pthread_mutexattr_init(&attr) != 0) return -1;
        // Priority inheritance is POSIX-optional; MSYS2/Cygwin lacks
        // it. Windows has no real-time scheduling, so fall back to a
        // plain recursive mutex.
#if !defined(__CYGWIN__) && !defined(__MSYS__) && \
    defined(_POSIX_THREAD_PRIO_INHERIT) && _POSIX_THREAD_PRIO_INHERIT > 0
        pthread_mutexattr_setprotocol(&attr, PTHREAD_PRIO_INHERIT);
#endif
        pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE);
        int rc = pthread_mutex_init(m, &attr);
        pthread_mutexattr_destroy(&attr);
        return rc;
    }

    /* Optional lookups use the quiet variant so an older .so missing a
     * newer symbol does not spam dlsym errors. */
    void *resolve(PluginManager *pm, const char *name, bool required)
    {
        if (!required)
        {
            return plugin_manager_try_get_symbol(pm, name);
        }
        void *sym = plugin_manager_get_symbol(pm, name);
        if (!sym)
        {
            log_error("[strucpp] required symbol '%s' missing from .so", name);
        }
        return sym;
    }
}  // namespace

extern "C" pthread_mutex_t *image_tables_mutex(void)
{
    return &g_image_tables_mutex;
}

// Flush-on-lock read lock: takes the image mutex, drains the journal.
// Writes bypass this and go through journal_write_*. Mutex is recursive
// PI so a holder (e.g. plugin cycle hook) can re-enter.
extern "C" void image_lock(void)
{
    pthread_mutex_lock(&g_image_tables_mutex);
    journal_apply_and_clear();
}

extern "C" void image_unlock(void)
{
    pthread_mutex_unlock(&g_image_tables_mutex);
}

extern "C" void *strucpp_config_handle(void)
{
    return g_config_ptr;
}

// Walk the loaded configuration's tasks and store the GCD of declared
// intervals into base_tick_ns. Falls back to the 20 ms default if the
// configuration has no tasks (defensive — symbols_init returns success
// only after g_config_ptr is non-null).
static uint64_t gcd_u64(uint64_t a, uint64_t b)
{
    while (b)
    {
        uint64_t t = b;
        b = a % b;
        a = t;
    }
    return a;
}

static void compute_base_tick_from_config(strucpp::ConfigurationInstance *cfg)
{
    uint64_t gcd_ns = 0;
    auto *resources = cfg->get_resources();
    for (size_t r = 0; r < cfg->get_resource_count(); ++r)
    {
        for (size_t t = 0; t < resources[r].task_count; ++t)
        {
            uint64_t ivl = (uint64_t)resources[r].tasks[t].interval_ns;
            if (ivl == 0) ivl = 20000000ULL;
            gcd_ns = (gcd_ns == 0) ? ivl : gcd_u64(gcd_ns, ivl);
        }
    }
    if (gcd_ns != 0) base_tick_ns = gcd_ns;
}

extern "C" int symbols_init(PluginManager *pm)
{
    *(void **)&ext_strucpp_advance_time      = resolve(pm, "strucpp_advance_time",      true);
    *(void **)&ext_strucpp_set_current_time  = resolve(pm, "strucpp_set_current_time",  true);

    *(void **)&ext_strucpp_program_md5 = plugin_manager_get_symbol(pm, "strucpp_program_md5");

    *(void **)&ext_strucpp_get_config = resolve(pm, "strucpp_get_config", true);

    *(void **)&ext_strucpp_get_located_vars      = resolve(pm, "strucpp_get_located_vars",      true);
    *(void **)&ext_strucpp_get_located_var_count = resolve(pm, "strucpp_get_located_var_count", true);

    /* Config VAR_GLOBAL accessors are optional: older programs do not
     * emit them; the runtime then skips config-scope servicing but the
     * program (and POU-local located I/O) still runs. */
    *(void **)&ext_strucpp_get_located_globals =
        resolve(pm, "strucpp_get_located_globals", false);
    *(void **)&ext_strucpp_get_located_global_count =
        resolve(pm, "strucpp_get_located_global_count", false);

    *(void **)&ext_strucpp_debug_array_count = resolve(pm, "strucpp_debug_array_count", true);
    *(void **)&ext_strucpp_debug_elem_count  = resolve(pm, "strucpp_debug_elem_count",  true);
    *(void **)&ext_strucpp_debug_size        = resolve(pm, "strucpp_debug_size",        true);
    *(void **)&ext_strucpp_debug_set         = resolve(pm, "strucpp_debug_set",         true);
    *(void **)&ext_strucpp_debug_read        = resolve(pm, "strucpp_debug_read",        true);
    *(void **)&ext_strucpp_debug_write       = resolve(pm, "strucpp_debug_write",       true);

    /* Optional: a program built by an older STruC++ has no retain exports, and
     * the retain path then simply never runs. `required = false` so that is a
     * quiet degradation rather than a failed load. */
    *(void **)&ext_strucpp_retain_blob_size   = resolve(pm, "strucpp_retain_blob_size",   false);
    *(void **)&ext_strucpp_retain_layout_hash = resolve(pm, "strucpp_retain_layout_hash", false);
    *(void **)&ext_strucpp_retain_pack        = resolve(pm, "strucpp_retain_pack",        false);
    *(void **)&ext_strucpp_retain_unpack      = resolve(pm, "strucpp_retain_unpack",      false);
    /* Optional: present only on .so's built with strucpp_capabilities bit 2.
     * When NULL the debug-write drain routes every leaf as a global write. */
    *(void **)&ext_strucpp_debug_locate      = resolve(pm, "strucpp_debug_locate",      false);

    if (!ext_strucpp_advance_time || !ext_strucpp_set_current_time ||
        !ext_strucpp_get_config ||
        !ext_strucpp_get_located_vars || !ext_strucpp_get_located_var_count ||
        !ext_strucpp_debug_array_count || !ext_strucpp_debug_elem_count ||
        !ext_strucpp_debug_size || !ext_strucpp_debug_set ||
        !ext_strucpp_debug_read || !ext_strucpp_debug_write)
    {
        log_error("[strucpp] failed to resolve all required .so symbols");
        return -1;
    }

    // Only execution model: threaded process-image (every .so is built
    // with -DSTRUCPP_THREADED).
    log_info("[strucpp] execution model: threaded process-image");

    if (!g_locks_initialized)
    {
        if (init_recursive_pi_mutex(&g_image_tables_mutex) != 0)
        {
            log_error("[strucpp] failed to initialize runtime mutexes");
            return -1;
        }
        g_locks_initialized = true;
    }

    g_config_ptr = ext_strucpp_get_config();
    if (!g_config_ptr)
    {
        log_error("[strucpp] strucpp_get_config returned NULL");
        return -1;
    }

    /* Compute base_tick_ns from the loaded configuration. Replaces the
     * old config_init__ shim entry — runtime owns the tick now. */
    compute_base_tick_from_config(g_config_ptr);

    void (*ext_python_loader_set_loggers)(void (*)(const char *, ...),
                                          void (*)(const char *, ...));
    *(void **)&ext_python_loader_set_loggers =
        plugin_manager_get_symbol(pm, "python_loader_set_loggers");
    if (ext_python_loader_set_loggers)
    {
        ext_python_loader_set_loggers(log_info, log_error);
        log_info("[python] loader logging callbacks initialized");
    }

    log_info("[strucpp] symbols resolved (config=%p, debug=hier)",
             (void *)g_config_ptr);
    return 0;
}

// Adapter letting located_globals.c read locatedVars[i].pointer without knowing
// the strucpp LocatedVar layout (that mirror lives only in this TU).
static const void *located_pointer_at(const void *located_vars, uint32_t index)
{
    const strucpp::LocatedVar *lv = (const strucpp::LocatedVar *)located_vars;
    return lv[index].pointer;
}

void image_tables_bind_located_vars(void)
{
    if (!ext_strucpp_get_located_vars || !ext_strucpp_get_located_var_count)
    {
        log_warn("[image_tables] located-vars accessors unresolved — skip");
        return;
    }

    uint32_t lv_count = ext_strucpp_get_located_var_count();

    // Runtime owns the image; image<->program storage is copied per
    // task. Do NOT alias image slots to .so located-var members here.
    g_located_count = lv_count;
    free(g_located_snapshot);
    g_located_snapshot =
        (uint64_t *)calloc(lv_count ? lv_count : 1, sizeof(uint64_t));

    // Build the config-scope index list by pointer identity between
    // locatedVars[].pointer and locatedGlobals[]. No layout assumption.
    free(g_located_globals_idx);
    g_located_globals_idx = nullptr;
    g_located_globals_n   = 0;

    if (!ext_strucpp_get_located_globals || !ext_strucpp_get_located_global_count)
    {
        // Program exported before strucpp emitted locatedGlobals[]. We cannot
        // identify the config-scope entries, and we will not guess: service none
        // of them. The program still runs and POU-local located I/O is
        // unaffected — only located VAR_GLOBALs are inert.
        log_warn("[image_tables] this program does not export locatedGlobals[] "
                 "(built by an older editor/STruC++) — located CONFIGURATION "
                 "VAR_GLOBALs (%%IX/%%QX/%%MX/%%MW ... AT on a global) will NOT "
                 "be synced; re-export the project from a current OpenPLC Editor");
        log_info("[image_tables] %u located var(s) via copy-in/out "
                 "(%u program-local, 0 config-scope shared globals)",
                 lv_count, lv_count);
        return;
    }

    const strucpp::LocatedVar *lv = ext_strucpp_get_located_vars();
    void *const *lg      = ext_strucpp_get_located_globals();
    uint32_t     lg_count = ext_strucpp_get_located_global_count();

    if (lv_count)
    {
        g_located_globals_idx = (uint32_t *)calloc(lv_count, sizeof(uint32_t));
        if (!g_located_globals_idx)
        {
            log_error("[image_tables] out of memory building the config-located "
                      "index — located configuration globals will NOT be synced");
            return;
        }
    }

    // Independent cross-check witness: which indices a task actually claims via
    // located_range(). Used only to detect disagreement with the authoritative
    // classification, never to derive it.
    uint8_t *claimed = (uint8_t *)calloc(lv_count ? lv_count : 1, 1);
    if (claimed && g_config_ptr)
    {
        strucpp::ResourceInstance *res = g_config_ptr->get_resources();
        size_t rc = g_config_ptr->get_resource_count();
        for (size_t r = 0; r < rc; ++r)
        {
            for (size_t t = 0; t < res[r].task_count; ++t)
            {
                strucpp::TaskInstance &tk = res[r].tasks[t];
                for (size_t p = 0; p < tk.program_count; ++p)
                {
                    uint32_t off = 0, cnt = 0;
                    tk.programs[p]->located_range(&off, &cnt);
                    for (uint32_t i = off; i < off + cnt && i < lv_count; ++i)
                        claimed[i] = 1;
                }
            }
        }
    }

    uint32_t matched_globals = 0;
    g_located_globals_n = located_globals_join_ex(lv_count, lv,
                                                 located_pointer_at,
                                                 lg, lg_count,
                                                 g_located_globals_idx,
                                                 &matched_globals);

    // Every entry in locatedGlobals[] must correspond to some locatedVars[]
    // entry; a shortfall means the two arrays disagree, i.e. a codegen bug.
    if (matched_globals != lg_count)
    {
        log_error("[image_tables] only %u of %u locatedGlobals[] entries matched "
                  "a located variable — generated code is inconsistent",
                  matched_globals, lg_count);
    }

    // A config global that a task's located_range() also claims would be copied
    // twice, by the dispatcher and by that task.
    if (claimed)
    {
        for (uint32_t j = 0; j < g_located_globals_n; ++j)
        {
            uint32_t k = g_located_globals_idx[j];
            if (k < lv_count && claimed[k])
                log_error("[image_tables] locatedVars[%u] is a configuration "
                          "global but a task's located_range() also claims it — "
                          "double-serviced slot", k);
        }
    }
    free(claimed);

    log_info("[image_tables] %u located var(s) via copy-in/out "
             "(%u program-local, %u config-scope shared globals)",
             lv_count, lv_count - g_located_globals_n, g_located_globals_n);
}

namespace {

uint64_t threaded_member_read(const strucpp::LocatedVar &v)
{
    if (!v.pointer) return 0;
    switch (v.size)
    {
    case strucpp::LocatedSize::Bit:
    case strucpp::LocatedSize::Byte:  return *(const uint8_t *)v.pointer;
    case strucpp::LocatedSize::Word:  return *(const uint16_t *)v.pointer;
    case strucpp::LocatedSize::DWord: return *(const uint32_t *)v.pointer;
    case strucpp::LocatedSize::LWord: return *(const uint64_t *)v.pointer;
    }
    return 0;
}

void threaded_member_write(const strucpp::LocatedVar &v, uint64_t val)
{
    if (!v.pointer) return;
    switch (v.size)
    {
    case strucpp::LocatedSize::Bit:   *(uint8_t *)v.pointer  = (uint8_t)(val & 1); break;
    case strucpp::LocatedSize::Byte:  *(uint8_t *)v.pointer  = (uint8_t)val; break;
    case strucpp::LocatedSize::Word:  *(uint16_t *)v.pointer = (uint16_t)val; break;
    case strucpp::LocatedSize::DWord: *(uint32_t *)v.pointer = (uint32_t)val; break;
    case strucpp::LocatedSize::LWord: *(uint64_t *)v.pointer = val; break;
    }
}

uint64_t threaded_image_read(const strucpp::LocatedVar &v)
{
    uint16_t bi = v.byte_index;
    uint8_t  b  = v.bit_index;
    if (bi >= BUFFER_SIZE) return 0;
    switch (v.area)
    {
    case strucpp::LocatedArea::Input:
        switch (v.size)
        {
        case strucpp::LocatedSize::Bit:   return (b < 8 && bool_input[bi][b]) ? (*bool_input[bi][b] ? 1u : 0u) : 0u;
        case strucpp::LocatedSize::Byte:  return byte_input[bi] ? *byte_input[bi] : 0u;
        case strucpp::LocatedSize::Word:  return int_input[bi]  ? *int_input[bi]  : 0u;
        case strucpp::LocatedSize::DWord: return dint_input[bi] ? *dint_input[bi] : 0u;
        case strucpp::LocatedSize::LWord: return lint_input[bi] ? *lint_input[bi] : 0u;
        }
        break;
    case strucpp::LocatedArea::Output:
        switch (v.size)
        {
        case strucpp::LocatedSize::Bit:   return (b < 8 && bool_output[bi][b]) ? (*bool_output[bi][b] ? 1u : 0u) : 0u;
        case strucpp::LocatedSize::Byte:  return byte_output[bi] ? *byte_output[bi] : 0u;
        case strucpp::LocatedSize::Word:  return int_output[bi]  ? *int_output[bi]  : 0u;
        case strucpp::LocatedSize::DWord: return dint_output[bi] ? *dint_output[bi] : 0u;
        case strucpp::LocatedSize::LWord: return lint_output[bi] ? *lint_output[bi] : 0u;
        }
        break;
    case strucpp::LocatedArea::Memory:
        switch (v.size)
        {
        case strucpp::LocatedSize::Bit:   return (b < 8 && bool_memory[bi][b]) ? (*bool_memory[bi][b] ? 1u : 0u) : 0u;
        case strucpp::LocatedSize::Word:  return int_memory[bi]  ? *int_memory[bi]  : 0u;
        case strucpp::LocatedSize::DWord: return dint_memory[bi] ? *dint_memory[bi] : 0u;
        case strucpp::LocatedSize::LWord: return lint_memory[bi] ? *lint_memory[bi] : 0u;
        default: break;
        }
        break;
    }
    return 0;
}

// Copy a SINGLE located entry. Both the per-task range walk and the config-scope
// index walk go through these, so the two callers can never drift apart in how an
// entry is actually moved.
void copy_in_one(const strucpp::LocatedVar *lv, uint32_t k)
{
    uint64_t v = threaded_image_read(lv[k]);
    threaded_member_write(lv[k], v);
    if (g_located_snapshot) g_located_snapshot[k] = v;
}

void copy_out_one(const strucpp::LocatedVar *lv, uint32_t k)
{
    const strucpp::LocatedVar &v = lv[k];
    if (v.area == strucpp::LocatedArea::Input) return;  // %I is never committed
    uint64_t cur = threaded_member_read(v);
    if (g_located_snapshot && cur == g_located_snapshot[k]) return;  // unchanged
    if (g_located_snapshot) g_located_snapshot[k] = cur;
    uint16_t idx = v.byte_index;
    bool out = (v.area == strucpp::LocatedArea::Output);
    switch (v.size)
    {
    case strucpp::LocatedSize::Bit:
        journal_write_bool(out ? JOURNAL_BOOL_OUTPUT : JOURNAL_BOOL_MEMORY,
                           idx, v.bit_index, cur != 0);
        break;
    case strucpp::LocatedSize::Byte:
        journal_write_byte(JOURNAL_BYTE_OUTPUT, idx, (uint8_t)cur);
        break;
    case strucpp::LocatedSize::Word:
        journal_write_int(out ? JOURNAL_INT_OUTPUT : JOURNAL_INT_MEMORY,
                          idx, (uint16_t)cur);
        break;
    case strucpp::LocatedSize::DWord:
        journal_write_dint(out ? JOURNAL_DINT_OUTPUT : JOURNAL_DINT_MEMORY,
                           idx, (uint32_t)cur);
        break;
    case strucpp::LocatedSize::LWord:
        journal_write_lint(out ? JOURNAL_LINT_OUTPUT : JOURNAL_LINT_MEMORY,
                           idx, cur);
        break;
    }
}

}  // namespace

extern "C" void image_tables_threaded_copy_in(uint32_t offset, uint32_t count)
{
    if (!ext_strucpp_get_located_vars) return;
    const strucpp::LocatedVar *lv = ext_strucpp_get_located_vars();
    uint32_t end = offset + count;
    if (end > g_located_count) end = g_located_count;
    for (uint32_t k = offset; k < end; ++k) copy_in_one(lv, k);
}

extern "C" void image_tables_threaded_copy_out(uint32_t offset, uint32_t count)
{
    if (!ext_strucpp_get_located_vars) return;
    const strucpp::LocatedVar *lv = ext_strucpp_get_located_vars();
    uint32_t end = offset + count;
    if (end > g_located_count) end = g_located_count;
    for (uint32_t k = offset; k < end; ++k) copy_out_one(lv, k);
}

// Config-scope VAR_GLOBAL AT. Not covered by any program's
// located_range(), so copies are driven here at the dispatcher's
// quiescent frame boundary (safe without the per-global mutex).
extern "C" void image_tables_copy_config_globals_in(void)
{
    if (!g_located_globals_n || !g_located_globals_idx) return;
    if (!ext_strucpp_get_located_vars) return;
    const strucpp::LocatedVar *lv = ext_strucpp_get_located_vars();
    for (uint32_t j = 0; j < g_located_globals_n; ++j)
    {
        uint32_t k = g_located_globals_idx[j];
        if (k < g_located_count) copy_in_one(lv, k);
    }
}

extern "C" void image_tables_copy_config_globals_out(void)
{
    if (!g_located_globals_n || !g_located_globals_idx) return;
    if (!ext_strucpp_get_located_vars) return;
    const strucpp::LocatedVar *lv = ext_strucpp_get_located_vars();
    for (uint32_t j = 0; j < g_located_globals_n; ++j)
    {
        uint32_t k = g_located_globals_idx[j];
        if (k < g_located_count) copy_out_one(lv, k);
    }
}

// ---------------------------------------------------------------------------
// Backing storage for slots not covered by located variables.
// ---------------------------------------------------------------------------
static IEC_BOOL  temp_bool_input[BUFFER_SIZE][8];
static IEC_BOOL  temp_bool_output[BUFFER_SIZE][8];
static IEC_BYTE  temp_byte_input[BUFFER_SIZE];
static IEC_BYTE  temp_byte_output[BUFFER_SIZE];
static IEC_UINT  temp_int_input[BUFFER_SIZE];
static IEC_UINT  temp_int_output[BUFFER_SIZE];
static IEC_UDINT temp_dint_input[BUFFER_SIZE];
static IEC_UDINT temp_dint_output[BUFFER_SIZE];
static IEC_ULINT temp_lint_input[BUFFER_SIZE];
static IEC_ULINT temp_lint_output[BUFFER_SIZE];
static IEC_UINT  temp_int_memory[BUFFER_SIZE];
static IEC_UDINT temp_dint_memory[BUFFER_SIZE];
static IEC_ULINT temp_lint_memory[BUFFER_SIZE];
static IEC_BOOL  temp_bool_memory[BUFFER_SIZE][8];

void image_tables_fill_null_pointers(void)
{
    int filled = 0;
    for (int i = 0; i < BUFFER_SIZE; ++i)
    {
        for (int b = 0; b < 8; ++b)
        {
            if (!bool_input[i][b])  { temp_bool_input[i][b]  = 0; bool_input[i][b]  = &temp_bool_input[i][b];  ++filled; }
            if (!bool_output[i][b]) { temp_bool_output[i][b] = 0; bool_output[i][b] = &temp_bool_output[i][b]; ++filled; }
            if (!bool_memory[i][b]) { temp_bool_memory[i][b] = 0; bool_memory[i][b] = &temp_bool_memory[i][b]; ++filled; }
        }
        if (!byte_input[i])  { temp_byte_input[i]  = 0; byte_input[i]  = &temp_byte_input[i];  ++filled; }
        if (!byte_output[i]) { temp_byte_output[i] = 0; byte_output[i] = &temp_byte_output[i]; ++filled; }
        if (!int_input[i])   { temp_int_input[i]   = 0; int_input[i]   = &temp_int_input[i];   ++filled; }
        if (!int_output[i])  { temp_int_output[i]  = 0; int_output[i]  = &temp_int_output[i];  ++filled; }
        if (!dint_input[i])  { temp_dint_input[i]  = 0; dint_input[i]  = &temp_dint_input[i];  ++filled; }
        if (!dint_output[i]) { temp_dint_output[i] = 0; dint_output[i] = &temp_dint_output[i]; ++filled; }
        if (!lint_input[i])  { temp_lint_input[i]  = 0; lint_input[i]  = &temp_lint_input[i];  ++filled; }
        if (!lint_output[i]) { temp_lint_output[i] = 0; lint_output[i] = &temp_lint_output[i]; ++filled; }
        if (!int_memory[i])  { temp_int_memory[i]  = 0; int_memory[i]  = &temp_int_memory[i];  ++filled; }
        if (!dint_memory[i]) { temp_dint_memory[i] = 0; dint_memory[i] = &temp_dint_memory[i]; ++filled; }
        if (!lint_memory[i]) { temp_lint_memory[i] = 0; lint_memory[i] = &temp_lint_memory[i]; ++filled; }
    }
    log_info("[image_tables] filled %d NULL slots with backing buffers", filled);
}

void image_tables_zero_outputs(void)
{
    for (int i = 0; i < BUFFER_SIZE; ++i)
    {
        for (int b = 0; b < 8; ++b)
        {
            if (bool_output[i][b]) *bool_output[i][b] = 0;
        }
        if (byte_output[i]) *byte_output[i] = 0;
        if (int_output[i])  *int_output[i]  = 0;
        if (dint_output[i]) *dint_output[i] = 0;
        if (lint_output[i]) *lint_output[i] = 0;
    }
}

void image_tables_clear_null_pointers(void)
{
    // Threaded process-image state: free the dirty-diff snapshot. (The mutexes
    // persist across loads via g_locks_initialized.)
    free(g_located_snapshot);
    g_located_snapshot = nullptr;
    g_located_count    = 0;
    free(g_located_globals_idx);
    g_located_globals_idx = nullptr;
    g_located_globals_n   = 0;

    std::memset(bool_input,   0, sizeof(bool_input));
    std::memset(bool_output,  0, sizeof(bool_output));
    std::memset(byte_input,   0, sizeof(byte_input));
    std::memset(byte_output,  0, sizeof(byte_output));
    std::memset(int_input,    0, sizeof(int_input));
    std::memset(int_output,   0, sizeof(int_output));
    std::memset(dint_input,   0, sizeof(dint_input));
    std::memset(dint_output,  0, sizeof(dint_output));
    std::memset(lint_input,   0, sizeof(lint_input));
    std::memset(lint_output,  0, sizeof(lint_output));
    std::memset(int_memory,   0, sizeof(int_memory));
    std::memset(dint_memory,  0, sizeof(dint_memory));
    std::memset(lint_memory,  0, sizeof(lint_memory));
    std::memset(bool_memory,  0, sizeof(bool_memory));

    ext_strucpp_advance_time     = nullptr;
    ext_strucpp_set_current_time = nullptr;
    ext_strucpp_program_md5      = nullptr;
    ext_strucpp_get_config   = nullptr;
    ext_strucpp_debug_array_count = nullptr;
    ext_strucpp_debug_elem_count  = nullptr;
    ext_strucpp_debug_size        = nullptr;
    ext_strucpp_debug_set         = nullptr;
    ext_strucpp_debug_read        = nullptr;
    ext_strucpp_debug_write       = nullptr;
    ext_strucpp_debug_locate      = nullptr;
    ext_strucpp_get_located_vars      = nullptr;
    ext_strucpp_get_located_var_count = nullptr;
    g_config_ptr = nullptr;

    log_info("[image_tables] cleared all pointers");
}
