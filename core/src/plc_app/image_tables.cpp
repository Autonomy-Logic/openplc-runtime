// image_tables.cpp
//
// Resolves the strucpp .so's exported symbols (configuration accessor,
// locks setter, debug PDU helpers) and walks strucpp::locatedVars[] to
// bind image-table buffer pointers. Plugins read/write through the
// buffer pointers directly under the image-tables mutex.

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include <pthread.h>

extern "C" {
#include "include/iec_python.h"
#include "located_globals.h"
}

// Layout-compatible mirror of the strucpp ABI. The runtime executable
// is built once and walks ConfigurationInstance / LocatedVar through
// these mirrors; the actual strucpp runtime headers ship with the user
// program upload (under core/generated/strucpp_runtime/include/) and
// are consumed only by scripts/compile.sh when building the .so.
#include "../lib/strucpp_abi.hpp"

#include "image_tables.h"
#include "journal_buffer.h"
#include "plcapp_manager.h"
#include "utils/log.h"
#include "utils/utils.h"

// ---------------------------------------------------------------------------
// Image-table storage
// ---------------------------------------------------------------------------
image_tables_t g_image;

// How many elements each table currently holds. Zero means nothing is
// allocated and every table pointer is null, which is the state before the
// first program load and after the last unload. Every index into the image is
// bounded by this, so it lives beside the image rather than beside the
// allocator that sets it.
static uint32_t g_capacity = 0;

// The tables are heap pointers now, and these assertions are what got us here
// safely. In their previous form they pinned the inline-array shape, so the
// moment the types changed the build stopped and named the function to follow.
// They now pin the opposite invariant: nothing may quietly go back to inline
// storage, and no table may drift to a shape whose element size differs from
// the one image_tables_alloc() allocates it at.
//
// The hazard they exist for has not gone away. Indexing a pointer-to-array is
// syntactically identical to indexing an array, and `sizeof` on the two differs
// by four orders of magnitude, so the compiler cannot tell a correct use site
// from a wrong one. `sizeof` on these tables appears in no other function.
static_assert(sizeof(g_image.bool_input) == sizeof(IEC_BOOL *(*)[8]),
              "bool_input went back to inline storage: image_tables_alloc() and "
              "image_tables_zero_slots() both assume a heap pointer.");
static_assert(sizeof(g_image.byte_input) == sizeof(IEC_BYTE **),
              "byte_input went back to inline storage: see image_tables_alloc().");
static_assert(sizeof(g_image) == 14 * sizeof(void *),
              "the image struct gained, lost, or inlined a table -- "
              "image_tables_alloc() allocates exactly fourteen.");

// ---------------------------------------------------------------------------
// strucpp shim: per-project located-variable descriptor accessors
// (declared as C-linkage in the .so via runtime_v4_entry.cpp).
// ---------------------------------------------------------------------------
namespace {
    using GetLocatedVarsFn  = const strucpp::LocatedVar *(*)(void);
    using GetLocatedCountFn = uint32_t (*)(void);
    // Located CONFIGURATION VAR_GLOBALs. strucpp emits these accessors beside
    // the array in the generated configuration TU (not in our shim), so an older
    // program simply does not export them -- see the OPTIONAL note in
    // image_tables.h and the fallback in image_tables_bind_located_vars().
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

    // Indices of the locatedVars[] entries that are CONFIGURATION VAR_GLOBAL
    // ... AT. No program's located_range() covers them, so the per-task
    // copy-in/out never touches them; the dispatcher copies them at the
    // quiescent frame boundary instead (image_tables_copy_config_globals_in/out).
    //
    // Built once at program load by joining locatedVars[].pointer against the
    // .so's locatedGlobals[] on POINTER IDENTITY. strucpp states which storage
    // belongs to a configuration global; we never infer it.
    //
    // This deliberately replaces an earlier "[offset, count) tail not covered by
    // any program range" slice. That assumed strucpp emitted program-local
    // entries before config globals; the real order is the reverse, so the
    // computed count collapsed to zero as soon as any POU declared a located
    // variable and every located global silently stopped being serviced. Do not
    // reintroduce any rule based on an entry's position in the array.
    uint32_t       *g_located_globals_idx = nullptr;
    uint32_t        g_located_globals_n   = 0;

    int init_recursive_pi_mutex(pthread_mutex_t *m)
    {
        pthread_mutexattr_t attr;
        if (pthread_mutexattr_init(&attr) != 0) return -1;
        // Priority inheritance is a POSIX optional feature.  MSYS2/Cygwin
        // pthread on Windows doesn't ship it (PTHREAD_PRIO_INHERIT is
        // undefined, pthread_mutexattr_setprotocol is unavailable).
        // Windows has no real-time scheduling anyway, so the PI protocol
        // would be a no-op even if it linked — fall back to a plain
        // recursive mutex.
#if !defined(__CYGWIN__) && !defined(__MSYS__) && \
    defined(_POSIX_THREAD_PRIO_INHERIT) && _POSIX_THREAD_PRIO_INHERIT > 0
        pthread_mutexattr_setprotocol(&attr, PTHREAD_PRIO_INHERIT);
#endif
        pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE);
        int rc = pthread_mutex_init(m, &attr);
        pthread_mutexattr_destroy(&attr);
        return rc;
    }

    /* Optional lookups go through the QUIET variant. plugin_manager_get_symbol
     * reports every miss as "dlsym error", which is right for a symbol the
     * runtime cannot work without and wrong for one it can: a program built by
     * an editor older than a feature is correct, runs correctly, and used to
     * announce itself with a burst of errors describing a healthy device as a
     * broken one. Where absence is worth mentioning, the owning subsystem says
     * so in its own words (see the located-globals warning below). */
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
    /* Initialised on first use rather than only in symbols_init.
     *
     * symbols_init runs on the cycle thread, so on the first program load the
     * image-tables mutex was still a zero-filled pthread_mutex_t when the load
     * path locked it around the allocation, and when the boot path did not
     * lock it at all. Zero-filled happens to behave on glibc, but it is
     * neither recursive nor priority-inheriting there -- the two properties
     * this mutex is created for -- and it is undefined elsewhere.
     *
     * pthread_once, so the two callers cannot race to create it, and so it is
     * created exactly once for the life of the process rather than per load.
     * symbols_init's own guarded init is now redundant and harmless. */
    static pthread_once_t once = PTHREAD_ONCE_INIT;
    pthread_once(&once,
                 []
                 {
                     if (!g_locks_initialized)
                     {
                         init_recursive_pi_mutex(&g_image_tables_mutex);
                         g_locks_initialized = true;
                     }
                 });
    return &g_image_tables_mutex;
}

// Flush-on-lock read lock. This is the canonical entry for any consumer that
// needs a coherent view of the image to READ it (plugins reading %Q, the IEC
// task copy-in, etc.). It acquires the image mutex and then drains the journal
// so the holder sees every committed write.
//
// Usage mirrors the original BufferAccessor contract:
//   - Individual read:  image_lock(); v = <read>; image_unlock();
//   - Bulk read (preferred): image_lock(); <copy region to a local buffer>;
//                            image_unlock(); <slow work on the buffer>;
//     i.e. do the slow part (network, conversion) OUTSIDE the lock.
//
// Writes do NOT take this lock -- they go through journal_write_* (lock-free)
// and are applied by the drain here (or by the fastest task's drain).
//
// The mutex is recursive PI, so a consumer already holding it (e.g. the fastest
// task running plugin cycle hooks) can re-enter safely. The drain skips its
// bank flip when nothing is pending, so locking every cycle to read is cheap.
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

    /* Located CONFIGURATION VAR_GLOBALs — OPTIONAL. strucpp emits these
     * accessors beside locatedGlobals[] in the generated configuration TU, so a
     * program exported by an editor that predates them simply does not have the
     * symbols. When absent the runtime cannot tell which located entries are
     * config-scope, so it services none of them (the program still runs, and
     * POU-local located I/O is unaffected) and warns once at load. */
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

    // The runtime compiles every program's .so itself, always with
    // -DSTRUCPP_THREADED, so the only execution model is the threaded
    // process-image one: per-task located copy-in/out for program-local
    // `VAR AT`, dispatcher-boundary copy for config-scope located globals, and
    // per-global mutexes (strucpp GlobalVar<V>) for shared-global access. There
    // is no legacy shared-image path and nothing to detect.
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

// ---------------------------------------------------------------------------
// How big the image has to be (RTOP-284)
// ---------------------------------------------------------------------------

/* Local copy rather than shared with plc_retain_file_store.cpp, where the same
 * three lines live in an anonymous namespace: hoisting a four-line string trim
 * into a header shared between two config readers would couple them for no
 * gain, and the parsers are deliberately independent -- each mirrors the file
 * IT reads, key for key. */
static std::string trimmed(const std::string &s)
{
    const size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) return "";
    const size_t e = s.find_last_not_of(" \t\r\n");
    return s.substr(b, e - b + 1);
}

static const char *const kImageTableKeys[IMAGE_TABLE_COUNT] = {
    "bool_input",  "bool_output", "byte_input",  "byte_output",
    "int_input",   "int_output",  "dint_input",  "dint_output",
    "lint_input",  "lint_output", "int_memory",  "dint_memory",
    "lint_memory", "bool_memory",
};

/* The unit each table's ADDRESSES use, which is what image.conf carries.
 *
 * It is NOT always the unit the table is STORED in, and that gap is the whole
 * reason the unit is written down. bool_output is declared IEC_BOOL *[N][8],
 * so N counts bytes -- but %QX addresses bits, so the file says bits and the
 * conversion happens here, once, where the storage shape is known. The editor
 * emits the address's unit for every table and converts nothing.
 *
 * Parallel to kImageTableKeys, index for index. The contract test checks the
 * pairing against the editor and the webserver, so a table whose unit
 * disagrees across the four implementations fails CI. */
static const char *const kImageTableUnits[IMAGE_TABLE_COUNT] = {
    "bits",   "bits",   "bytes",  "bytes", "words",  "words",  "dwords",
    "dwords", "lwords", "lwords", "words", "dwords", "lwords", "bits",
};

static_assert(sizeof(kImageTableUnits) / sizeof(kImageTableUnits[0]) == IMAGE_TABLE_COUNT,
              "kImageTableUnits and image_table_id_t disagree on how many tables there are.");

/* The three BOOL tables, and only those, arrive in bits. */
static bool table_is_in_bits(int i)
{
    return std::strcmp(kImageTableUnits[i], "bits") == 0;
}

// A key missing here would make image_table_key() read past the array, and a
// spare one would go unnoticed. The count is the cheap half of keeping the enum
// and the strings in step; the ORDER is checked from the Python side, in
// tests/pytest/plugins/test_image_conf_contract.py, which is the only one of
// the three implementations of this file format that CI actually runs.
static_assert(sizeof(kImageTableKeys) / sizeof(kImageTableKeys[0]) == IMAGE_TABLE_COUNT,
              "kImageTableKeys and image_table_id_t disagree on how many tables there are.");

extern "C" const char *image_table_key(image_table_id_t id)
{
    return (id >= 0 && id < IMAGE_TABLE_COUNT) ? kImageTableKeys[id] : "";
}

/**
 * (area, size) -> the table that stores it, or IMAGE_TABLE_COUNT for a
 * combination this runtime has no storage for.
 *
 * There is exactly one such hole, and it is real rather than an oversight of
 * this function: `%MB` (Memory + Byte). image_tables.h declares byte_input and
 * byte_output but no byte_memory, so a program declaring `AT %MB4` names
 * storage that does not exist. A current editor refuses that before the build
 * (DOPE-615); an older one, or a hand-built .so, can still reach us, and the
 * caller says so once rather than sizing a table that is not there.
 */
static image_table_id_t table_for(strucpp::LocatedArea area, strucpp::LocatedSize size)
{
    switch (area)
    {
    case strucpp::LocatedArea::Input:
        switch (size)
        {
        case strucpp::LocatedSize::Bit:   return IMAGE_TABLE_BOOL_INPUT;
        case strucpp::LocatedSize::Byte:  return IMAGE_TABLE_BYTE_INPUT;
        case strucpp::LocatedSize::Word:  return IMAGE_TABLE_INT_INPUT;
        case strucpp::LocatedSize::DWord: return IMAGE_TABLE_DINT_INPUT;
        case strucpp::LocatedSize::LWord: return IMAGE_TABLE_LINT_INPUT;
        }
        break;
    case strucpp::LocatedArea::Output:
        switch (size)
        {
        case strucpp::LocatedSize::Bit:   return IMAGE_TABLE_BOOL_OUTPUT;
        case strucpp::LocatedSize::Byte:  return IMAGE_TABLE_BYTE_OUTPUT;
        case strucpp::LocatedSize::Word:  return IMAGE_TABLE_INT_OUTPUT;
        case strucpp::LocatedSize::DWord: return IMAGE_TABLE_DINT_OUTPUT;
        case strucpp::LocatedSize::LWord: return IMAGE_TABLE_LINT_OUTPUT;
        }
        break;
    case strucpp::LocatedArea::Memory:
        switch (size)
        {
        case strucpp::LocatedSize::Bit:   return IMAGE_TABLE_BOOL_MEMORY;
        case strucpp::LocatedSize::Word:  return IMAGE_TABLE_INT_MEMORY;
        case strucpp::LocatedSize::DWord: return IMAGE_TABLE_DINT_MEMORY;
        case strucpp::LocatedSize::LWord: return IMAGE_TABLE_LINT_MEMORY;
        case strucpp::LocatedSize::Byte:  break;  // %MB: no byte_memory table
        }
        break;
    }
    return IMAGE_TABLE_COUNT;
}

extern "C" void image_sizes_read_conf(const char *config_path, image_sizes_t *out)
{
    if (!out) return;
    std::memset(out, 0, sizeof(*out));

    // A missing file is not an error. It means nobody delivered sizes for this
    // program, and the caller falls back to the floor derived below -- which is
    // also what makes an older editor, or a device provisioned by hand, work.
    FILE *f = fopen(config_path, "r");
    if (!f) return;

    /* Built into a local and published only once the version checks out, so a
     * file this runtime cannot read leaves ZEROS rather than a half-applied
     * mixture of tables it understood and tables it did not. */
    image_sizes_t parsed;
    std::memset(&parsed, 0, sizeof(parsed));
    long version = 0;

    char line[256];
    while (fgets(line, sizeof(line), f))
    {
        std::string s = trimmed(line);
        if (s.empty() || s[0] == '#') continue;
        const size_t eq = s.find('=');
        if (eq == std::string::npos) continue;
        const std::string key = trimmed(s.substr(0, eq));
        const std::string val = trimmed(s.substr(eq + 1));

        if (key == "format_version")
        {
            char *vend = nullptr;
            version    = strtol(val.c_str(), &vend, 10);
            if (vend == val.c_str() || *vend != '\0')
                version = 0;
            continue;
        }

        for (int i = 0; i < IMAGE_TABLE_COUNT; ++i)
        {
            if (key != kImageTableKeys[i]) continue;

            errno        = 0;
            char *endp   = nullptr;
            const long v = strtol(val.c_str(), &endp, 10);

            /* The unit is not decoration: it is what stops a bit count being
             * allocated as an element count, which is a factor of eight with
             * no diagnostic on either side. A value whose unit is not the one
             * this table carries is refused rather than guessed at. */
            const std::string unit = (endp && endp != val.c_str()) ? trimmed(endp) : std::string();
            const bool unit_ok     = unit == kImageTableUnits[i];

            /* THE CEILING IS IN ELEMENTS, SO IT IS COMPARED AFTER CONVERTING.
             * IMAGE_MAX_ELEMENTS is the uint16 index the ABI addresses through
             * (CON03), a count of TABLE ELEMENTS. A BOOL table's file value is
             * in bits, and 65536 elements is 524288 bits -- comparing the raw
             * bit count against the element ceiling would refuse every legal
             * image above 8192 bytes. */
            const long max_in_file_unit =
                table_is_in_bits(i) ? (long)IMAGE_MAX_ELEMENTS * 8 : (long)IMAGE_MAX_ELEMENTS;

            /* Anything the runtime cannot honour reads as ZERO, which falls
             * through to the floor derived from the program. That is the safe
             * direction. Out of range, out of the uint16 the ABI addresses
             * through, unparsed, trailing junk, or carrying the wrong unit:
             * all of them mean the same thing here, which is "ignore me".
             *
             * The webserver refuses these at install, so reaching this branch
             * means a hand-edited device. */
            const bool numeric_ok =
                errno == 0 && endp != val.c_str() && v > 0 && v <= max_in_file_unit;
            const bool usable = numeric_ok && unit_ok;

            if (!usable && !val.empty() && !(v == 0 && unit_ok))
            {
                log_warn("[image_tables] image.conf: ignoring %s=%s, expected 1..%ld %s",
                         kImageTableKeys[i], val.c_str(), max_in_file_unit, kImageTableUnits[i]);
            }

            /* Bits to elements, once, here. Round UP: the slots of a partial
             * byte have to be addressable, and erring upward costs one byte
             * where erring downward loses up to seven addresses. */
            uint32_t elements = 0;
            if (usable)
            {
                elements = table_is_in_bits(i) ? (uint32_t)((v + 7) / 8) : (uint32_t)v;
            }
            parsed.elements[i] = elements;
            break;
        }
    }
    fclose(f);

    /* No version, or one this runtime does not know, refuses the WHOLE file.
     * Guessing would mean reading a future format by today's rules, which is
     * how a unit change becomes a silent factor of eight. Zeros here are not a
     * failure: the floor derived from the loaded program takes over, which is
     * the same path a device with no image.conf at all follows.
     *
     * There is no branch for version 1. It was written but never merged, so no
     * device has ever read this file in that form. */
    if (version != IMAGE_CONF_FORMAT_VERSION)
    {
        log_warn("[image_tables] image.conf: format_version %ld is not %d; ignoring the file "
                 "and sizing from the loaded program instead",
                 version, IMAGE_CONF_FORMAT_VERSION);
        return;
    }

    *out = parsed;
}

extern "C" void image_sizes_derive_floor(PluginManager *pm, image_sizes_t *out)
{
    if (!out) return;
    std::memset(out, 0, sizeof(*out));

    /* RESOLVED HERE, NOT READ FROM THE GLOBALS, and that is the whole point of
     * taking `pm`.
     *
     * The obvious version of this function read ext_strucpp_get_located_vars.
     * Those globals are populated by symbols_init, which runs on the cycle
     * thread (plc_state_manager.cpp) — created AFTER the load path sizes and
     * allocates the image. So they were always null here, the floor was always
     * a zero vector, and max(configured, derived) silently degraded to
     * "whatever image.conf said". With no image.conf that meant capacity 1 for
     * every program, every located address above index 0 rejected by the
     * bounds check, and no log to show for it — precisely the safety net this
     * function exists to be. Unload nulls them again, so the second load would
     * not have escaped it either.
     *
     * Resolving from the PluginManager makes the answer depend on the program
     * being dlopen'd, which the caller has just done, rather than on the order
     * two threads happen to run in. */
    GetLocatedVarsFn     get_vars  = nullptr;
    GetLocatedCountFn    get_count = nullptr;
    if (pm)
    {
        *(void **)&get_vars  = plugin_manager_get_symbol(pm, "strucpp_get_located_vars");
        *(void **)&get_count = plugin_manager_get_symbol(pm, "strucpp_get_located_var_count");
    }
    if (!get_vars) get_vars = ext_strucpp_get_located_vars;
    if (!get_count) get_count = ext_strucpp_get_located_var_count;

    if (!get_vars || !get_count)
    {
        // No program loaded, or one whose accessors are absent. Zeros, so the
        // caller sizes from the configuration alone -- and at boot, when there
        // is no program at all, from nothing.
        return;
    }

    const strucpp::LocatedVar *lv = get_vars();
    const uint32_t             n  = get_count();
    if (!lv) return;

    uint32_t unstorable = 0;

    for (uint32_t i = 0; i < n; ++i)
    {
        const image_table_id_t id = table_for(lv[i].area, lv[i].size);
        if (id == IMAGE_TABLE_COUNT)
        {
            ++unstorable;
            continue;
        }
        // byte_index IS the table index for every table, including the BOOL
        // ones -- those are indexed [byte][bit], and bit_index selects within
        // the byte. So the floor is uniformly the highest index plus one, and
        // no table needs a different unit here.
        const uint32_t needed = (uint32_t)lv[i].byte_index + 1u;
        if (needed > out->elements[id]) out->elements[id] = needed;
    }

    if (unstorable)
    {
        log_warn("[image_tables] %u located variable(s) address %%MB, which this "
                 "runtime has no table for - they will not be serviced",
                 unstorable);
    }
}

extern "C" void image_sizes_take_max(image_sizes_t *dst, const image_sizes_t *other)
{
    if (!dst || !other) return;
    for (int i = 0; i < IMAGE_TABLE_COUNT; ++i)
    {
        if (other->elements[i] > dst->elements[i]) dst->elements[i] = other->elements[i];
    }
}

void image_tables_bind_located_vars(void)
{
    if (!ext_strucpp_get_located_vars || !ext_strucpp_get_located_var_count)
    {
        log_warn("[image_tables] located-vars accessors unresolved — skip");
        return;
    }

    uint32_t lv_count = ext_strucpp_get_located_var_count();

    // The runtime OWNS the image (temp_* backing buffers, installed by
    // image_tables_fill_null_pointers) and copies image<->program storage per
    // task. So we deliberately do NOT alias image slots to the .so located-var
    // members here; we only size the dirty-diff snapshot buffer.
    g_located_count = lv_count;
    free(g_located_snapshot);
    g_located_snapshot =
        (uint64_t *)calloc(lv_count ? lv_count : 1, sizeof(uint64_t));

    // Build the config-scope index list. Authority is the .so's
    // locatedGlobals[]: strucpp records there the canonical storage pointer of
    // every located CONFIGURATION VAR_GLOBAL — the same raw_ptr() value it writes
    // into locatedVars[].pointer — so an entry is config-scope exactly when its
    // pointer appears in that array. Pointer identity, no layout or ordering
    // assumption, and distinct objects have distinct addresses so there are no
    // false positives.
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

// ---------------------------------------------------------------------------
// Threaded process-image copy-in / copy-out.
//
// In threaded mode the image (bool_input[] ... lint_memory[], backed by the
// temp_* buffers) is runtime-owned and decoupled from the program's located
// storage (the .so IECVar members, reachable via locatedVars[i].pointer). At a
// task boundary the runtime copies the task's located slice IN (image ->
// member) before run(), and commits CHANGED outputs OUT (member -> journal ->
// image) after. The journal makes the commit race-free vs other tasks/plugins;
// the snapshot makes it dirty (a task that only reads a shared output never
// clobbers a concurrent writer).
// ---------------------------------------------------------------------------
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
    if (bi >= g_capacity) return 0;
    switch (v.area)
    {
    case strucpp::LocatedArea::Input:
        switch (v.size)
        {
        case strucpp::LocatedSize::Bit:   return (b < 8 && g_image.bool_input[bi][b]) ? (*g_image.bool_input[bi][b] ? 1u : 0u) : 0u;
        case strucpp::LocatedSize::Byte:  return g_image.byte_input[bi] ? *g_image.byte_input[bi] : 0u;
        case strucpp::LocatedSize::Word:  return g_image.int_input[bi]  ? *g_image.int_input[bi]  : 0u;
        case strucpp::LocatedSize::DWord: return g_image.dint_input[bi] ? *g_image.dint_input[bi] : 0u;
        case strucpp::LocatedSize::LWord: return g_image.lint_input[bi] ? *g_image.lint_input[bi] : 0u;
        }
        break;
    case strucpp::LocatedArea::Output:
        switch (v.size)
        {
        case strucpp::LocatedSize::Bit:   return (b < 8 && g_image.bool_output[bi][b]) ? (*g_image.bool_output[bi][b] ? 1u : 0u) : 0u;
        case strucpp::LocatedSize::Byte:  return g_image.byte_output[bi] ? *g_image.byte_output[bi] : 0u;
        case strucpp::LocatedSize::Word:  return g_image.int_output[bi]  ? *g_image.int_output[bi]  : 0u;
        case strucpp::LocatedSize::DWord: return g_image.dint_output[bi] ? *g_image.dint_output[bi] : 0u;
        case strucpp::LocatedSize::LWord: return g_image.lint_output[bi] ? *g_image.lint_output[bi] : 0u;
        }
        break;
    case strucpp::LocatedArea::Memory:
        switch (v.size)
        {
        case strucpp::LocatedSize::Bit:   return (b < 8 && g_image.bool_memory[bi][b]) ? (*g_image.bool_memory[bi][b] ? 1u : 0u) : 0u;
        case strucpp::LocatedSize::Word:  return g_image.int_memory[bi]  ? *g_image.int_memory[bi]  : 0u;
        case strucpp::LocatedSize::DWord: return g_image.dint_memory[bi] ? *g_image.dint_memory[bi] : 0u;
        case strucpp::LocatedSize::LWord: return g_image.lint_memory[bi] ? *g_image.lint_memory[bi] : 0u;
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

// Config-scope located globals (CONFIGURATION VAR_GLOBAL ... AT). No program's
// located_range() covers these, so the per-task copy-in/out never reaches them.
// The dispatcher calls these at the quiescent frame boundary
// (g_tasks_running == 0) so there is no concurrent task access to the shared
// canonical storage — the copy is safe WITHOUT the per-global mutex (quiescence
// is the synchronization). copy_in primes the canonical globals from the image
// (inputs get fresh hardware values); copy_out journals changed output/memory
// globals back to the image (drained by the dispatcher).
//
// The entries are an explicit index list built at load by
// image_tables_bind_located_vars() from the .so's locatedGlobals[]; they are NOT
// a contiguous slice, so these walk the list rather than calling the range-based
// helpers above. No-ops when the program has no located globals, or when it
// predates locatedGlobals[] (a warning is logged once at load).
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
// Backing storage for image slots no located variable claims. Heap, and the
// same length as the tables that point into it -- these were fourteen more
// [BUFFER_SIZE] statics, and leaving them fixed while the tables grew would put
// fill_null_pointers() to work handing out addresses past their end.
static IEC_BOOL (*temp_bool_input)[8]  = nullptr;
static IEC_BOOL (*temp_bool_output)[8] = nullptr;
static IEC_BOOL (*temp_bool_memory)[8] = nullptr;
static IEC_BYTE  *temp_byte_input      = nullptr;
static IEC_BYTE  *temp_byte_output     = nullptr;
static IEC_UINT  *temp_int_input       = nullptr;
static IEC_UINT  *temp_int_output      = nullptr;
static IEC_UDINT *temp_dint_input      = nullptr;
static IEC_UDINT *temp_dint_output     = nullptr;
static IEC_ULINT *temp_lint_input      = nullptr;
static IEC_ULINT *temp_lint_output     = nullptr;
static IEC_UINT  *temp_int_memory      = nullptr;
static IEC_UDINT *temp_dint_memory     = nullptr;
static IEC_ULINT *temp_lint_memory     = nullptr;

/* The smallest image that is not no image at all.
 *
 * Not a tuning knob and not a guess: it is the least count that leaves every
 * base pointer non-null and buffer_size non-zero, which is what plugins are
 * promised even at boot, before any program exists. A plugin bounds-checking
 * against it accepts index 0 and nothing else, which is the correct answer for
 * an image with nothing in it. */
static const uint32_t IMAGE_MIN_ELEMENTS = 1;

extern "C" uint32_t image_tables_capacity(void) { return g_capacity; }

extern "C" uint32_t image_sizes_largest(const image_sizes_t *sizes)
{
    if (!sizes) return 0;
    uint32_t largest = 0;
    for (int i = 0; i < IMAGE_TABLE_COUNT; ++i)
    {
        if (sizes->elements[i] > largest) largest = sizes->elements[i];
    }
    return largest;
}

extern "C" void image_tables_free(void)
{
    free(g_image.bool_input);
    free(g_image.bool_output);
    free(g_image.bool_memory);
    free(g_image.byte_input);
    free(g_image.byte_output);
    free(g_image.int_input);
    free(g_image.int_output);
    free(g_image.dint_input);
    free(g_image.dint_output);
    free(g_image.lint_input);
    free(g_image.lint_output);
    free(g_image.int_memory);
    free(g_image.dint_memory);
    free(g_image.lint_memory);

    free(temp_bool_input);
    free(temp_bool_output);
    free(temp_bool_memory);
    free(temp_byte_input);
    free(temp_byte_output);
    free(temp_int_input);
    free(temp_int_output);
    free(temp_dint_input);
    free(temp_dint_output);
    free(temp_lint_input);
    free(temp_lint_output);
    free(temp_int_memory);
    free(temp_dint_memory);
    free(temp_lint_memory);

    // Null every pointer, not just free it. A dangling table would index
    // exactly as a live one does, and the next fill_null_pointers() would read
    // freed memory to decide whether a slot needs backing.
    std::memset(&g_image, 0, sizeof(g_image));
    temp_bool_input  = nullptr;
    temp_bool_output = nullptr;
    temp_bool_memory = nullptr;
    temp_byte_input  = nullptr;
    temp_byte_output = nullptr;
    temp_int_input   = nullptr;
    temp_int_output  = nullptr;
    temp_dint_input  = nullptr;
    temp_dint_output = nullptr;
    temp_lint_input  = nullptr;
    temp_lint_output = nullptr;
    temp_int_memory  = nullptr;
    temp_dint_memory = nullptr;
    temp_lint_memory = nullptr;

    g_capacity = 0;
}

extern "C" bool image_tables_alloc(uint32_t elements)
{
    if (elements < IMAGE_MIN_ELEMENTS) elements = IMAGE_MIN_ELEMENTS;

    /* BUILT INTO LOCALS AND PUBLISHED ONLY ON SUCCESS.
     *
     * This used to call image_tables_free() first and allocate into g_image
     * directly, which made the all-or-nothing promise in the header only half
     * true: it covered the new image, not the one it had just destroyed. A
     * re-allocation that failed left capacity 0 and fourteen null tables while
     * every plugin still held the base pointers it cached by value at init(),
     * so the failure surfaced inside a plugin rather than here.
     *
     * Now nothing observable changes until all twenty-eight allocations have
     * succeeded. A failure frees the locals and leaves the running image
     * exactly as it was, which is what lets the caller log and stop with the
     * device still in a describable state. */
    image_tables_t next;
    std::memset(&next, 0, sizeof(next));

    IEC_BOOL(*t_bool_input)[8]  = nullptr;
    IEC_BOOL(*t_bool_output)[8] = nullptr;
    IEC_BOOL(*t_bool_memory)[8] = nullptr;
    IEC_BYTE *t_byte_input      = nullptr;
    IEC_BYTE *t_byte_output     = nullptr;
    IEC_UINT *t_int_input       = nullptr;
    IEC_UINT *t_int_output      = nullptr;
    IEC_UDINT *t_dint_input     = nullptr;
    IEC_UDINT *t_dint_output    = nullptr;
    IEC_ULINT *t_lint_input     = nullptr;
    IEC_ULINT *t_lint_output    = nullptr;
    IEC_UINT *t_int_memory      = nullptr;
    IEC_UDINT *t_dint_memory    = nullptr;
    IEC_ULINT *t_lint_memory    = nullptr;

    next.bool_input  = (IEC_BOOL * (*)[8]) calloc(elements, sizeof(IEC_BOOL *[8]));
    next.bool_output = (IEC_BOOL * (*)[8]) calloc(elements, sizeof(IEC_BOOL *[8]));
    next.bool_memory = (IEC_BOOL * (*)[8]) calloc(elements, sizeof(IEC_BOOL *[8]));
    next.byte_input  = (IEC_BYTE **)calloc(elements, sizeof(IEC_BYTE *));
    next.byte_output = (IEC_BYTE **)calloc(elements, sizeof(IEC_BYTE *));
    next.int_input   = (IEC_UINT **)calloc(elements, sizeof(IEC_UINT *));
    next.int_output  = (IEC_UINT **)calloc(elements, sizeof(IEC_UINT *));
    next.dint_input  = (IEC_UDINT **)calloc(elements, sizeof(IEC_UDINT *));
    next.dint_output = (IEC_UDINT **)calloc(elements, sizeof(IEC_UDINT *));
    next.lint_input  = (IEC_ULINT **)calloc(elements, sizeof(IEC_ULINT *));
    next.lint_output = (IEC_ULINT **)calloc(elements, sizeof(IEC_ULINT *));
    next.int_memory  = (IEC_UINT **)calloc(elements, sizeof(IEC_UINT *));
    next.dint_memory = (IEC_UDINT **)calloc(elements, sizeof(IEC_UDINT *));
    next.lint_memory = (IEC_ULINT **)calloc(elements, sizeof(IEC_ULINT *));

    t_bool_input  = (IEC_BOOL(*)[8])calloc(elements, sizeof(IEC_BOOL[8]));
    t_bool_output = (IEC_BOOL(*)[8])calloc(elements, sizeof(IEC_BOOL[8]));
    t_bool_memory = (IEC_BOOL(*)[8])calloc(elements, sizeof(IEC_BOOL[8]));
    t_byte_input  = (IEC_BYTE *)calloc(elements, sizeof(IEC_BYTE));
    t_byte_output = (IEC_BYTE *)calloc(elements, sizeof(IEC_BYTE));
    t_int_input   = (IEC_UINT *)calloc(elements, sizeof(IEC_UINT));
    t_int_output  = (IEC_UINT *)calloc(elements, sizeof(IEC_UINT));
    t_dint_input  = (IEC_UDINT *)calloc(elements, sizeof(IEC_UDINT));
    t_dint_output = (IEC_UDINT *)calloc(elements, sizeof(IEC_UDINT));
    t_lint_input  = (IEC_ULINT *)calloc(elements, sizeof(IEC_ULINT));
    t_lint_output = (IEC_ULINT *)calloc(elements, sizeof(IEC_ULINT));
    t_int_memory  = (IEC_UINT *)calloc(elements, sizeof(IEC_UINT));
    t_dint_memory = (IEC_UDINT *)calloc(elements, sizeof(IEC_UDINT));
    t_lint_memory = (IEC_ULINT *)calloc(elements, sizeof(IEC_ULINT));

    const bool complete = next.bool_input && next.bool_output && next.bool_memory &&
                          next.byte_input && next.byte_output && next.int_input &&
                          next.int_output && next.dint_input && next.dint_output &&
                          next.lint_input && next.lint_output && next.int_memory &&
                          next.dint_memory && next.lint_memory && t_bool_input && t_bool_output &&
                          t_bool_memory && t_byte_input && t_byte_output && t_int_input &&
                          t_int_output && t_dint_input && t_dint_output && t_lint_input &&
                          t_lint_output && t_int_memory && t_dint_memory && t_lint_memory;

    if (!complete)
    {
        free(next.bool_input);
        free(next.bool_output);
        free(next.bool_memory);
        free(next.byte_input);
        free(next.byte_output);
        free(next.int_input);
        free(next.int_output);
        free(next.dint_input);
        free(next.dint_output);
        free(next.lint_input);
        free(next.lint_output);
        free(next.int_memory);
        free(next.dint_memory);
        free(next.lint_memory);
        free(t_bool_input);
        free(t_bool_output);
        free(t_bool_memory);
        free(t_byte_input);
        free(t_byte_output);
        free(t_int_input);
        free(t_int_output);
        free(t_dint_input);
        free(t_dint_output);
        free(t_lint_input);
        free(t_lint_output);
        free(t_int_memory);
        free(t_dint_memory);
        free(t_lint_memory);
        log_error("[image_tables] could not allocate an image of %u elements per table; "
                  "the previous image is untouched",
                  elements);
        return false;
    }

    // Everything succeeded: retire the old image and publish the new one.
    image_tables_free();

    g_image          = next;
    temp_bool_input  = t_bool_input;
    temp_bool_output = t_bool_output;
    temp_bool_memory = t_bool_memory;
    temp_byte_input  = t_byte_input;
    temp_byte_output = t_byte_output;
    temp_int_input   = t_int_input;
    temp_int_output  = t_int_output;
    temp_dint_input  = t_dint_input;
    temp_dint_output = t_dint_output;
    temp_lint_input  = t_lint_input;
    temp_lint_output = t_lint_output;
    temp_int_memory  = t_int_memory;
    temp_dint_memory = t_dint_memory;
    temp_lint_memory = t_lint_memory;
    g_capacity       = elements;

    log_info("[image_tables] image allocated: %u elements per table", elements);
    return true;
}

void image_tables_fill_null_pointers(void)
{
    int filled = 0;
    for (uint32_t i = 0; i < g_capacity; ++i)
    {
        for (int b = 0; b < 8; ++b)
        {
            if (!g_image.bool_input[i][b])  { temp_bool_input[i][b]  = 0; g_image.bool_input[i][b]  = &temp_bool_input[i][b];  ++filled; }
            if (!g_image.bool_output[i][b]) { temp_bool_output[i][b] = 0; g_image.bool_output[i][b] = &temp_bool_output[i][b]; ++filled; }
            if (!g_image.bool_memory[i][b]) { temp_bool_memory[i][b] = 0; g_image.bool_memory[i][b] = &temp_bool_memory[i][b]; ++filled; }
        }
        if (!g_image.byte_input[i])  { temp_byte_input[i]  = 0; g_image.byte_input[i]  = &temp_byte_input[i];  ++filled; }
        if (!g_image.byte_output[i]) { temp_byte_output[i] = 0; g_image.byte_output[i] = &temp_byte_output[i]; ++filled; }
        if (!g_image.int_input[i])   { temp_int_input[i]   = 0; g_image.int_input[i]   = &temp_int_input[i];   ++filled; }
        if (!g_image.int_output[i])  { temp_int_output[i]  = 0; g_image.int_output[i]  = &temp_int_output[i];  ++filled; }
        if (!g_image.dint_input[i])  { temp_dint_input[i]  = 0; g_image.dint_input[i]  = &temp_dint_input[i];  ++filled; }
        if (!g_image.dint_output[i]) { temp_dint_output[i] = 0; g_image.dint_output[i] = &temp_dint_output[i]; ++filled; }
        if (!g_image.lint_input[i])  { temp_lint_input[i]  = 0; g_image.lint_input[i]  = &temp_lint_input[i];  ++filled; }
        if (!g_image.lint_output[i]) { temp_lint_output[i] = 0; g_image.lint_output[i] = &temp_lint_output[i]; ++filled; }
        if (!g_image.int_memory[i])  { temp_int_memory[i]  = 0; g_image.int_memory[i]  = &temp_int_memory[i];  ++filled; }
        if (!g_image.dint_memory[i]) { temp_dint_memory[i] = 0; g_image.dint_memory[i] = &temp_dint_memory[i]; ++filled; }
        if (!g_image.lint_memory[i]) { temp_lint_memory[i] = 0; g_image.lint_memory[i] = &temp_lint_memory[i]; ++filled; }
    }
    log_info("[image_tables] filled %d NULL slots with backing buffers", filled);
}

/**
 * Null every slot of every table. THE ONLY PLACE `sizeof` IS TAKEN ON THEM.
 *
 * This used to be fourteen `memset(table, 0, sizeof(table))` calls at the top
 * of image_tables_clear_null_pointers(). Fourteen call sites is fourteen
 * places to miss when the tables become pointers plus counts (RTOP-284), and
 * missing one is silent: `sizeof` drops from 65536 to 8, it compiles without a
 * warning, and the damage only shows on the SECOND program load, when
 * fill_null_pointers() finds the slots still populated and declines to rebind
 * them -- so plugins keep writing into the previous program's memory.
 *
 * One function, so the heap version is one function body, and the
 * static_asserts beside the definition of g_image say when to write it.
 */
static void image_tables_zero_slots(void)
{
    // Was `memset(&g_image, 0, sizeof(g_image))` while the tables were inline
    // arrays. That line still compiles now and is now WRONG: it would null the
    // fourteen pointers and leak every table. This is the one function the
    // static_asserts above point at, and this is the change they were asking
    // for -- the length comes from g_capacity, never from sizeof.
    const uint32_t n = g_capacity;
    if (n == 0) return;

    std::memset(g_image.bool_input, 0, (size_t)n * sizeof(IEC_BOOL *[8]));
    std::memset(g_image.bool_output, 0, (size_t)n * sizeof(IEC_BOOL *[8]));
    std::memset(g_image.bool_memory, 0, (size_t)n * sizeof(IEC_BOOL *[8]));
    std::memset(g_image.byte_input, 0, (size_t)n * sizeof(IEC_BYTE *));
    std::memset(g_image.byte_output, 0, (size_t)n * sizeof(IEC_BYTE *));
    std::memset(g_image.int_input, 0, (size_t)n * sizeof(IEC_UINT *));
    std::memset(g_image.int_output, 0, (size_t)n * sizeof(IEC_UINT *));
    std::memset(g_image.dint_input, 0, (size_t)n * sizeof(IEC_UDINT *));
    std::memset(g_image.dint_output, 0, (size_t)n * sizeof(IEC_UDINT *));
    std::memset(g_image.lint_input, 0, (size_t)n * sizeof(IEC_ULINT *));
    std::memset(g_image.lint_output, 0, (size_t)n * sizeof(IEC_ULINT *));
    std::memset(g_image.int_memory, 0, (size_t)n * sizeof(IEC_UINT *));
    std::memset(g_image.dint_memory, 0, (size_t)n * sizeof(IEC_UDINT *));
    std::memset(g_image.lint_memory, 0, (size_t)n * sizeof(IEC_ULINT *));
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

    image_tables_zero_slots();

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
