// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#ifndef IMAGE_TABLES_H
#define IMAGE_TABLES_H

#include "image_table_id.h"
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>

#include "../lib/plc_image_types.h"
#include "plcapp_manager.h"

#ifdef __cplusplus
extern "C"
{
#endif

/* BUFFER_SIZE is gone, and its absence is the point.
 *
 * It was 1024 per table, compiled in, identical for every program that ever
 * ran on the device: a project needing more could not have it, and a project
 * needing less paid for the rest anyway, out of the memory its own program
 * wanted. The image is now allocated per program load -- see
 * image_tables_alloc() and image_tables_capacity() below, which is where a
 * size comes from now.
 *
 * Nothing should reintroduce it. If some code needs to know how big the image
 * is, the answer is image_tables_capacity(), and the answer changes between
 * program loads. `-DBUFFER_SIZE=<n>` in project.yml is inert and can go
 * whenever that file is next touched. */
#define libplc_build_dir "./build"

    /* -------------------------------------------------------------------------
     * Image-table buffers (booleans, bytes, ints, dints, lints, memories).
     *
     * Populated at program-load time by image_tables_bind_located_vars(),
     * which walks strucpp::locatedVars[] and points each slot at the
     * matching IECVar's underlying primitive storage. Plugins read/write
     * these directly under the image-tables mutex.
     *
     * ONE SYMBOL, NOT FOURTEEN, and that is the point of the struct.
     *
     * These used to be fourteen separate globals, which meant fourteen chances
     * for another translation unit to declare one by hand and get it subtly
     * wrong. `plugin_driver.c` did exactly that: it redeclared all fourteen as
     * extern while already including this header. Redundant while the shapes
     * agree; two incompatible declarations in different TUs the moment they
     * stop, which C does not diagnose across TUs -- it links, and the reader
     * walks the wrong layout. With one struct there is one declaration to get
     * right, and it lives here.
     *
     * What the struct deliberately does NOT do is make the switch to heap
     * allocation a compile error. Indexing `IEC_BOOL *(*p)[8]`
     * is syntactically identical to indexing `IEC_BOOL *a[N][8]`, so every
     * access site compiles unchanged either way -- verified, with -Wall
     * -Wextra. The tripwires that do work are in image_tables.cpp: the size
     * assertions next to the definition, and the fact that `sizeof` on these
     * tables now appears in exactly one function.
     * --------------------------------------------------------------------- */

    typedef struct
    {
        /* Heap-allocated by image_tables_alloc(), each one
         * image_tables_capacity() elements long. These are the very types
         * plugin_types.h already declares for the same tables, which is what
         * lets the runtime args keep pointing straight at them.
         *
         * Indexing reads exactly as it did when these were [BUFFER_SIZE]
         * arrays. That is not a convenience -- it is the hazard: the compiler
         * cannot tell the two shapes apart at a use site, and `sizeof` silently
         * went from 65536 to 8 when they changed. The size assertions and the
         * single zeroing function in image_tables.cpp exist for exactly that,
         * and they are what caught this transition. */
        IEC_BOOL *(*bool_input)[8];
        IEC_BOOL *(*bool_output)[8];

        IEC_BYTE **byte_input;
        IEC_BYTE **byte_output;

        IEC_UINT **int_input;
        IEC_UINT **int_output;

        IEC_UDINT **dint_input;
        IEC_UDINT **dint_output;

        IEC_ULINT **lint_input;
        IEC_ULINT **lint_output;

        IEC_UINT **int_memory;
        IEC_UDINT **dint_memory;
        IEC_ULINT **lint_memory;
        IEC_BOOL *(*bool_memory)[8];
    } image_tables_t;

    extern image_tables_t g_image;

    /* -------------------------------------------------------------------------
     * How big the image has to be
     *
     * Two independent answers, and the runtime takes the larger:
     *
     *   CONFIGURED -- `image.conf`, installed from the program upload. The
     *   editor derives it from what the project contains: the addresses its
     *   producers claim (Modbus master points, EtherCAT channels, VPP slots,
     *   pins) and the located variables it declares. That is the only source
     *   that knows about producers, because a Modbus master I/O group can claim
     *   two thousand bits without the program declaring a single variable.
     *
     *   DERIVED -- walked out of the loaded .so's locatedVars[]. This one knows
     *   only what the PROGRAM declares, which is a subset, but it is always
     *   available and always current.
     *
     * The maximum of the two is what makes a missing or stale `image.conf`
     * harmless: it can leave the image larger than needed, never smaller than
     * the program requires. A device that was provisioned by some other route,
     * or whose editor predates the file, still comes up correct.
     * --------------------------------------------------------------------- */

    /* The table identities live in their own header so plugin_types.h can
     * reach them without pulling the runtime internals in. */

    /* Elements per table, in that table's own unit -- which for the three BOOL
     * tables is BYTES, because they are declared [N][8], and for every other
     * table is the number of addresses. Zero is a real answer: a program with
     * no `%QX` has no reason to carry a bool_output image. */
    typedef struct
    {
        uint32_t elements[IMAGE_TABLE_COUNT];
    } image_sizes_t;

    /** The key `image.conf` uses for a table, which is the table's own name. */
    const char *image_table_key(image_table_id_t id);

    /** Read the installed `image.conf`. Every table zero when the file is
     *  absent, which means "nothing configured, size from the program". */
    void image_sizes_read_conf(const char *config_path, image_sizes_t *out);

    /** Walk the loaded .so's locatedVars[] for the floor the PROGRAM requires.
     *  Zeroes `out` first, so an unloaded or symbol-less program yields zeros
     *  rather than stale numbers.
     *
     *  Takes the PluginManager and resolves the accessors from it rather than
     *  reading the file-scope ones: those are populated by `symbols_init`,
     *  which runs on the cycle thread and therefore AFTER the load path has
     *  already sized and allocated the image. Reading them here made the floor
     *  a zero vector on every load. */
    void image_sizes_derive_floor(PluginManager *pm, image_sizes_t *out);

    /** Per table, the larger of the two. */
    void image_sizes_take_max(image_sizes_t *dst, const image_sizes_t *other);

    /**
     * Make every table the same length: the largest any of them needs.
     *
     * The square fallback, for a run where some plugin does not understand
     * per-table sizes. It takes and returns an `image_sizes_t` rather than
     * collapsing to one number on purpose: a single figure for
     * fourteen tables is the assumption this work exists to remove, and a
     * helper that produces one is an invitation to reintroduce it.
     *
     * The LARGEST, not the smallest, because square has to cover every
     * area the program actually uses. It costs memory the project does not
     * need, which is the price of a plugin that cannot be told the truth.
     */
    void image_sizes_flatten(image_sizes_t *sizes);

    /** The most any one table may hold: the ceiling of the uint16 `byte_index`
     *  in the STruC++ ABI, so no located variable can address beyond it. The
     *  webserver refuses a larger `image.conf` at install for the same reason
     *  and reaches the number the same way. */
#define IMAGE_MAX_ELEMENTS 65536u

    /** The `image.conf` wire format this runtime reads.
     *
     * Version 2 carries a unit word on every value and leaves the three BOOL
     * tables in bits, which is the unit their ADDRESSES use; this file
     * converts to the [N][8] shape the storage has. A file declaring any other
     * version, or none, is ignored whole rather than read by today's rules --
     * guessing is how a unit change becomes a silent factor of eight.
     *
     * There is no version 1 to be compatible with. It was written but never
     * merged, so no device has ever read this file in that form. */
#define IMAGE_CONF_FORMAT_VERSION 2

    /**
     * Allocate the image at `elements` per table, replacing whatever is there.
     *
     * CALLER MUST HOLD THE IMAGE-TABLES MUTEX, as with bind / fill / clear
     * below. Stated because the two call sites used to disagree: the load path
     * locked and the boot path did not, and nothing said which was right.
     *
     * All or nothing, and that now covers the image already running: the new
     * tables are built into locals and published only once every allocation
     * has succeeded, so a failure leaves the previous image exactly as it was.
     * Returns false, having changed nothing observable; the caller logs and
     * stops. A partial image would be worse than none, because every table
     * indexes the same way whether it is real or null.
     */
    bool image_tables_alloc(const image_sizes_t *sizes);

    /** Release the image. Safe to call when nothing is allocated.
     *  CALLER MUST HOLD THE IMAGE-TABLES MUTEX. */
    void image_tables_free(void);

    /** The SMALLEST table's length, in elements; 0 before any allocation.
     *
     * Not "how big the image is": the tables no longer share a length. This is
     * the figure a consumer that reads one number can index every table with,
     * so it refuses early rather than running off the end of the shortest.
     * `image_table_capacity(id)` is the honest answer per area. */
    uint32_t image_tables_capacity(void);

    /** How long one table actually is, in its own elements.
     *
     * The tables no longer share a length, so this is the only honest answer
     * to "how far does this area reach". `image_tables_capacity()` remains for
     * consumers that read a single number and returns the SMALLEST of the
     * fourteen, which refuses an index rather than letting one run off the end
     * of a shorter table.
     *
     * Zero for an id outside the enum, which is the safe reading: a caller
     * that asks about a table this runtime does not have gets an area it
     * cannot index into. */
    uint32_t image_table_capacity(image_table_id_t id);

    extern void (*ext_strucpp_advance_time)(uint64_t tick_ns);
    /* Sets IEC TIME() for the CALLING thread. Call on the worker thread at the
     * top of its scan with the dispatch-stamped time. */
    extern void (*ext_strucpp_set_current_time)(int64_t ns);

    /* Hierarchical debug PDU shims (defined inside the .so by
     * debug_dispatch.hpp under STRUCPP_V4_DEBUG_EXPORTS_DEFINE). */
    extern uint8_t  (*ext_strucpp_debug_array_count)(void);
    extern uint16_t (*ext_strucpp_debug_elem_count) (uint8_t arr);
    extern uint16_t (*ext_strucpp_debug_size)       (uint8_t arr, uint16_t elem);
    extern uint8_t  (*ext_strucpp_debug_set)        (uint8_t arr, uint16_t elem,
                                                     bool forcing,
                                                     const uint8_t *bytes,
                                                     uint16_t len);
    extern uint16_t (*ext_strucpp_debug_read)       (uint8_t arr, uint16_t elem,
                                                     uint8_t *dest);
    /* Soft write via IECVar::set(). Silently ignored if the variable
     * is forced (force wins). Distinct from debug_set(forcing=true). */
    extern uint8_t  (*ext_strucpp_debug_write)      (uint8_t arr, uint16_t elem,
                                                     const uint8_t *bytes,
                                                     uint16_t len);

    /* Retain marshalling. Walk lives in the .so (where the debug
     * tables are). unpack takes a write callback so a LOCATED retained
     * leaf can be routed through the image journal. Optional. */
    extern size_t   (*ext_strucpp_retain_blob_size)  (void);
    extern uint32_t (*ext_strucpp_retain_layout_hash)(void);
    extern size_t (*ext_strucpp_retain_pack)(uint8_t *out, size_t cap);
    extern uint8_t (*ext_strucpp_retain_unpack)(const uint8_t *blob, size_t len,
                                                uint8_t (*write_leaf)(uint8_t, uint16_t,
                                                                      const uint8_t *, uint16_t));

    /* Classifier: returns 1 and fills out-params when (arr, elem) is a
     * LOCATED leaf; 0 otherwise. The debug-write drain uses this to
     * route located writes through the image journal. Optional. */
    extern int      (*ext_strucpp_debug_locate)     (uint8_t arr, uint16_t elem,
                                                     uint8_t *area, uint8_t *size,
                                                     uint16_t *byte_index,
                                                     uint8_t *bit_index);

    int symbols_init(PluginManager *pm);

    /* Caller must hold the image-tables mutex. */
    void image_tables_bind_located_vars(void);

    /* Caller must hold the image-tables mutex. */
    void image_tables_fill_null_pointers(void);

    /* Reset all image-table pointers to NULL before unloading a program.
     * Caller must hold the image-tables mutex. */
    void image_tables_clear_null_pointers(void);

    /**
     * @brief Write 0 to every output image slot (%QX, %QB, %QW, %QD, %QL).
     *
     * Used on every stop so plugins push de-energised outputs to the hardware
     * before they are stopped. Program storage is not touched. Caller must hold
     * the image-tables mutex.
     */
    void image_tables_zero_outputs(void);

    pthread_mutex_t *image_tables_mutex(void);

    void image_lock(void);
    void image_unlock(void);

    /* copy_in runs before run(), under the image mutex, after the journal drain.
     * copy_out publishes changed slots through the lock-free journal and never
     * commits %I. config_globals_* mirror the same ordering for CONFIG globals. */
    void image_tables_threaded_copy_in(uint32_t offset, uint32_t count);
    void image_tables_threaded_copy_out(uint32_t offset, uint32_t count);
    void image_tables_copy_config_globals_in(void);
    void image_tables_copy_config_globals_out(void);

    /* Seeds the image with every located leaf's ctor-initialised value, so a
     * declared `:= <literal>` is visible from the first scan. plc_retain_read
     * overrides retained slots on top. Takes the image-tables mutex itself. */
    void image_tables_seed_image_from_members(void);

    /* NULL until symbols_init() succeeds; cleared by image_tables_clear_null_pointers(). */
    void *strucpp_config_handle(void);

#ifdef __cplusplus
}
#endif

#endif /* IMAGE_TABLES_H */
