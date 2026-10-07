// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef OPENPLC_LOCATED_GLOBALS_H
#define OPENPLC_LOCATED_GLOBALS_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Pointer accessor for a located variable. Kept as a callback so this module
 * needs no knowledge of the strucpp LocatedVar layout and stays unit-testable.
 * Returns the entry's canonical storage pointer, or NULL when unbound. */
typedef const void *(*located_pointer_at_fn)(const void *located_vars,
                                             uint32_t index);

/**
 * @brief Join located-variable descriptors with their global pointers.
 *
 * @param lv_count       entries in located_vars[].
 * @param located_vars   opaque descriptor array; pointer_at() dereferences it.
 * @param pointer_at     accessor returning each descriptor's storage pointer.
 * @param globals        array of global storage pointers to probe.
 * @param globals_count  entries in globals[].
 * @param out_idx        caller-provided buffer, room for lv_count entries;
 *                       filled with the located_vars index each global binds to.
 * @param out_matched    out: number of locatedGlobals[] entries that found a
 *                       descriptor; a value less than globals_count means the
 *                       two generated arrays disagree.
 * @return number of entries written to out_idx.
 */
uint32_t located_globals_join_ex(uint32_t lv_count,
                                 const void *located_vars,
                                 located_pointer_at_fn pointer_at,
                                 const void *const *globals,
                                 uint32_t globals_count,
                                 uint32_t *out_idx,
                                 uint32_t *out_matched);

#ifdef __cplusplus
}
#endif

#endif /* OPENPLC_LOCATED_GLOBALS_H */
