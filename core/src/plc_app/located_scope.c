/* -----------------------------------------------------------------------------
 * located_scope.c — see located_scope.h for the rationale.
 * -------------------------------------------------------------------------- */

#include "located_scope.h"

uint32_t located_scope_partition(uint32_t lv_count,
                                 int (*scope_fn)(uint32_t index),
                                 const uint8_t *claimed,
                                 uint32_t *out_config_idx,
                                 int8_t *out_anomaly)
{
    uint32_t n = 0;

    if (!scope_fn || !out_config_idx) return 0;

    for (uint32_t i = 0; i < lv_count; ++i)
    {
        int8_t anomaly = LOCATED_ANOMALY_NONE;
        int    scope   = scope_fn(i);

        if (scope < 0)
        {
            /* Not classifiable — record it and skip. Deliberately NOT defaulted
             * to either scope: a wrong guess here is exactly the failure mode
             * this module exists to remove. */
            anomaly = LOCATED_ANOMALY_UNCLASSIFIABLE;
            if (out_anomaly) out_anomaly[i] = anomaly;
            continue;
        }

        if (scope == LOCATED_SCOPE_CONFIG)
        {
            if (claimed && claimed[i]) anomaly = LOCATED_ANOMALY_CONFIG_BUT_CLAIMED;
            out_config_idx[n++] = i;
        }
        else
        {
            if (claimed && !claimed[i]) anomaly = LOCATED_ANOMALY_PROGRAM_BUT_UNCLAIMED;
        }

        if (out_anomaly) out_anomaly[i] = anomaly;
    }

    return n;
}
