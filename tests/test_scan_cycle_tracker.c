// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/* Unit tests for the per-task scan_cycle_tracker: every metric starts from the second cycle,
 * latency is measured against the scheduled tick, and an overrun is a scan ending past it. */

#include "plc_state_manager.h"
#include "scan_cycle_manager.h"
#include "unity.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

PlcTaskCtx *plc_tasks      = NULL;
size_t      plc_task_count = 0;

static scan_cycle_tracker_t tracker;

void setUp(void)
{
    memset(&tracker, 0, sizeof(tracker));
}

void tearDown(void)
{
    scan_cycle_tracker_cleanup(&tracker);
}

/* Busy-wait for at least `us` microseconds of CLOCK_MONOTONIC time. */
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

/* Current CLOCK_MONOTONIC time in nanoseconds. */
static int64_t monotonic_now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + (int64_t)ts.tv_nsec;
}

/* Drives one scan with literal CLOCK_MONOTONIC stamps in nanoseconds. */
static void run_scan(scan_cycle_tracker_t *t, int64_t scheduled_ns, int64_t release_ns,
                     int64_t start_ns, int64_t end_ns)
{
    scan_cycle_tracker_start_at(t, scheduled_ns, release_ns, start_ns);
    scan_cycle_tracker_end_at(t, end_ns);
}

/* Scans of the slow-first-scan scenario, interval 10 ms. */
static const int64_t slow_first_scans[4][4] = {
    {1000000000LL, 1000005000LL, 1000050000LL, 1300050000LL},
    {1310000000LL, 1310004000LL, 1310020000LL, 1312020000LL},
    {1320000000LL, 1320006000LL, 1320030000LL, 1321030000LL},
    {1330000000LL, 1330002000LL, 1330010000LL, 1333010000LL},
};

/* Scans with a one-sample EWMA window, interval 2 s. */
static const int64_t one_sample_window_scans[4][4] = {
    {10000000000LL, 10000005000LL, 10000050000LL, 15000050000LL},
    {16000000000LL, 16000004000LL, 16000020000LL, 16002020000LL},
    {18000000000LL, 18000006000LL, 18000030000LL, 18001030000LL},
    {20000000000LL, 20000002000LL, 20000010000LL, 20003010000LL},
};

/* Runs the stamp-validity scenario with the given scheduled and release stamps on scan 2. */
static void run_invalid_stamp_variant(int64_t scan2_scheduled_ns, int64_t scan2_release_ns)
{
    scan_cycle_tracker_t t;
    memset(&t, 0, sizeof(t));
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&t, 10000000LL));

    run_scan(&t, 1000000000LL, 1000001000LL, 1000010000LL, 1002010000LL);
    run_scan(&t, scan2_scheduled_ns, scan2_release_ns, 1010010000LL, 1040000000LL);
    run_scan(&t, 1050000000LL, 1050001000LL, 1050020000LL, 1051020000LL);
    run_scan(&t, 1060000000LL, 1060001000LL, 1060040000LL, 1061040000LL);

    plc_timing_stats_t out = {0};
    bool valid = scan_cycle_tracker_snapshot(&t, &out);
    scan_cycle_tracker_cleanup(&t);

    TEST_ASSERT_TRUE(valid);
    TEST_ASSERT_EQUAL_INT64(0, out.overruns);
    TEST_ASSERT_EQUAL_INT64(20, out.cycle_latency_min);
    TEST_ASSERT_EQUAL_INT64(40, out.cycle_latency_max);
    TEST_ASSERT_EQUAL_INT64(29990, out.scan_time_max);
    TEST_ASSERT_EQUAL_INT64(40010, out.cycle_time_max);
}

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

void test_snapshot_rejects_null_args(void)
{
    plc_timing_stats_t out = {0};
    TEST_ASSERT_FALSE(scan_cycle_tracker_snapshot(NULL, &out));

    scan_cycle_tracker_init(&tracker, 1000000);
    TEST_ASSERT_FALSE(scan_cycle_tracker_snapshot(&tracker, NULL));
}

void test_first_cycle_records_no_sample(void)
{
    scan_cycle_tracker_init(&tracker, 10000000LL);
    run_scan(&tracker, 1000000000LL, 1000005000LL, 1000050000LL, 1300050000LL);

    plc_timing_stats_t out = {0};
    bool valid = scan_cycle_tracker_snapshot(&tracker, &out);

    TEST_ASSERT_EQUAL_INT64(1, tracker.stats.scan_count);
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.scan_time_min);
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.cycle_time_min);
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.cycle_latency_min);
    TEST_ASSERT_EQUAL_INT64(0, tracker.stats.scan_time_max);
    TEST_ASSERT_EQUAL_INT64(0, tracker.stats.overruns);
    TEST_ASSERT_FALSE(valid);
}

void test_slow_first_scan_counts_no_overrun_and_is_left_out_of_every_metric(void)
{
    scan_cycle_tracker_init(&tracker, 10000000LL);
    for (size_t i = 0; i < 4; ++i)
        run_scan(&tracker, slow_first_scans[i][0], slow_first_scans[i][1], slow_first_scans[i][2],
                 slow_first_scans[i][3]);

    plc_timing_stats_t out = {0};
    bool valid = scan_cycle_tracker_snapshot(&tracker, &out);

    TEST_ASSERT_TRUE(valid);
    TEST_ASSERT_EQUAL_INT64(4, out.scan_count);
    TEST_ASSERT_EQUAL_INT64(0, out.overruns);
    TEST_ASSERT_EQUAL_INT64(1000, out.scan_time_min);
    TEST_ASSERT_EQUAL_INT64(3000, out.scan_time_max);
    TEST_ASSERT_EQUAL_INT64(9980, out.cycle_time_min);
    TEST_ASSERT_EQUAL_INT64(10010, out.cycle_time_max);
    TEST_ASSERT_EQUAL_INT64(10, out.cycle_latency_min);
    TEST_ASSERT_EQUAL_INT64(30, out.cycle_latency_max);
}

void test_transient_overrun_counts_once_and_latency_does_not_accumulate(void)
{
    scan_cycle_tracker_init(&tracker, 10000000LL);
    run_scan(&tracker, 1000000000LL, 1000003000LL, 1000010000LL, 1002010000LL);
    run_scan(&tracker, 1010000000LL, 1010003000LL, 1010010000LL, 1012010000LL);
    run_scan(&tracker, 1020000000LL, 1020003000LL, 1020010000LL, 1045010000LL);
    run_scan(&tracker, 1050000000LL, 1050003000LL, 1050015000LL, 1052015000LL);
    run_scan(&tracker, 1060000000LL, 1060003000LL, 1060012000LL, 1062012000LL);

    TEST_ASSERT_EQUAL_INT64(5, tracker.stats.scan_count);
    TEST_ASSERT_EQUAL_INT64(1, tracker.stats.overruns);
    TEST_ASSERT_EQUAL_INT64(10, tracker.stats.cycle_latency_min);
    TEST_ASSERT_EQUAL_INT64(15, tracker.stats.cycle_latency_max);
    TEST_ASSERT_EQUAL_INT64(2000, tracker.stats.scan_time_min);
    TEST_ASSERT_EQUAL_INT64(25000, tracker.stats.scan_time_max);
    TEST_ASSERT_EQUAL_INT64(9997, tracker.stats.cycle_time_min);
    TEST_ASSERT_EQUAL_INT64(30005, tracker.stats.cycle_time_max);
}

void test_steady_on_time_scans_have_no_overrun_and_non_negative_latency(void)
{
    scan_cycle_tracker_init(&tracker, 10000000LL);
    for (int64_t k = 0; k < 100; ++k)
    {
        int64_t scheduled = 1000000000LL + k * 10000000LL;
        int64_t release   = scheduled + 1000LL;
        int64_t start     = release + (k % 7) * 3000LL;
        run_scan(&tracker, scheduled, release, start, start + 1000000LL);
    }

    TEST_ASSERT_EQUAL_INT64(100, tracker.stats.scan_count);
    TEST_ASSERT_EQUAL_INT64(0, tracker.stats.overruns);
    TEST_ASSERT_EQUAL_INT64(1, tracker.stats.cycle_latency_min);
    TEST_ASSERT_EQUAL_INT64(19, tracker.stats.cycle_latency_max);
    TEST_ASSERT_EQUAL_INT64(1000, tracker.stats.scan_time_min);
    TEST_ASSERT_EQUAL_INT64(1000, tracker.stats.scan_time_max);
    TEST_ASSERT_EQUAL_INT64(9982, tracker.stats.cycle_time_min);
    TEST_ASSERT_EQUAL_INT64(10003, tracker.stats.cycle_time_max);
}

void test_late_release_counts_toward_the_scheduled_deadline(void)
{
    scan_cycle_tracker_init(&tracker, 10000000LL);
    run_scan(&tracker, 1000000000LL, 1000001000LL, 1000010000LL, 1002010000LL);
    run_scan(&tracker, 1010000000LL, 1012000000LL, 1013000000LL, 1021000000LL);

    TEST_ASSERT_EQUAL_INT64(1, tracker.stats.overruns);
    TEST_ASSERT_EQUAL_INT64(3000, tracker.stats.cycle_latency_max);
    TEST_ASSERT_EQUAL_INT64(8000, tracker.stats.scan_time_max);
}

void test_scan_ending_exactly_on_next_tick_is_not_an_overrun(void)
{
    scan_cycle_tracker_init(&tracker, 10000000LL);
    run_scan(&tracker, 1000000000LL, 1000001000LL, 1000010000LL, 1002010000LL);
    run_scan(&tracker, 1010000000LL, 1010000000LL, 1010000000LL, 1020000000LL);

    TEST_ASSERT_EQUAL_INT64(0, tracker.stats.overruns);
}

void test_scan_ending_one_ns_past_next_tick_is_an_overrun(void)
{
    scan_cycle_tracker_init(&tracker, 10000000LL);
    run_scan(&tracker, 1000000000LL, 1000001000LL, 1000010000LL, 1002010000LL);
    run_scan(&tracker, 1010000000LL, 1010000000LL, 1010000000LL, 1020000001LL);

    TEST_ASSERT_EQUAL_INT64(1, tracker.stats.overruns);
}

void test_zero_scheduled_stamp_drops_latency_and_overrun_of_that_scan(void)
{
    run_invalid_stamp_variant(0, 1010001000LL);
}

void test_release_before_scheduled_drops_latency_and_overrun_of_that_scan(void)
{
    run_invalid_stamp_variant(1010000000LL, 1009999000LL);
}

void test_release_after_start_drops_latency_and_overrun_of_that_scan(void)
{
    run_invalid_stamp_variant(1010000000LL, 1010011000LL);
}

void test_snapshot_valid_only_once_scan_cycle_and_latency_each_have_a_sample(void)
{
    plc_timing_stats_t out = {0};
    scan_cycle_tracker_init(&tracker, 10000000LL);

    scan_cycle_tracker_start_at(&tracker, slow_first_scans[0][0], slow_first_scans[0][1],
                                slow_first_scans[0][2]);
    TEST_ASSERT_FALSE(scan_cycle_tracker_snapshot(&tracker, &out));
    scan_cycle_tracker_end_at(&tracker, slow_first_scans[0][3]);
    TEST_ASSERT_FALSE(scan_cycle_tracker_snapshot(&tracker, &out));

    scan_cycle_tracker_start_at(&tracker, slow_first_scans[1][0], slow_first_scans[1][1],
                                slow_first_scans[1][2]);
    TEST_ASSERT_FALSE(scan_cycle_tracker_snapshot(&tracker, &out));
    scan_cycle_tracker_end_at(&tracker, slow_first_scans[1][3]);
    TEST_ASSERT_FALSE(scan_cycle_tracker_snapshot(&tracker, &out));

    scan_cycle_tracker_start_at(&tracker, slow_first_scans[2][0], slow_first_scans[2][1],
                                slow_first_scans[2][2]);
    TEST_ASSERT_TRUE(scan_cycle_tracker_snapshot(&tracker, &out));
    TEST_ASSERT_EQUAL_INT64(3, out.scan_count);
}

void test_averages_equal_latest_sample_with_one_sample_window(void)
{
    scan_cycle_tracker_init(&tracker, 2000000000LL);
    TEST_ASSERT_EQUAL_INT64(1, tracker.avg_window);
    for (size_t i = 0; i < 4; ++i)
        run_scan(&tracker, one_sample_window_scans[i][0], one_sample_window_scans[i][1],
                 one_sample_window_scans[i][2], one_sample_window_scans[i][3]);

    plc_timing_stats_t out = {0};
    TEST_ASSERT_TRUE(scan_cycle_tracker_snapshot(&tracker, &out));
    TEST_ASSERT_EQUAL_INT64(3000, out.scan_time_avg);
    TEST_ASSERT_EQUAL_INT64(1999980, out.cycle_time_avg);
    TEST_ASSERT_EQUAL_INT64(10, out.cycle_latency_avg);
}

void test_stats_response_keeps_json_shape_with_null_entry_for_task_without_samples(void)
{
    static const char expected[] =
        "STATS:{\"tasks\":["
        "{\"name\":\"main\",\"scan_count\":4,\"scan_time_min\":1000,\"scan_time_max\":3000,"
        "\"scan_time_avg\":3000,\"cycle_time_min\":1999980,\"cycle_time_max\":2000010,"
        "\"cycle_time_avg\":1999980,\"cycle_latency_min\":10,\"cycle_latency_max\":30,"
        "\"cycle_latency_avg\":10,\"overruns\":0},"
        "{\"name\":\"init\",\"scan_count\":0,\"scan_time_min\":null,\"scan_time_max\":null,"
        "\"scan_time_avg\":null,\"cycle_time_min\":null,\"cycle_time_max\":null,"
        "\"cycle_time_avg\":null,\"cycle_latency_min\":null,\"cycle_latency_max\":null,"
        "\"cycle_latency_avg\":null,\"overruns\":0}"
        "]}\n";

    static PlcTaskCtx tasks[2];
    memset(tasks, 0, sizeof(tasks));
    snprintf(tasks[0].name, sizeof(tasks[0].name), "%s", "main");
    snprintf(tasks[1].name, sizeof(tasks[1].name), "%s", "init");
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&tasks[0].tracker, 2000000000LL));
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&tasks[1].tracker, 2000000000LL));
    for (size_t i = 0; i < 4; ++i)
        run_scan(&tasks[0].tracker, one_sample_window_scans[i][0], one_sample_window_scans[i][1],
                 one_sample_window_scans[i][2], one_sample_window_scans[i][3]);
    for (size_t i = 0; i < 2; ++i)
        run_scan(&tasks[1].tracker, one_sample_window_scans[i][0], one_sample_window_scans[i][1],
                 one_sample_window_scans[i][2], one_sample_window_scans[i][3]);

    plc_tasks      = tasks;
    plc_task_count = 2;
    char buffer[2048];
    memset(buffer, 0, sizeof(buffer));
    int written = format_timing_stats_response(buffer, sizeof(buffer));
    plc_tasks      = NULL;
    plc_task_count = 0;
    scan_cycle_tracker_cleanup(&tasks[0].tracker);
    scan_cycle_tracker_cleanup(&tasks[1].tracker);

    TEST_ASSERT_EQUAL_STRING(expected, buffer);
    TEST_ASSERT_EQUAL((int)strlen(expected), written);
}

void test_clock_wrappers_measure_against_the_given_scheduled_stamp(void)
{
    scan_cycle_tracker_init(&tracker, 100000000LL);
    for (int i = 0; i < 3; ++i)
    {
        int64_t t = monotonic_now_ns();
        scan_cycle_tracker_start(&tracker, t, t);
        busy_sleep_us(1000);
        scan_cycle_tracker_end(&tracker);
    }

    plc_timing_stats_t out = {0};
    TEST_ASSERT_TRUE(scan_cycle_tracker_snapshot(&tracker, &out));
    TEST_ASSERT_EQUAL_INT64(0, out.overruns);
    TEST_ASSERT_GREATER_OR_EQUAL_INT64(0, out.cycle_latency_min);
    TEST_ASSERT_LESS_OR_EQUAL_INT64(50000, out.cycle_latency_max);
    TEST_ASSERT_GREATER_OR_EQUAL_INT64(500, out.scan_time_min);
}

void test_start_end_null_safe(void)
{
    scan_cycle_tracker_start(NULL, 0, 0);
    scan_cycle_tracker_end(NULL);
    scan_cycle_tracker_start_at(NULL, 0, 0, 0);
    scan_cycle_tracker_end_at(NULL, 0);
    TEST_PASS();
}

void test_cleanup_null_safe(void)
{
    scan_cycle_tracker_cleanup(NULL);
    TEST_PASS();
}
