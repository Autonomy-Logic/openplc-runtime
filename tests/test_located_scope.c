/*
 * test_located_scope.c — unit tests for located_scope_partition(), which splits
 * locatedVars[] into config-scope globals (dispatcher-serviced, at the quiescent
 * frame boundary) and program-local located variables (task-serviced).
 *
 * Why this exists: the runtime previously derived the config-scope set from
 * POSITION — it assumed strucpp emitted program-local entries first and treated
 * the tail not covered by any program's located_range() as the globals. strucpp
 * actually emits config globals FIRST, so `covered_end` always reached
 * lv_count once any POU declared a located variable, the computed count
 * collapsed to 0, and EVERY located configuration global (%MX, %QX, %MW alike)
 * silently stopped being serviced. Reported on the forum as "%MX locations now
 * invalid - Runtime v4".
 *
 * The behaviour locked here:
 *   - the partition follows scope_fn ONLY, so it is identical whether globals
 *     come first, last, or interleaved (ordering_* cases);
 *   - the claimed[] bitmap is a cross-check witness, never a source of truth:
 *     it can flag disagreement but cannot move an entry between buckets;
 *   - unclassifiable entries are excluded from the config list rather than
 *     defaulted into either bucket;
 *   - a config global also claimed by a task range, and a program-local entry
 *     claimed by nobody, are both reported as anomalies.
 *
 * Build (standalone, no runtime deps):
 *   cc -std=c11 -Wall -Wextra -I core/src/plc_app \
 *      tests/test_located_scope.c core/src/plc_app/located_scope.c \
 *      -o /tmp/test_located_scope && /tmp/test_located_scope
 */

#include <stdio.h>
#include <string.h>

#include "located_scope.h"

static int g_failures = 0;

#define CHECK(cond, msg, ...)                                                  \
    do {                                                                       \
        if (!(cond)) {                                                         \
            printf("  FAIL: " msg "\n", ##__VA_ARGS__);                        \
            ++g_failures;                                                      \
        }                                                                      \
    } while (0)

/* ---------------------------------------------------------------------------
 * Scope table driven by the current test case. Index -> LOCATED_SCOPE_* or -1.
 * ------------------------------------------------------------------------- */
#define MAX_VARS 16
static int g_scopes[MAX_VARS];

static int scope_fn(uint32_t index)
{
    if (index >= MAX_VARS) return -1;
    return g_scopes[index];
}

static void set_scopes(const int *scopes, uint32_t n)
{
    for (uint32_t i = 0; i < MAX_VARS; ++i) g_scopes[i] = -1;
    for (uint32_t i = 0; i < n; ++i) g_scopes[i] = scopes[i];
}

/* ---------------------------------------------------------------------------
 * The regression: config globals FIRST, one program-local var last. Under the
 * old positional rule this produced zero config entries.
 * ------------------------------------------------------------------------- */
static void test_globals_first(void)
{
    printf("test_globals_first (the reported regression)\n");
    /* locatedVars = [ %MD0 global, %MX0.0 global, %IX0.1 program-local ] */
    const int scopes[] = { LOCATED_SCOPE_CONFIG, LOCATED_SCOPE_CONFIG,
                           LOCATED_SCOPE_PROGRAM };
    const uint8_t claimed[] = { 0, 0, 1 };   /* PLC_PRG's range is (2,1) */
    set_scopes(scopes, 3);

    uint32_t idx[3];
    int8_t   anomaly[3];
    uint32_t n = located_scope_partition(3, scope_fn, claimed, idx, anomaly);

    CHECK(n == 2, "expected 2 config globals, got %u", n);
    CHECK(idx[0] == 0 && idx[1] == 1, "expected indices [0,1], got [%u,%u]",
          idx[0], idx[1]);
    for (uint32_t i = 0; i < 3; ++i)
        CHECK(anomaly[i] == LOCATED_ANOMALY_NONE,
              "entry %u unexpectedly flagged (%d)", i, anomaly[i]);
}

/* ---------------------------------------------------------------------------
 * Order independence: the same project laid out three different ways must
 * produce the same config set. This is the property the old code lacked.
 * ------------------------------------------------------------------------- */
static void test_ordering_independence(void)
{
    printf("test_ordering_independence\n");

    /* (a) globals last */
    {
        const int scopes[] = { LOCATED_SCOPE_PROGRAM, LOCATED_SCOPE_CONFIG,
                               LOCATED_SCOPE_CONFIG };
        const uint8_t claimed[] = { 1, 0, 0 };
        set_scopes(scopes, 3);
        uint32_t idx[3];
        uint32_t n = located_scope_partition(3, scope_fn, claimed, idx, NULL);
        CHECK(n == 2, "(globals last) expected 2, got %u", n);
        CHECK(idx[0] == 1 && idx[1] == 2, "(globals last) got [%u,%u]",
              idx[0], idx[1]);
    }

    /* (b) interleaved */
    {
        const int scopes[] = { LOCATED_SCOPE_CONFIG, LOCATED_SCOPE_PROGRAM,
                               LOCATED_SCOPE_CONFIG, LOCATED_SCOPE_PROGRAM };
        const uint8_t claimed[] = { 0, 1, 0, 1 };
        set_scopes(scopes, 4);
        uint32_t idx[4];
        uint32_t n = located_scope_partition(4, scope_fn, claimed, idx, NULL);
        CHECK(n == 2, "(interleaved) expected 2, got %u", n);
        CHECK(idx[0] == 0 && idx[1] == 2, "(interleaved) got [%u,%u]",
              idx[0], idx[1]);
    }
}

/* ---------------------------------------------------------------------------
 * Multi-program shape that also collapsed to zero under the old rule:
 * 3 globals, then two programs with two located vars each.
 * ------------------------------------------------------------------------- */
static void test_multi_program(void)
{
    printf("test_multi_program\n");
    const int scopes[] = { LOCATED_SCOPE_CONFIG, LOCATED_SCOPE_CONFIG,
                           LOCATED_SCOPE_CONFIG,
                           LOCATED_SCOPE_PROGRAM, LOCATED_SCOPE_PROGRAM,
                           LOCATED_SCOPE_PROGRAM, LOCATED_SCOPE_PROGRAM };
    const uint8_t claimed[] = { 0, 0, 0, 1, 1, 1, 1 };  /* ranges (3,2) + (5,2) */
    set_scopes(scopes, 7);

    uint32_t idx[7];
    int8_t   anomaly[7];
    uint32_t n = located_scope_partition(7, scope_fn, claimed, idx, anomaly);

    CHECK(n == 3, "expected 3 config globals, got %u", n);
    CHECK(idx[0] == 0 && idx[1] == 1 && idx[2] == 2,
          "expected [0,1,2], got [%u,%u,%u]", idx[0], idx[1], idx[2]);
    for (uint32_t i = 0; i < 7; ++i)
        CHECK(anomaly[i] == LOCATED_ANOMALY_NONE, "entry %u flagged (%d)",
              i, anomaly[i]);
}

/* ------------------------------------------------------------------------- */
static void test_edges(void)
{
    printf("test_edges\n");

    /* No located vars at all. */
    {
        uint32_t idx[1];
        set_scopes(NULL, 0);
        CHECK(located_scope_partition(0, scope_fn, NULL, idx, NULL) == 0,
              "empty table should yield 0");
    }

    /* Only globals (no POU declares a located var) — the case that happened to
     * work under the old rule. */
    {
        const int scopes[] = { LOCATED_SCOPE_CONFIG, LOCATED_SCOPE_CONFIG };
        const uint8_t claimed[] = { 0, 0 };
        set_scopes(scopes, 2);
        uint32_t idx[2];
        CHECK(located_scope_partition(2, scope_fn, claimed, idx, NULL) == 2,
              "globals-only should yield 2");
    }

    /* Only program-local vars. */
    {
        const int scopes[] = { LOCATED_SCOPE_PROGRAM, LOCATED_SCOPE_PROGRAM };
        const uint8_t claimed[] = { 1, 1 };
        set_scopes(scopes, 2);
        uint32_t idx[2];
        CHECK(located_scope_partition(2, scope_fn, claimed, idx, NULL) == 0,
              "program-only should yield 0");
    }

    /* Defensive: NULL scope_fn / NULL output must not crash and must yield 0. */
    {
        uint32_t idx[2];
        CHECK(located_scope_partition(2, NULL, NULL, idx, NULL) == 0,
              "NULL scope_fn should yield 0");
        CHECK(located_scope_partition(2, scope_fn, NULL, NULL, NULL) == 0,
              "NULL out_config_idx should yield 0");
    }

    /* claimed == NULL disables cross-checks but must not change the partition. */
    {
        const int scopes[] = { LOCATED_SCOPE_CONFIG, LOCATED_SCOPE_PROGRAM };
        set_scopes(scopes, 2);
        uint32_t idx[2];
        int8_t   anomaly[2] = { 99, 99 };
        CHECK(located_scope_partition(2, scope_fn, NULL, idx, anomaly) == 1,
              "claimed=NULL should still yield 1");
        CHECK(anomaly[0] == LOCATED_ANOMALY_NONE &&
              anomaly[1] == LOCATED_ANOMALY_NONE,
              "claimed=NULL must not raise anomalies");
    }
}

/* ---------------------------------------------------------------------------
 * Anomalies. These must be reported, and must NOT silently move an entry
 * between buckets — reporting is the whole point of keeping two witnesses.
 * ------------------------------------------------------------------------- */
static void test_anomalies(void)
{
    printf("test_anomalies\n");

    /* Unclassifiable entry: excluded from the config list, flagged, and the
     * surrounding entries still classify normally. */
    {
        const int scopes[] = { LOCATED_SCOPE_CONFIG, -1, LOCATED_SCOPE_PROGRAM };
        const uint8_t claimed[] = { 0, 0, 1 };
        set_scopes(scopes, 3);
        uint32_t idx[3];
        int8_t   anomaly[3] = { 0, 0, 0 };
        uint32_t n = located_scope_partition(3, scope_fn, claimed, idx, anomaly);
        CHECK(n == 1, "expected 1 config global, got %u", n);
        CHECK(idx[0] == 0, "expected index 0, got %u", idx[0]);
        CHECK(anomaly[1] == LOCATED_ANOMALY_UNCLASSIFIABLE,
              "entry 1 should be UNCLASSIFIABLE, got %d", anomaly[1]);
    }

    /* Config global that a task range also claims — storage-model drift, or the
     * double-claim produced by two instances of one program type sharing a
     * locatedVars[] slot. Still counted as config (scope_fn is authority). */
    {
        const int scopes[] = { LOCATED_SCOPE_CONFIG };
        const uint8_t claimed[] = { 1 };
        set_scopes(scopes, 1);
        uint32_t idx[1];
        int8_t   anomaly[1] = { 0 };
        uint32_t n = located_scope_partition(1, scope_fn, claimed, idx, anomaly);
        CHECK(n == 1, "config-but-claimed must still be listed, got %u", n);
        CHECK(anomaly[0] == LOCATED_ANOMALY_CONFIG_BUT_CLAIMED,
              "expected CONFIG_BUT_CLAIMED, got %d", anomaly[0]);
    }

    /* Program-local that nobody claims — e.g. a program not bound to a task. */
    {
        const int scopes[] = { LOCATED_SCOPE_PROGRAM };
        const uint8_t claimed[] = { 0 };
        set_scopes(scopes, 1);
        uint32_t idx[1];
        int8_t   anomaly[1] = { 0 };
        uint32_t n = located_scope_partition(1, scope_fn, claimed, idx, anomaly);
        CHECK(n == 0, "program-local must not enter the config list, got %u", n);
        CHECK(anomaly[0] == LOCATED_ANOMALY_PROGRAM_BUT_UNCLAIMED,
              "expected PROGRAM_BUT_UNCLAIMED, got %d", anomaly[0]);
    }
}

int main(void)
{
    printf("=== located_scope_partition unit tests ===\n");
    test_globals_first();
    test_ordering_independence();
    test_multi_program();
    test_edges();
    test_anomalies();

    if (g_failures == 0)
    {
        printf("=== all tests passed ===\n");
        return 0;
    }
    printf("=== %d check(s) FAILED ===\n", g_failures);
    return 1;
}
