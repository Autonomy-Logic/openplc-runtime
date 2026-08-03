/* -----------------------------------------------------------------------------
 * located_globals.c — see located_globals.h for the rationale.
 * -------------------------------------------------------------------------- */

#include <stddef.h>

#include "located_globals.h"

uint32_t located_globals_join_ex(uint32_t lv_count,
                                 const void *located_vars,
                                 located_pointer_at_fn pointer_at,
                                 const void *const *globals,
                                 uint32_t globals_count,
                                 uint32_t *out_idx,
                                 uint32_t *out_matched)
{
    uint32_t n = 0;

    if (out_matched) *out_matched = 0;
    if (!pointer_at || !out_idx) return 0;
    if (!globals || globals_count == 0) return 0;

    for (uint32_t i = 0; i < lv_count; ++i)
    {
        const void *p = pointer_at(located_vars, i);
        /* An unbound descriptor can't be matched, and must not be guessed at. */
        if (p == NULL) continue;

        for (uint32_t g = 0; g < globals_count; ++g)
        {
            if (globals[g] == p)
            {
                out_idx[n++] = i;
                break;
            }
        }
    }

    /* Report how many locatedGlobals[] entries found a home, so the caller can
     * detect the two generated arrays disagreeing. Counted separately because a
     * single global could in principle be referenced by more than one located
     * descriptor (aliasing), which would make the index count misleading. */
    if (out_matched)
    {
        uint32_t matched = 0;
        for (uint32_t g = 0; g < globals_count; ++g)
        {
            if (globals[g] == NULL) continue;
            for (uint32_t i = 0; i < lv_count; ++i)
            {
                if (pointer_at(located_vars, i) == globals[g])
                {
                    ++matched;
                    break;
                }
            }
        }
        *out_matched = matched;
    }

    return n;
}
