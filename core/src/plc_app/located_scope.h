/* -----------------------------------------------------------------------------
 * located_scope.h — partition locatedVars[] into config-scope globals and
 * program-local located variables.
 *
 * Pure logic, no runtime dependencies, so it can be unit-tested directly
 * (tests/test_located_scope.c).
 *
 * Background: a located variable is either a CONFIGURATION VAR_GLOBAL ... AT
 * (serviced by the dispatcher at the quiescent frame boundary) or a POU-local
 * VAR ... AT (serviced by the owning task around run()). The runtime used to
 * infer which was which from POSITION — it assumed strucpp emitted program-local
 * entries first and treated the uncovered tail as the globals. strucpp actually
 * emits config globals FIRST, so a single POU-local located variable made the
 * computed tail length collapse to zero and every located global silently
 * stopped being serviced.
 *
 * The authority is now scope_fn(), backed by the .so's strucpp_located_scope():
 * it answers from WHERE THE VARIABLE'S STORAGE LIVES, which is a property of the
 * compiled program and therefore independent of array order. Do not reintroduce
 * any positional rule here.
 * -------------------------------------------------------------------------- */

#ifndef OPENPLC_LOCATED_SCOPE_H
#define OPENPLC_LOCATED_SCOPE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Scope values returned by scope_fn (mirrors the shim's
 * STRUCPP_LOCATED_SCOPE_* macros). Anything negative means "cannot classify". */
#define LOCATED_SCOPE_PROGRAM 0
#define LOCATED_SCOPE_CONFIG  1

/* Per-entry cross-check outcome. The claimed[] bitmap is an INDEPENDENT witness
 * (which indices a task claims via located_range()); it is used only to detect
 * disagreement, never to derive the scope. */
typedef enum
{
    LOCATED_ANOMALY_NONE = 0,
    /* scope_fn could not classify the entry (unbound pointer, bad index). */
    LOCATED_ANOMALY_UNCLASSIFIABLE,
    /* Classified config-global, yet a task's located_range() also claims it —
     * the storage model drifted, or a slot is double-claimed (e.g. two
     * instances of one program type sharing a locatedVars[] entry). */
    LOCATED_ANOMALY_CONFIG_BUT_CLAIMED,
    /* Classified program-local, yet no task claims it — usually a program that
     * is not bound to any task, so the entry is never serviced. */
    LOCATED_ANOMALY_PROGRAM_BUT_UNCLAIMED
} located_anomaly_t;

/* -----------------------------------------------------------------------------
 * Partition the located-variable table.
 *
 *   lv_count        number of entries in locatedVars[]
 *   scope_fn        classifier; must not be NULL
 *   claimed         optional bitmap of length lv_count (1 = some task's
 *                   located_range() covers this index); NULL skips cross-checks
 *   out_config_idx  receives the config-global indices, ascending; must have
 *                   room for lv_count entries; must not be NULL
 *   out_anomaly     optional array of length lv_count receiving a
 *                   located_anomaly_t per entry
 *
 * Returns the number of indices written to out_config_idx. Unclassifiable
 * entries are excluded from the list — the caller reports them and leaves them
 * unserviced rather than guessing a scope.
 * -------------------------------------------------------------------------- */
uint32_t located_scope_partition(uint32_t lv_count,
                                 int (*scope_fn)(uint32_t index),
                                 const uint8_t *claimed,
                                 uint32_t *out_config_idx,
                                 int8_t *out_anomaly);

#ifdef __cplusplus
}
#endif

#endif /* OPENPLC_LOCATED_SCOPE_H */
