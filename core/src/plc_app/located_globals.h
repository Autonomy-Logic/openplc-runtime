/* -----------------------------------------------------------------------------
 * located_globals.h — resolve which locatedVars[] entries are CONFIGURATION
 * VAR_GLOBAL ... AT, by joining against the .so's locatedGlobals[] array.
 *
 * Pure logic with no runtime dependencies, so it can be unit-tested directly
 * (tests/test_located_globals.c).
 *
 * Why a join, and why this module exists at all:
 *
 * locatedVars[] mixes two ownership classes. POU-local `VAR ... AT` is serviced
 * by the owning IEC task around run(); CONFIGURATION `VAR_GLOBAL ... AT` is
 * owned by no task, so the dispatcher copies it at the quiescent frame boundary.
 * The LocatedVar descriptors carry no scope, and the runtime cannot recover it
 * from the configuration tree — globals are file-scope singletons that appear
 * nowhere in ConfigurationInstance -> Resource -> Task -> Program.
 *
 * The runtime used to INFER the split from position, assuming the layout was
 * [program-local ...][config globals ...] and treating the tail uncovered by any
 * program's located_range() as the globals. strucpp actually emits config
 * globals FIRST, so the program-local block is always the tail: one POU-local
 * located variable made the computed count collapse to zero and every located
 * global silently stopped being serviced (forum: "%MX locations now invalid").
 *
 * strucpp now states the answer. locatedGlobals[] holds the canonical storage
 * pointer of each located VAR_GLOBAL — the same raw_ptr() value written into
 * locatedVars[].pointer — so an entry is config-scope exactly when its pointer
 * appears in that array. Pointer identity: no ordering rule, no layout
 * assumption, and no false positives, since distinct objects have distinct
 * addresses.
 * -------------------------------------------------------------------------- */

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

/* -----------------------------------------------------------------------------
 * Join locatedVars[] against locatedGlobals[] and collect the config-scope
 * indices, ascending.
 *
 *   lv_count        number of locatedVars[] entries
 *   located_vars    opaque handle passed back to pointer_at
 *   pointer_at      accessor yielding locatedVars[i]'s storage pointer
 *   globals         locatedGlobals[] — storage pointers of the located globals
 *   globals_count   number of entries in globals
 *   out_idx         receives the config-scope indices; room for lv_count
 *   out_matched     optional; receives how many `globals` entries matched some
 *                   located variable. A value below globals_count means the two
 *                   generated arrays disagree, which the caller should report.
 *
 * Returns the number of indices written to out_idx.
 *
 * Entries with a NULL pointer are skipped: an unbound descriptor cannot be
 * matched, and guessing a scope for it is exactly the failure this replaces.
 * -------------------------------------------------------------------------- */
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
