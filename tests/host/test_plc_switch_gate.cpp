// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#include "plc_switch.h"

#include <chrono>
#include <cstdio>
#include <thread>

static_assert(PLC_SWITCH_REPORT_WAIT_MS == 2000, "the switch-report wait bound is 2 s");

static int g_failures = 0;

#define CHECK(cond, what)                                                                          \
    do                                                                                             \
    {                                                                                              \
        if (!(cond))                                                                               \
        {                                                                                          \
            std::printf("  FAIL  %s\n", what);                                                     \
            g_failures++;                                                                          \
        }                                                                                          \
    } while (0)

extern "C" void log_info(const char *, ...) {}
extern "C" void log_debug(const char *, ...) {}
extern "C" void log_warn(const char *, ...) {}
extern "C" void log_error(const char *, ...) {}

static const char *const VPP   = "fakevpp|./build/plugins/libfakevpp_plugin.so";
static const char *const IOMOD = "iomod|./build/plugins/libiomod_plugin.so";
static const char *const OTHER = "modbus|./core/src/drivers/plugins/python/modbus.py";

using Clock = std::chrono::steady_clock;

static long elapsed_ms(Clock::time_point since)
{
    return (long)std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - since)
        .count();
}

/* Forget every owner and leave the stored position at RUN with no movement on record. */
static void reset()
{
    plc_switch_owners_forget_absent(nullptr, 0);
    plc_switch_owner_report("reset|reset", PLC_SWITCH_RUN);
    plc_switch_owners_forget_absent(nullptr, 0);
    (void)plc_switch_take_movement();
}

static void test_no_owner_reads_run()
{
    std::printf("no switch owner\n");
    reset();
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_RUN, "gate is RUN with no owner");
    CHECK(plc_switch_allows_run(), "allows_run is true with no owner");
}

static void test_declared_unreported_reads_unknown()
{
    std::printf("declared owner that has not reported\n");
    reset();
    plc_switch_owner_declare(VPP, true);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_UNKNOWN, "gate is UNKNOWN");
    CHECK(!plc_switch_allows_run(), "allows_run is false while the gate is UNKNOWN");
}

static void test_declared_false_is_not_an_owner()
{
    std::printf("declaration that returns zero\n");
    reset();
    plc_switch_owner_declare(VPP, false);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_RUN, "a zero declaration makes no owner");
}

static void test_report_sets_the_gate()
{
    std::printf("declared owner reports a position\n");
    reset();
    plc_switch_owner_declare(VPP, true);
    plc_switch_owner_report(VPP, PLC_SWITCH_STOP);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_STOP, "report STOP: gate is STOP");
    CHECK(!plc_switch_allows_run(), "report STOP: allows_run is false");
    plc_switch_owner_report(VPP, PLC_SWITCH_RUN);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_RUN, "report RUN: gate is RUN");
}

static void test_invalidate_is_not_movement()
{
    std::printf("invalidate, then report the same position again\n");
    reset();
    plc_switch_owner_declare(VPP, true);
    plc_switch_owner_report(VPP, PLC_SWITCH_STOP);
    (void)plc_switch_take_movement();

    plc_switch_owner_invalidate(VPP);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_UNKNOWN, "invalidated owner: gate is UNKNOWN");
    plc_switch_owner_report(VPP, PLC_SWITCH_STOP);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_STOP, "re-reported STOP: gate is STOP");
    CHECK(!plc_switch_take_movement(), "re-reporting the same position records no movement");

    plc_switch_owner_report(VPP, PLC_SWITCH_RUN);
    CHECK(plc_switch_take_movement(), "a real edge after the re-report still records movement");
}

static void test_implicit_owner_survives_invalidate()
{
    std::printf("implicit owner: reported once, never declared\n");
    reset();
    plc_switch_owner_report(VPP, PLC_SWITCH_RUN);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_RUN, "first report: gate is RUN");
    plc_switch_owner_invalidate(VPP);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_UNKNOWN,
          "still an owner after invalidate: gate is UNKNOWN");
}

static void test_invalidate_never_makes_an_owner()
{
    std::printf("invalidating a plugin that never declared or reported\n");
    reset();
    plc_switch_owner_invalidate(OTHER);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_RUN, "no owner created: gate is RUN");
}

static void test_forget_absent()
{
    std::printf("owners forgotten when they leave the configuration\n");
    reset();
    plc_switch_owner_declare(VPP, true);
    const char *const present_without_vpp[] = {OTHER};
    plc_switch_owners_forget_absent(present_without_vpp, 1);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_RUN, "absent owner forgotten: gate is RUN");

    reset();
    plc_switch_owner_declare(VPP, true);
    plc_switch_owner_report(IOMOD, PLC_SWITCH_RUN);
    const char *const present_with_vpp[] = {VPP};
    plc_switch_owners_forget_absent(present_with_vpp, 1);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_UNKNOWN,
          "a configured owner is kept: gate stays UNKNOWN");
}

static void test_two_owners_one_reported()
{
    std::printf("two owners, one reported\n");
    reset();
    plc_switch_owner_declare(VPP, true);
    plc_switch_owner_declare(IOMOD, true);
    plc_switch_owner_report(VPP, PLC_SWITCH_RUN);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_UNKNOWN, "one owner unreported: gate is UNKNOWN");
    plc_switch_owner_report(IOMOD, PLC_SWITCH_RUN);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_RUN, "both reported: gate is RUN");
}

static void test_await_report_returns_on_a_report()
{
    std::printf("await_report: a report from another thread\n");
    reset();
    plc_switch_owner_declare(VPP, true);
    std::thread reporter([] {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        plc_switch_owner_report(VPP, PLC_SWITCH_RUN);
    });
    const Clock::time_point t0 = Clock::now();
    const bool reported        = plc_switch_await_report(PLC_SWITCH_REPORT_WAIT_MS);
    const long took            = elapsed_ms(t0);
    reporter.join();
    CHECK(reported, "returns true once the owner reports");
    CHECK(took < 1000, "returns on the report, not at the end of the wait");
}

static void test_await_report_times_out()
{
    std::printf("await_report: no report\n");
    reset();
    plc_switch_owner_declare(VPP, true);
    const unsigned timeout_ms  = 300;
    const Clock::time_point t0 = Clock::now();
    const bool reported        = plc_switch_await_report(timeout_ms);
    const long took            = elapsed_ms(t0);
    CHECK(!reported, "returns false without a report");
    CHECK(took >= (long)timeout_ms - 10, "waits the whole bound before giving up");
    CHECK(took < (long)timeout_ms + 1000, "gives up once the bound has elapsed");
}

static void test_has_owner()
{
    std::printf("has_owner: which plugins count as switch owners\n");
    reset();
    CHECK(!plc_switch_has_owner(), "no owner after reset: has_owner is false");

    plc_switch_owner_declare(VPP, true);
    CHECK(plc_switch_gate() == PLC_SWITCH_GATE_UNKNOWN, "declared, unreported: gate is UNKNOWN");
    CHECK(plc_switch_has_owner(), "a declared owner that has not reported counts");

    reset();
    plc_switch_owner_declare(VPP, false);
    CHECK(!plc_switch_has_owner(), "a zero declaration makes no owner");

    reset();
    plc_switch_owner_report(IOMOD, PLC_SWITCH_RUN);
    CHECK(plc_switch_has_owner(), "an implicit owner (one that reported) counts");
    plc_switch_owner_invalidate(IOMOD);
    CHECK(plc_switch_has_owner(), "an owner whose report was invalidated still counts");
    plc_switch_owners_forget_absent(nullptr, 0);
    CHECK(!plc_switch_has_owner(), "every owner forgotten: has_owner is false");
}

static void test_has_owner_on_overflow()
{
    std::printf("has_owner: more owners than the registry holds\n");
    reset();
    char ids[PLC_SWITCH_MAX_OWNERS + 1][32];
    for (int i = 0; i < PLC_SWITCH_MAX_OWNERS + 1; i++)
    {
        std::snprintf(ids[i], sizeof(ids[i]), "owner%d|./owner%d.so", i, i);
        plc_switch_owner_declare(ids[i], true);
    }
    CHECK(plc_switch_has_owner(), "registry overflowed: has_owner is true");
    plc_switch_owners_forget_absent(nullptr, 0);
    CHECK(!plc_switch_has_owner(), "overflow cleared by forget_absent: has_owner is false");
}

int main()
{
    test_no_owner_reads_run();
    test_declared_unreported_reads_unknown();
    test_declared_false_is_not_an_owner();
    test_report_sets_the_gate();
    test_invalidate_is_not_movement();
    test_implicit_owner_survives_invalidate();
    test_invalidate_never_makes_an_owner();
    test_forget_absent();
    test_two_owners_one_reported();
    test_await_report_returns_on_a_report();
    test_await_report_times_out();
    test_has_owner();
    test_has_owner_on_overflow();

    if (g_failures == 0)
        std::printf("all cases passed\n");
    return g_failures == 0 ? 0 : 1;
}
