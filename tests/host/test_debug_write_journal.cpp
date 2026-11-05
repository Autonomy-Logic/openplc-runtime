// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/*
 * Host test for the external-write paths of debug_write_journal.cpp.
 *
 * Pins the difference between the two entry points:
 *
 *   - runtime_external_write() queues, and the queue is bounded, so a burst
 *     larger than it is refused rather than silently deferred;
 *   - runtime_external_write_now() applies at once, with the drain's routing,
 *     no entry cap and no payload cap, and reports a leaf it could not write.
 *     The retain restore depends on all three. Outside an open direct window
 *     it refuses every write.
 *
 * The strucpp debug exports and the image journal are stubbed here, so the
 * test runs without a compiled program.
 *
 *   ./tests/host/run.sh
 */
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

#include "debug_write_journal.h"
#include "journal_buffer.h"

// ---------------------------------------------------------------------------
// Stubs for what debug_write_journal.cpp links against
// ---------------------------------------------------------------------------
struct GlobalWrite
{
    uint8_t  arr;
    uint16_t elem;
    uint16_t len;
};

static std::vector<GlobalWrite> g_global_writes;
static uint8_t                  g_debug_write_status = 0x7E;
static uint16_t                 g_located_elem       = 0xFFFF;
static int                      g_journal_result     = 0;
static int                      g_journal_writes     = 0;

static uint8_t stub_debug_write(uint8_t arr, uint16_t elem, const uint8_t *, uint16_t len)
{
    g_global_writes.push_back({arr, elem, len});
    return g_debug_write_status;
}

static uint8_t stub_debug_set(uint8_t, uint16_t, bool, const uint8_t *, uint16_t)
{
    return 0x7E;
}

/* Leaf g_located_elem is %MW at index 3; every other leaf is a global. */
static int stub_debug_locate(uint8_t, uint16_t elem, uint8_t *area, uint8_t *size,
                             uint16_t *byte_index, uint8_t *bit_index)
{
    if (elem != g_located_elem) return 0;
    *area       = 2;
    *size       = 2;
    *byte_index = 3;
    *bit_index  = 0;
    return 1;
}

extern "C" {
uint8_t (*ext_strucpp_debug_set)(uint8_t, uint16_t, bool, const uint8_t *, uint16_t) =
    stub_debug_set;
uint8_t (*ext_strucpp_debug_write)(uint8_t, uint16_t, const uint8_t *, uint16_t) =
    stub_debug_write;
int (*ext_strucpp_debug_locate)(uint8_t, uint16_t, uint8_t *, uint8_t *, uint16_t *,
                                uint8_t *) = stub_debug_locate;

void log_warn(const char *, ...) {}
void log_error(const char *, ...) {}

int journal_write_bool(journal_buffer_type_t, uint16_t, uint8_t, bool)
{
    ++g_journal_writes;
    return g_journal_result;
}
int journal_write_byte(journal_buffer_type_t, uint16_t, uint8_t)
{
    ++g_journal_writes;
    return g_journal_result;
}
int journal_write_int(journal_buffer_type_t, uint16_t, uint16_t)
{
    ++g_journal_writes;
    return g_journal_result;
}
int journal_write_dint(journal_buffer_type_t, uint16_t, uint32_t)
{
    ++g_journal_writes;
    return g_journal_result;
}
int journal_write_lint(journal_buffer_type_t, uint16_t, uint64_t)
{
    ++g_journal_writes;
    return g_journal_result;
}
int journal_force_set(journal_buffer_type_t, uint16_t, uint8_t, uint64_t) { return 0; }
int journal_force_clear(journal_buffer_type_t, uint16_t, uint8_t) { return 0; }
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------
static int         g_failures = 0;
static const char *g_case     = "";

#define CHECK(cond, what)                                                                      \
    do {                                                                                       \
        if (!(cond)) {                                                                         \
            fprintf(stderr, "  FAIL  %s: %s\n         (%s:%d)\n", g_case, (what), __FILE__,     \
                    __LINE__);                                                                 \
            g_failures++;                                                                      \
        }                                                                                      \
    } while (0)

static void reset()
{
    debug_write_journal_reset();
    g_global_writes.clear();
    g_debug_write_status = 0x7E;
    g_located_elem       = 0xFFFF;
    g_journal_result     = 0;
    g_journal_writes     = 0;
    debug_write_journal_set_direct_window(true);
}

// ---------------------------------------------------------------------------
// Cases
// ---------------------------------------------------------------------------
static void case_queue_refuses_past_its_capacity()
{
    g_case = "queue refuses past its capacity";
    reset();
    const uint8_t v = 1;
    int           accepted = 0;
    for (uint16_t i = 0; i < 200; ++i)
    {
        if (runtime_external_write(0, i, DBGW_OP_WRITE, &v, 1) == 0) ++accepted;
    }
    CHECK(accepted == 128, "the queue holds 128 entries");
    debug_write_journal_drain();
    CHECK(g_global_writes.size() == 128, "only the queued entries are applied");
    debug_write_journal_drain();
    CHECK(g_global_writes.size() == 128, "a refused entry is never applied later");
}

static void case_now_has_no_entry_cap()
{
    g_case = "write_now has no entry cap";
    reset();
    const uint8_t v = 1;
    int           ok = 0;
    for (uint16_t i = 0; i < 1000; ++i)
    {
        if (runtime_external_write_now(0, i, DBGW_OP_WRITE, &v, 1) == 0) ++ok;
    }
    CHECK(ok == 1000, "every write is applied");
    CHECK(g_global_writes.size() == 1000, "every write reaches the IECVar");
}

static void case_now_has_no_payload_cap()
{
    g_case = "write_now has no payload cap";
    reset();
    std::vector<uint8_t> payload(1000, 'x');
    CHECK(runtime_external_write_now(0, 7, DBGW_OP_WRITE, payload.data(),
                                     (uint16_t)payload.size()) == 0,
          "a long payload is accepted");
    CHECK(!g_global_writes.empty() && g_global_writes.back().len == 1000,
          "the full payload length reaches the IECVar");
}

static void case_now_reports_a_refused_global()
{
    g_case = "write_now reports a refused global";
    reset();
    g_debug_write_status = 0x82;
    const uint8_t v      = 1;
    CHECK(runtime_external_write_now(0, 1, DBGW_OP_WRITE, &v, 1) == -1,
          "a refused write returns -1");
}

static void case_now_routes_a_located_leaf()
{
    g_case = "write_now routes a located leaf through the image journal";
    reset();
    g_located_elem     = 5;
    const uint8_t v[2] = {0x34, 0x12};
    CHECK(runtime_external_write_now(0, 5, DBGW_OP_WRITE, v, 2) == 0, "the write is applied");
    CHECK(g_journal_writes == 1, "it goes to the image journal");
    CHECK(g_global_writes.empty(), "it does not poke the IECVar");

    g_journal_result = -1;
    CHECK(runtime_external_write_now(0, 5, DBGW_OP_WRITE, v, 2) == -1,
          "a journal refusal returns -1");
}

static void case_now_refused_outside_the_window()
{
    g_case = "write_now is refused outside the direct window";
    reset();
    debug_write_journal_set_direct_window(false);
    const uint8_t v = 1;
    CHECK(runtime_external_write_now(0, 1, DBGW_OP_WRITE, &v, 1) == -1, "the write is refused");
    CHECK(g_global_writes.empty(), "nothing reaches the IECVar");
    CHECK(runtime_external_write(0, 1, DBGW_OP_WRITE, &v, 1) == 0, "the queue still accepts");
}

int main()
{
    printf("debug_write_journal — queued and immediate writes\n");
    case_queue_refuses_past_its_capacity();
    case_now_has_no_entry_cap();
    case_now_has_no_payload_cap();
    case_now_reports_a_refused_global();
    case_now_routes_a_located_leaf();
    case_now_refused_outside_the_window();
    reset();

    if (g_failures == 0)
    {
        printf("all cases passed\n");
        return 0;
    }
    fprintf(stderr, "%d check(s) failed\n", g_failures);
    return 1;
}
