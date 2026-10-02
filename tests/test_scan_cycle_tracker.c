// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/*
 * test_scan_cycle_tracker.c — unit tests for the per-task scan-cycle
 * tracker introduced alongside the multi-task refactor.
 *
 * Replaces the previous global-stats path (single fastest-task counters
 * shared across all threads). Each task now owns its own tracker and the
 * STATS handler walks plc_tasks[] to emit one entry per task.
 *
 * The behaviour we lock here:
 *   - first call to scan_cycle_tracker_start() seeds anchors WITHOUT
 *     emitting cycle-time / latency stats (those need a baseline);
 *   - second and subsequent starts compute cycle_time = now -
 *     last_start, latency = now - expected_start;
 *   - scan_cycle_tracker_end() captures scan_time = now - last_start
 *     and increments `overruns` if we ran past the projected next-wakeup;
 *   - scan_cycle_tracker_snapshot() returns false until at least one
 *     scan completes, then returns the tracker's stats with the avg
 *     fields recovered from the EWMA sum/avg_window.
 *
 * The EWMA window is computed as EWMA_TARGET_WINDOW_US / interval_us
 * (clamped to >= 1). Tests that pin specific `avg` values choose
 * intervals that produce avg_window=1, so a single sample IS the average
 * — that side-steps the cold-start ramp the reviewer flagged in #16,
 * which is intended behaviour.
 */

#include "scan_cycle_manager.h"
#include "unity.h"

#include <stdint.h>
#include <string.h>
#include <time.h>

/* Allow the test to drive elapsed time deterministically. The tracker
 * uses CLOCK_MONOTONIC under the hood; all assertions compare against
 * elapsed durations rather than absolute timestamps to ride over real
 * clock noise. */

static scan_cycle_tracker_t tracker;

void setUp(void)
{
    memset(&tracker, 0, sizeof(tracker));
}

void tearDown(void)
{
    scan_cycle_tracker_cleanup(&tracker);
}

/* Busy-wait for at least `us` microseconds of CLOCK_MONOTONIC time.
 * Tests use this to control the gap between start/end calls without
 * pulling in the full mock-clock infrastructure ceedling would need. */
static void busy_sleep_us(uint64_t us)
{
    struct timespec t0, now;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    do
    {
        clock_gettime(CLOCK_MONOTONIC, &now);
        uint64_t dt = (uint64_t)(now.tv_sec - t0.tv_sec) * 1000000ull +
                      (uint64_t)(now.tv_nsec - t0.tv_nsec) / 1000ull;
        if (dt >= us) break;
    } while (1);
}

/* ----------------------------------------------------------------------
 *  Initialisation and validation
 * -------------------------------------------------------------------- */

void test_init_rejects_null(void)
{
    TEST_ASSERT_EQUAL(-1, scan_cycle_tracker_init(NULL, 1000000));
}

void test_init_seeds_min_to_int64_max(void)
{
    /* On a fresh tracker, min fields must start at INT64_MAX so the
     * first observation always wins the min comparison — otherwise
     * min would be reported as 0 forever. */
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&tracker, 1000000));
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.scan_time_min);
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.cycle_time_min);
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.cycle_latency_min);
}

void test_init_clamps_avg_window_to_at_least_one(void)
{
    /* interval_ns = 0 would normally yield interval_us = 0 and a
     * divide-by-zero in the avg_window calculation. The clamp catches
     * that and drops to 1 (single-sample window). */
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&tracker, 0));
    TEST_ASSERT_EQUAL_INT64(1, tracker.avg_window);
}

void test_init_avg_window_matches_target_for_1ms_cycle(void)
{
    /* 1 ms cycle, target window 2 s → 2000 samples. */
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&tracker, 1000000));
    TEST_ASSERT_EQUAL_INT64(2000, tracker.avg_window);
}

void test_init_avg_window_matches_target_for_100ms_cycle(void)
{
    /* 100 ms cycle, target window 2 s → 20 samples. */
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&tracker, 100000000));
    TEST_ASSERT_EQUAL_INT64(20, tracker.avg_window);
}

/* ----------------------------------------------------------------------
 *  First-cycle seeding behaviour
 * -------------------------------------------------------------------- */

void test_first_start_only_seeds_no_stats_emitted(void)
{
    /* The first call to start() lays down anchors but cannot compute
     * cycle_time or latency (no prior reference). After it returns,
     * scan_count is 1 but stats aren't meaningful — snapshot returns
     * true once scan_count > 0, but min fields stay at INT64_MAX
     * until the second cycle observes something. */
    scan_cycle_tracker_init(&tracker, 1000000);
    scan_cycle_tracker_start(&tracker);

    TEST_ASSERT_EQUAL_INT64(1, tracker.stats.scan_count);
    /* Did NOT touch cycle_time_min — first cycle has no baseline */
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.cycle_time_min);
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.cycle_latency_min);
    TEST_ASSERT_NOT_EQUAL(0, tracker.last_start_us);
    TEST_ASSERT_NOT_EQUAL(0, tracker.expected_start_us);
}

void test_snapshot_returns_false_before_first_scan(void)
{
    /* scan_count starts at 0; snapshot's "valid" predicate is
     * scan_count > 0. Important for the JSON path: we emit nulls for
     * pre-first-scan trackers instead of bogus zero stats. */
    scan_cycle_tracker_init(&tracker, 1000000);
    plc_timing_stats_t out = {0};
    bool valid = scan_cycle_tracker_snapshot(&tracker, &out);
    TEST_ASSERT_FALSE(valid);
}

void test_snapshot_returns_true_after_first_start(void)
{
    scan_cycle_tracker_init(&tracker, 1000000);
    scan_cycle_tracker_start(&tracker);
    plc_timing_stats_t out = {0};
    bool valid = scan_cycle_tracker_snapshot(&tracker, &out);
    TEST_ASSERT_TRUE(valid);
    TEST_ASSERT_EQUAL_INT64(1, out.scan_count);
}

void test_snapshot_rejects_null_args(void)
{
    plc_timing_stats_t out = {0};
    TEST_ASSERT_FALSE(scan_cycle_tracker_snapshot(NULL, &out));

    scan_cycle_tracker_init(&tracker, 1000000);
    TEST_ASSERT_FALSE(scan_cycle_tracker_snapshot(&tracker, NULL));
}

/* ----------------------------------------------------------------------
 *  Per-cycle metric capture
 * -------------------------------------------------------------------- */

void test_scan_time_captured_between_start_and_end(void)
{
    scan_cycle_tracker_init(&tracker, 1000000);
    scan_cycle_tracker_start(&tracker);
    busy_sleep_us(2000);                  /* ~2 ms scan body */
    scan_cycle_tracker_end(&tracker);

    /* scan_time_min is updated by end(). It should reflect ~2 ms. */
    TEST_ASSERT_GREATER_OR_EQUAL_INT64(1500, tracker.stats.scan_time_min);
    /* Allow a generous upper bound for CI / scheduler noise. */
    TEST_ASSERT_LESS_OR_EQUAL_INT64(50000, tracker.stats.scan_time_max);
}

void test_cycle_time_captured_between_consecutive_starts(void)
{
    /* cycle_time = elapsed since previous start, computed on the
     * SECOND start() call onward. We sleep enough to dominate
     * scheduler noise. */
    scan_cycle_tracker_init(&tracker, 1000000);
    scan_cycle_tracker_start(&tracker);          /* seed */
    busy_sleep_us(3000);
    scan_cycle_tracker_end(&tracker);
    busy_sleep_us(2000);
    scan_cycle_tracker_start(&tracker);          /* now we measure */

    TEST_ASSERT_GREATER_OR_EQUAL_INT64(4000, tracker.stats.cycle_time_min);
    TEST_ASSERT_EQUAL_INT64(2, tracker.stats.scan_count);
}

void test_overruns_increment_when_scan_runs_past_deadline(void)
{
    /* Tight 1 ms interval, sleep 5 ms inside the scan body — the end()
     * must observe now > expected_start and bump overruns. */
    scan_cycle_tracker_init(&tracker, 1000000);
    scan_cycle_tracker_start(&tracker);
    busy_sleep_us(5000);
    scan_cycle_tracker_end(&tracker);

    TEST_ASSERT_GREATER_OR_EQUAL_INT64(1, tracker.stats.overruns);
}

void test_no_overrun_when_scan_finishes_within_period(void)
{
    /* 100 ms interval, sleep 1 ms inside scan: well within budget. */
    scan_cycle_tracker_init(&tracker, 100000000);
    scan_cycle_tracker_start(&tracker);
    busy_sleep_us(1000);
    scan_cycle_tracker_end(&tracker);

    TEST_ASSERT_EQUAL_INT64(0, tracker.stats.overruns);
}

/* ----------------------------------------------------------------------
 *  EWMA recovery
 * -------------------------------------------------------------------- */

void test_avg_recovers_single_sample_when_avg_window_is_one(void)
{
    /* avg_window=1 makes the EWMA collapse to "the latest sample IS
     * the average". interval_ns = EWMA_TARGET_WINDOW_US * 1000 makes
     * the calculation interval_us / EWMA_TARGET_WINDOW_US = 1 sample.
     *
     * This sidesteps the cold-start ramp the reviewer flagged in #16
     * — at avg_window=1 there is no ramp.
     *
     * 2 s = 2_000_000 us → interval_ns = 2_000_000_000 (2 s cycle).
     */
    scan_cycle_tracker_init(&tracker, 2000000000LL);
    TEST_ASSERT_EQUAL_INT64(1, tracker.avg_window);

    scan_cycle_tracker_start(&tracker);
    busy_sleep_us(1500);
    scan_cycle_tracker_end(&tracker);

    plc_timing_stats_t out = {0};
    TEST_ASSERT_TRUE(scan_cycle_tracker_snapshot(&tracker, &out));

    /* scan_time_avg should equal the single observed sample (within
     * the ~us-noise floor of clock_gettime + busy-sleep). */
    TEST_ASSERT_GREATER_OR_EQUAL_INT64(1000, out.scan_time_avg);
    TEST_ASSERT_LESS_OR_EQUAL_INT64(50000,  out.scan_time_avg);
}

/* ----------------------------------------------------------------------
 *  NULL / cleanup safety
 * -------------------------------------------------------------------- */

void test_start_end_null_safe(void)
{
    /* All public entry points must be NULL-safe — task threads call
     * these inside a tight loop and crashing on a partially-constructed
     * tracker would be worse than a no-op. */
    scan_cycle_tracker_start(NULL);
    scan_cycle_tracker_end(NULL);
    /* Reaching here means no segfault. */
    TEST_PASS();
}

void test_cleanup_null_safe(void)
{
    scan_cycle_tracker_cleanup(NULL);
    TEST_PASS();
}
