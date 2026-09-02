#ifndef IMAGE_TABLES_H
#define IMAGE_TABLES_H

#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>

#include "../lib/iec_types.h"
#include "plcapp_manager.h"

#ifdef __cplusplus
extern "C"
{
#endif

#define BUFFER_SIZE 1024
#define libplc_build_dir "./build"

    /* -------------------------------------------------------------------------
     * Image-table buffers (booleans, bytes, ints, dints, lints, memories).
     *
     * Populated at program-load time by image_tables_bind_located_vars(),
     * which walks strucpp::locatedVars[] and points each slot at the
     * matching IECVar's underlying primitive storage. Plugins read/write
     * these directly under the image-tables mutex.
     * --------------------------------------------------------------------- */

    extern IEC_BOOL *bool_input[BUFFER_SIZE][8];
    extern IEC_BOOL *bool_output[BUFFER_SIZE][8];

    extern IEC_BYTE *byte_input[BUFFER_SIZE];
    extern IEC_BYTE *byte_output[BUFFER_SIZE];

    extern IEC_UINT *int_input[BUFFER_SIZE];
    extern IEC_UINT *int_output[BUFFER_SIZE];

    extern IEC_UDINT *dint_input[BUFFER_SIZE];
    extern IEC_UDINT *dint_output[BUFFER_SIZE];

    extern IEC_ULINT *lint_input[BUFFER_SIZE];
    extern IEC_ULINT *lint_output[BUFFER_SIZE];

    extern IEC_UINT  *int_memory[BUFFER_SIZE];
    extern IEC_UDINT *dint_memory[BUFFER_SIZE];
    extern IEC_ULINT *lint_memory[BUFFER_SIZE];
    extern IEC_BOOL  *bool_memory[BUFFER_SIZE][8];

    /* -------------------------------------------------------------------------
     * Resolved .so symbols (populated by symbols_init).
     *
     * strucpp_set_current_time sets the per-.so __CURRENT_TIME_NS (thread_local
     * under STRUCPP_THREADED) for the calling thread; the GCD master-tick
     * dispatcher stamps each task's dispatch time and the worker thread calls
     * this at the top of its scan so IEC TIME() is stable within a scan.
     * strucpp_advance_time is retained for compatibility (unused by the
     * dispatcher). base_tick_ns is owned runtime-side (utils.c) and computed in
     * symbols_init by walking the loaded configuration.
     * --------------------------------------------------------------------- */

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
    /* Soft write — updates the variable's underlying value via
     * IECVar::set(). If the variable is currently forced, the write is
     * silently ignored (force remains authoritative). Distinct from
     * ext_strucpp_debug_set(forcing=true) which pins the value
     * indefinitely. Used by plugins (OPC-UA, BACnet) that want regular
     * write semantics rather than debugger-style forcing. */
    extern uint8_t  (*ext_strucpp_debug_write)      (uint8_t arr, uint16_t elem,
                                                     const uint8_t *bytes,
                                                     uint16_t len);

    /* ---- Retain marshalling (NODE-94) --------------------------------------
     *
     * The WALK lives inside the .so, not here: that is where the debug tables
     * and handle_read/handle_write are, and re-implementing the blob format on
     * this side would put two copies of one wire format in two repos.
     *
     * `unpack` takes a write CALLBACK because the runtime owns the write path.
     * A retained variable may also be LOCATED (`VAR RETAIN x AT %MW10`), and
     * poking such a leaf's IECVar is undone by the next copy-in from the
     * process image — so the callback we hand over routes through
     * runtime_external_write, which knows to send a located leaf through the
     * image journal.
     *
     * Optional: a program built by an older STruC++ resolves these to NULL and
     * the retain path simply never runs. */
    extern size_t   (*ext_strucpp_retain_blob_size)  (void);
    extern uint32_t (*ext_strucpp_retain_layout_hash)(void);
    extern size_t   (*ext_strucpp_retain_pack)       (uint8_t *out, size_t cap);
    extern uint8_t  (*ext_strucpp_retain_unpack)     (const uint8_t *blob, size_t len,
                                                      uint8_t (*write_leaf)(uint8_t, uint16_t,
                                                                            const uint8_t *, uint16_t));

    /* Located-variable classifier. Reports whether a debug (arr, elem) leaf is
     * a LOCATED variable and, if so, its image location (area / size /
     * byte_index / bit_index). Returns 1 + fills the out-params if located, 0
     * otherwise. The debug-write drain uses it to route located writes/forces
     * through the image journal + forced-slot bitmap (copy_in would clobber a
     * direct IECVar poke). OPTIONAL: an older .so without it leaves the pointer
     * NULL, and the drain treats every leaf as a global (IECVar) write. */
    extern int      (*ext_strucpp_debug_locate)     (uint8_t arr, uint16_t elem,
                                                     uint8_t *area, uint8_t *size,
                                                     uint16_t *byte_index,
                                                     uint8_t *bit_index);

    /* -------------------------------------------------------------------------
     * Symbol resolution.
     *
     * Resolves all required entry points from the dlopen'd .so, including
     * the strucpp shim entry (strucpp_get_config) the runtime needs to walk
     * the configuration. Initializes the runtime-owned image-tables mutex
     * (recursive PI) on first call. The mutex is locked by the runtime
     * directly; it is not handed to the .so.
     *
     * Returns 0 on success, -1 if anything required is missing.
     * --------------------------------------------------------------------- */
    int symbols_init(PluginManager *pm);

    /* -------------------------------------------------------------------------
     * Walk strucpp::locatedVars[] and point each image-table slot at the
     * corresponding IECVar's underlying primitive storage. Caller must hold
     * the image-tables mutex.
     * --------------------------------------------------------------------- */
    void image_tables_bind_located_vars(void);

    /* -------------------------------------------------------------------------
     * After binding, fill any unbound image-table slots with private
     * backing buffers so plugins reading those addresses don't dereference
     * NULL. Caller must hold the image-tables mutex.
     * --------------------------------------------------------------------- */
    void image_tables_fill_null_pointers(void);

    /* -------------------------------------------------------------------------
     * Reset all image-table pointers to NULL before unloading a program.
     * Caller must hold the image-tables mutex.
     * --------------------------------------------------------------------- */
    void image_tables_clear_null_pointers(void);

    /* -------------------------------------------------------------------------
     * Image-tables mutex accessor. Returns a pointer to the runtime-owned
     * recursive PI mutex that protects the image tables. The runtime locks
     * it directly; the .so never locks anything (generated code runs on its
     * own storage), so there is no lock handoff into the .so.
     * --------------------------------------------------------------------- */
    pthread_mutex_t *image_tables_mutex(void);

    /* -------------------------------------------------------------------------
     * Flush-on-lock read API. The canonical way for any consumer to obtain a
     * coherent view of the image for READING:
     *   image_lock()   -- take the image mutex, then drain the journal so the
     *                     holder sees every committed write.
     *   image_unlock() -- release the image mutex.
     * Writes never use this lock (they go through journal_write_*). Prefer the
     * bulk pattern: lock, copy the region to a local buffer, unlock, then do
     * any slow work (network, conversion) on the buffer outside the lock. The
     * mutex is recursive PI; the drain is a no-op when nothing is pending.
     * --------------------------------------------------------------------- */
    void image_lock(void);
    void image_unlock(void);

    /* -------------------------------------------------------------------------
     * Threaded process-image model (the only execution model — the runtime
     * compiles every .so itself with -DSTRUCPP_THREADED).
     *
     * The copy_in/out functions move a located slice [offset, offset+count) of
     * locatedVars[] between the runtime-owned image and the program's storage:
     *   - copy_in  : image -> members (called before run(), under the image
     *                mutex, after the journal drain).
     *   - copy_out : changed members -> journal (dirty-diff, lock-free; applied
     *                to the image on the next drain). %I is never committed.
     *
     * Shared globals no longer use a runtime-owned mutex — each carries its own
     * std::mutex inside the .so (strucpp GlobalVar<V>), taken per access in
     * run(). The config-scope helpers copy the located CONFIGURATION VAR_GLOBALs
     * at the dispatcher's quiescent frame boundary. Those entries are identified
     * at load by joining locatedVars[].pointer against the .so's locatedGlobals[]
     * on pointer identity — NOT by their position in locatedVars[], and NOT as a
     * contiguous slice (see image_tables.cpp).
     *
     * locatedGlobals[] is OPTIONAL: a program exported before STruC++ emitted it
     * does not export the accessors, in which case located configuration globals
     * are not synced at all (the program still runs, POU-local located I/O is
     * unaffected) and a warning is logged once at load.
     * --------------------------------------------------------------------- */
    void image_tables_threaded_copy_in(uint32_t offset, uint32_t count);
    void image_tables_threaded_copy_out(uint32_t offset, uint32_t count);
    void image_tables_copy_config_globals_in(void);
    void image_tables_copy_config_globals_out(void);

    /* -------------------------------------------------------------------------
     * Returns the cached strucpp::ConfigurationInstance* (as void* — the
     * runtime's .cpp callers static_cast to the right type). NULL until
     * symbols_init() succeeds; reset to NULL on image_tables_clear_null_pointers().
     * --------------------------------------------------------------------- */
    void *strucpp_config_handle(void);

#ifdef __cplusplus
}
#endif

#endif /* IMAGE_TABLES_H */
