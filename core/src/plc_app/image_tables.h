// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef IMAGE_TABLES_H
#define IMAGE_TABLES_H

#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>

#include "../lib/plc_image_types.h"
#include "plcapp_manager.h"

#ifdef __cplusplus
extern "C"
{
#endif

#define BUFFER_SIZE 1024
#define libplc_build_dir "./build"

    

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
    extern size_t   (*ext_strucpp_retain_pack)       (uint8_t *out, size_t cap);
    extern uint8_t  (*ext_strucpp_retain_unpack)     (const uint8_t *blob, size_t len,
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

    
    void image_tables_bind_located_vars(void);

    
    void image_tables_fill_null_pointers(void);

    /* -------------------------------------------------------------------------
     * Reset all image-table pointers to NULL before unloading a program.
     * Caller must hold the image-tables mutex.
     * --------------------------------------------------------------------- */
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

    
    void image_tables_threaded_copy_in(uint32_t offset, uint32_t count);
    void image_tables_threaded_copy_out(uint32_t offset, uint32_t count);
    void image_tables_copy_config_globals_in(void);
    void image_tables_copy_config_globals_out(void);

    
    void *strucpp_config_handle(void);

#ifdef __cplusplus
}
#endif

#endif /* IMAGE_TABLES_H */
