// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

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

/* Current CLOCK_MONOTONIC time in nanoseconds. */
static int64_t monotonic_now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + (int64_t)ts.tv_nsec;
}

/* Busy-wait for at least `us` microseconds of CLOCK_MONOTONIC time. */
static void busy_sleep_us(int64_t us)
{
    const int64_t t0 = monotonic_now_ns();
    while (monotonic_now_ns() - t0 < us * 1000LL)
    {
    }
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

/* Scenario D: latency decomposition scans with a one-sample EWMA window, interval 2 s. */
static const int64_t decomposition_scans[4][4] = {
    {10000000000LL, 10000009000LL, 10000050000LL, 15000050000LL},
    {16000000000LL, 16000004000LL, 16000020000LL, 16002020000LL},
    {18000000000LL, 18000006000LL, 18000030000LL, 18001030000LL},
    {20000000000LL, 20000002000LL, 20000010000LL, 20003010000LL},
};

/* Scenario T: dispatcher ticks as (deadline, wake) in ns, base tick 2 s. */
static const int64_t dispatcher_ticks[4][2] = {
    {10000000000LL, 10000000000LL},
    {12000000000LL, 12000007000LL},
    {14000000000LL, 14000003000LL},
    {16000000000LL, 16000011000LL},
};

/* Scenario T: cycle_start calls as (begin, end) in ns, one after each tick. */
static const int64_t dispatcher_cycle_starts[4][2] = {
    {10000010000LL, 10000510000LL},
    {12000010000LL, 12000050000LL},
    {14000010000LL, 14000130000LL},
    {16000020000LL, 16000080000LL},
};

/* Feeds scenario T to `ds`, ticks and cycle_start calls interleaved in time order. */
static void feed_dispatcher_scenario(dispatcher_stats_t *ds, bool with_cycle_start)
{
    for (size_t i = 0; i < 4; ++i)
    {
        dispatcher_stats_record_tick(ds, dispatcher_ticks[i][0], dispatcher_ticks[i][1]);
        if (with_cycle_start)
            dispatcher_stats_record_cycle_start(ds, dispatcher_cycle_starts[i][0],
                                                dispatcher_cycle_starts[i][1]);
    }
}

/* Asserts the snapshot of `ds` after scenario T with a one-sample window. */
static void assert_dispatcher_scenario_values(dispatcher_stats_t *ds)
{
    plc_dispatcher_stats_t out;
    memset(&out, 0, sizeof(out));
    TEST_ASSERT_TRUE(dispatcher_stats_snapshot(ds, &out));
    TEST_ASSERT_EQUAL_INT64(4, out.tick_count);
    TEST_ASSERT_EQUAL_INT64(3, out.wake_jitter_samples);
    TEST_ASSERT_EQUAL_INT64(3, out.wake_jitter_min);
    TEST_ASSERT_EQUAL_INT64(11, out.wake_jitter_max);
    TEST_ASSERT_EQUAL_INT64(11, out.wake_jitter_avg);
    TEST_ASSERT_EQUAL_INT64(3, out.cycle_start_samples);
    TEST_ASSERT_EQUAL_INT64(40, out.cycle_start_time_min);
    TEST_ASSERT_EQUAL_INT64(120, out.cycle_start_time_max);
    TEST_ASSERT_EQUAL_INT64(60, out.cycle_start_time_avg);
}

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

void test_latency_decomposes_into_dispatch_delay_and_wake_latency(void)
{
    scan_cycle_tracker_init(&tracker, 2000000000LL);
    for (size_t i = 0; i < 4; ++i)
        run_scan(&tracker, decomposition_scans[i][0], decomposition_scans[i][1],
                 decomposition_scans[i][2], decomposition_scans[i][3]);

    plc_timing_stats_t out;
    memset(&out, 0, sizeof(out));
    TEST_ASSERT_TRUE(scan_cycle_tracker_snapshot(&tracker, &out));
    TEST_ASSERT_EQUAL_INT64(2, out.dispatch_delay_min);
    TEST_ASSERT_EQUAL_INT64(6, out.dispatch_delay_max);
    TEST_ASSERT_EQUAL_INT64(2, out.dispatch_delay_avg);
    TEST_ASSERT_EQUAL_INT64(8, out.wake_latency_min);
    TEST_ASSERT_EQUAL_INT64(24, out.wake_latency_max);
    TEST_ASSERT_EQUAL_INT64(8, out.wake_latency_avg);
    TEST_ASSERT_EQUAL_INT64(10, out.cycle_latency_min);
    TEST_ASSERT_EQUAL_INT64(30, out.cycle_latency_max);
    TEST_ASSERT_EQUAL_INT64(10, out.cycle_latency_avg);
}

void test_init_seeds_decomposition_mins_to_int64_max_and_maxes_to_zero(void)
{
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&tracker, 2000000000LL));
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.dispatch_delay_min);
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.wake_latency_min);
    TEST_ASSERT_EQUAL_INT64(0, tracker.stats.dispatch_delay_max);
    TEST_ASSERT_EQUAL_INT64(0, tracker.stats.wake_latency_max);
}

void test_decomposition_drops_the_first_cycle(void)
{
    scan_cycle_tracker_init(&tracker, 2000000000LL);
    run_scan(&tracker, decomposition_scans[0][0], decomposition_scans[0][1],
             decomposition_scans[0][2], decomposition_scans[0][3]);

    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.dispatch_delay_min);
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, tracker.stats.wake_latency_min);
    TEST_ASSERT_EQUAL_INT64(0, tracker.stats.dispatch_delay_max);
    TEST_ASSERT_EQUAL_INT64(0, tracker.stats.wake_latency_max);

    for (size_t i = 1; i < 4; ++i)
        run_scan(&tracker, decomposition_scans[i][0], decomposition_scans[i][1],
                 decomposition_scans[i][2], decomposition_scans[i][3]);

    TEST_ASSERT_EQUAL_INT64(6, tracker.stats.dispatch_delay_max);
    TEST_ASSERT_EQUAL_INT64(24, tracker.stats.wake_latency_max);
}

void test_invalid_stamps_give_no_decomposition_sample(void)
{
    scan_cycle_tracker_init(&tracker, 10000000LL);
    run_scan(&tracker, 1000000000LL, 1000001000LL, 1000010000LL, 1002010000LL);
    run_scan(&tracker, 1010000000LL, 1010011000LL, 1010010000LL, 1040000000LL);
    run_scan(&tracker, 1050000000LL, 1050001000LL, 1050020000LL, 1051020000LL);
    run_scan(&tracker, 1060000000LL, 1060001000LL, 1060040000LL, 1061040000LL);

    TEST_ASSERT_EQUAL_INT64(1, tracker.stats.dispatch_delay_min);
    TEST_ASSERT_EQUAL_INT64(1, tracker.stats.dispatch_delay_max);
    TEST_ASSERT_EQUAL_INT64(19, tracker.stats.wake_latency_min);
    TEST_ASSERT_EQUAL_INT64(39, tracker.stats.wake_latency_max);
}

void test_steady_scans_have_constant_dispatch_delay_and_non_negative_wake_latency(void)
{
    scan_cycle_tracker_init(&tracker, 10000000LL);
    for (int64_t k = 0; k < 100; ++k)
    {
        int64_t scheduled = 1000000000LL + k * 10000000LL;
        int64_t release   = scheduled + 1000LL;
        int64_t start     = release + (k % 7) * 3000LL;
        run_scan(&tracker, scheduled, release, start, start + 1000000LL);
    }

    TEST_ASSERT_EQUAL_INT64(1, tracker.stats.dispatch_delay_min);
    TEST_ASSERT_EQUAL_INT64(1, tracker.stats.dispatch_delay_max);
    TEST_ASSERT_EQUAL_INT64(0, tracker.stats.wake_latency_min);
    TEST_ASSERT_EQUAL_INT64(18, tracker.stats.wake_latency_max);
    TEST_ASSERT_GREATER_OR_EQUAL_INT64(0, tracker.stats.dispatch_delay_min);
    TEST_ASSERT_GREATER_OR_EQUAL_INT64(0, tracker.stats.wake_latency_min);
}

void test_dispatcher_stats_drop_first_tick_and_first_cycle_start(void)
{
    dispatcher_stats_t ds;
    memset(&ds, 0, sizeof(ds));
    TEST_ASSERT_EQUAL(0, dispatcher_stats_init(&ds));
    dispatcher_stats_reset(&ds, 2000000000LL);
    feed_dispatcher_scenario(&ds, true);

    assert_dispatcher_scenario_values(&ds);
}

void test_dispatcher_stats_drop_invalid_samples_but_count_every_tick(void)
{
    dispatcher_stats_t ds;
    memset(&ds, 0, sizeof(ds));
    TEST_ASSERT_EQUAL(0, dispatcher_stats_init(&ds));
    dispatcher_stats_reset(&ds, 2000000000LL);
    dispatcher_stats_record_tick(&ds, 10000000000LL, 10000000000LL);
    dispatcher_stats_record_tick(&ds, 12000000000LL, 11999999000LL);
    dispatcher_stats_record_tick(&ds, 0, 14000000000LL);
    dispatcher_stats_record_cycle_start(&ds, 10000000000LL, 10000100000LL);
    dispatcher_stats_record_cycle_start(&ds, 12000100000LL, 12000000000LL);

    plc_dispatcher_stats_t out;
    memset(&out, 0, sizeof(out));
    TEST_ASSERT_TRUE(dispatcher_stats_snapshot(&ds, &out));
    TEST_ASSERT_EQUAL_INT64(3, out.tick_count);
    TEST_ASSERT_EQUAL_INT64(0, out.wake_jitter_samples);
    TEST_ASSERT_EQUAL_INT64(0, out.cycle_start_samples);
}

void test_dispatcher_stats_reset_clears_samples_and_rearms_first_sample_exclusion(void)
{
    dispatcher_stats_t ds;
    memset(&ds, 0, sizeof(ds));
    TEST_ASSERT_EQUAL(0, dispatcher_stats_init(&ds));
    dispatcher_stats_reset(&ds, 2000000000LL);
    feed_dispatcher_scenario(&ds, true);
    dispatcher_stats_reset(&ds, 2000000000LL);

    plc_dispatcher_stats_t out;
    memset(&out, 0, sizeof(out));
    TEST_ASSERT_TRUE(dispatcher_stats_snapshot(&ds, &out));
    TEST_ASSERT_EQUAL_INT64(0, out.tick_count);
    TEST_ASSERT_EQUAL_INT64(0, out.wake_jitter_samples);
    TEST_ASSERT_EQUAL_INT64(0, out.cycle_start_samples);
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, out.wake_jitter_min);
    TEST_ASSERT_EQUAL_INT64(INT64_MAX, out.cycle_start_time_min);
    TEST_ASSERT_EQUAL_INT64(0, out.wake_jitter_max);
    TEST_ASSERT_EQUAL_INT64(0, out.cycle_start_time_max);

    feed_dispatcher_scenario(&ds, true);
    assert_dispatcher_scenario_values(&ds);
}

void test_dispatcher_stats_never_initialised_ignores_calls_and_has_no_snapshot(void)
{
    dispatcher_stats_t ds;
    dispatcher_stats_t zero;
    memset(&ds, 0, sizeof(ds));
    memset(&zero, 0, sizeof(zero));

    dispatcher_stats_reset(&ds, 2000000000LL);
    feed_dispatcher_scenario(&ds, true);

    plc_dispatcher_stats_t out;
    memset(&out, 0, sizeof(out));
    TEST_ASSERT_EQUAL_MEMORY(&zero, &ds, sizeof(ds));
    TEST_ASSERT_FALSE(dispatcher_stats_snapshot(&ds, &out));
}

void test_dispatcher_stats_init_rejects_null_and_second_init_keeps_samples(void)
{
    TEST_ASSERT_NOT_EQUAL(0, dispatcher_stats_init(NULL));

    dispatcher_stats_t ds;
    memset(&ds, 0, sizeof(ds));
    TEST_ASSERT_EQUAL(0, dispatcher_stats_init(&ds));
    dispatcher_stats_reset(&ds, 2000000000LL);
    feed_dispatcher_scenario(&ds, true);
    TEST_ASSERT_EQUAL(0, dispatcher_stats_init(&ds));

    assert_dispatcher_scenario_values(&ds);
}

void test_dispatcher_stats_null_safe(void)
{
    dispatcher_stats_t ds;
    memset(&ds, 0, sizeof(ds));
    TEST_ASSERT_EQUAL(0, dispatcher_stats_init(&ds));
    plc_dispatcher_stats_t out;
    memset(&out, 0, sizeof(out));

    dispatcher_stats_reset(NULL, 0);
    dispatcher_stats_record_tick(NULL, 0, 0);
    dispatcher_stats_record_cycle_start(NULL, 0, 0);

    TEST_ASSERT_FALSE(dispatcher_stats_snapshot(NULL, &out));
    TEST_ASSERT_FALSE(dispatcher_stats_snapshot(&ds, NULL));
}

void test_stats_response_appends_decomposition_and_dispatcher_object(void)
{
    static const char expected[] =
        "STATS:{\"tasks\":["
        "{\"name\":\"main\",\"scan_count\":4,\"scan_time_min\":1000,\"scan_time_max\":3000,"
        "\"scan_time_avg\":3000,\"cycle_time_min\":1999980,\"cycle_time_max\":2000010,"
        "\"cycle_time_avg\":1999980,\"cycle_latency_min\":10,\"cycle_latency_max\":30,"
        "\"cycle_latency_avg\":10,\"overruns\":0,\"dispatch_delay_min\":2,"
        "\"dispatch_delay_max\":6,\"dispatch_delay_avg\":2,\"wake_latency_min\":8,"
        "\"wake_latency_max\":24,\"wake_latency_avg\":8},"
        "{\"name\":\"init\",\"scan_count\":2,\"scan_time_min\":null,\"scan_time_max\":null,"
        "\"scan_time_avg\":null,\"cycle_time_min\":null,\"cycle_time_max\":null,"
        "\"cycle_time_avg\":null,\"cycle_latency_min\":null,\"cycle_latency_max\":null,"
        "\"cycle_latency_avg\":null,\"overruns\":0,\"dispatch_delay_min\":null,"
        "\"dispatch_delay_max\":null,\"dispatch_delay_avg\":null,\"wake_latency_min\":null,"
        "\"wake_latency_max\":null,\"wake_latency_avg\":null}"
        "],\"dispatcher\":{\"tick_count\":4,\"wake_jitter_min\":3,\"wake_jitter_max\":11,"
        "\"wake_jitter_avg\":11,\"cycle_start_time_min\":40,\"cycle_start_time_max\":120,"
        "\"cycle_start_time_avg\":60}}\n";

    TEST_ASSERT_EQUAL(0, dispatcher_stats_init(&plc_dispatcher_stats));
    dispatcher_stats_reset(&plc_dispatcher_stats, 2000000000LL);
    feed_dispatcher_scenario(&plc_dispatcher_stats, true);

    static PlcTaskCtx tasks[2];
    memset(tasks, 0, sizeof(tasks));
    snprintf(tasks[0].name, sizeof(tasks[0].name), "%s", "main");
    snprintf(tasks[1].name, sizeof(tasks[1].name), "%s", "init");
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&tasks[0].tracker, 2000000000LL));
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&tasks[1].tracker, 2000000000LL));
    for (size_t i = 0; i < 4; ++i)
        run_scan(&tasks[0].tracker, decomposition_scans[i][0], decomposition_scans[i][1],
                 decomposition_scans[i][2], decomposition_scans[i][3]);
    for (size_t i = 0; i < 2; ++i)
        run_scan(&tasks[1].tracker, decomposition_scans[i][0], decomposition_scans[i][1],
                 decomposition_scans[i][2], decomposition_scans[i][3]);

    plc_tasks      = tasks;
    plc_task_count = 2;
    char buffer[4096];
    memset(buffer, 0, sizeof(buffer));
    int written = format_timing_stats_response(buffer, sizeof(buffer));
    plc_tasks      = NULL;
    plc_task_count = 0;
    scan_cycle_tracker_cleanup(&tasks[0].tracker);
    scan_cycle_tracker_cleanup(&tasks[1].tracker);
    dispatcher_stats_reset(&plc_dispatcher_stats, 2000000000LL);

    TEST_ASSERT_EQUAL_STRING(expected, buffer);
    TEST_ASSERT_EQUAL((int)strlen(expected), written);
}

void test_stats_response_reports_dispatcher_metrics_null_independently(void)
{
    static const char expected_after_reset[] =
        "STATS:{\"tasks\":[],\"dispatcher\":{\"tick_count\":0,\"wake_jitter_min\":null,"
        "\"wake_jitter_max\":null,\"wake_jitter_avg\":null,\"cycle_start_time_min\":null,"
        "\"cycle_start_time_max\":null,\"cycle_start_time_avg\":null}}\n";
    static const char expected_ticks_only[] =
        "STATS:{\"tasks\":[],\"dispatcher\":{\"tick_count\":4,\"wake_jitter_min\":3,"
        "\"wake_jitter_max\":11,\"wake_jitter_avg\":11,\"cycle_start_time_min\":null,"
        "\"cycle_start_time_max\":null,\"cycle_start_time_avg\":null}}\n";

    plc_tasks      = NULL;
    plc_task_count = 0;
    TEST_ASSERT_EQUAL(0, dispatcher_stats_init(&plc_dispatcher_stats));
    dispatcher_stats_reset(&plc_dispatcher_stats, 2000000000LL);

    char after_reset[4096];
    memset(after_reset, 0, sizeof(after_reset));
    int written_after_reset = format_timing_stats_response(after_reset, sizeof(after_reset));

    feed_dispatcher_scenario(&plc_dispatcher_stats, false);
    char ticks_only[4096];
    memset(ticks_only, 0, sizeof(ticks_only));
    int written_ticks_only = format_timing_stats_response(ticks_only, sizeof(ticks_only));
    dispatcher_stats_reset(&plc_dispatcher_stats, 2000000000LL);

    TEST_ASSERT_EQUAL_STRING(expected_after_reset, after_reset);
    TEST_ASSERT_EQUAL((int)strlen(expected_after_reset), written_after_reset);
    TEST_ASSERT_EQUAL_STRING(expected_ticks_only, ticks_only);
    TEST_ASSERT_EQUAL((int)strlen(expected_ticks_only), written_ticks_only);
}

/* Scans whose second one ends 1 ms past its next tick, interval 10 ms. */
static const int64_t second_scan_overrun_scans[2][4] = {
    {1000000000LL, 1000001000LL, 1000010000LL, 1002010000LL},
    {1010000000LL, 1012000000LL, 1013000000LL, 1021000000LL},
};

/* Scans with no scheduled stamp, so no latency sample is ever taken, interval 10 ms. */
static const int64_t unstamped_scans[4][4] = {
    {0, 1000001000LL, 1000010000LL, 1002010000LL},
    {0, 1010001000LL, 1010010000LL, 1012010000LL},
    {0, 1020001000LL, 1020010000LL, 1022010000LL},
    {0, 1030001000LL, 1030010000LL, 1032010000LL},
};

/* STATS response with one task whose metrics are all null, after a dispatcher reset. */
#define NULL_METRICS_RESPONSE(name, scan_count, overruns)                                          \
    "STATS:{\"tasks\":["                                                                           \
    "{\"name\":\"" name "\",\"scan_count\":" scan_count ",\"scan_time_min\":null,"                 \
    "\"scan_time_max\":null,\"scan_time_avg\":null,\"cycle_time_min\":null,"                       \
    "\"cycle_time_max\":null,\"cycle_time_avg\":null,\"cycle_latency_min\":null,"                  \
    "\"cycle_latency_max\":null,\"cycle_latency_avg\":null,\"overruns\":" overruns ","             \
    "\"dispatch_delay_min\":null,\"dispatch_delay_max\":null,\"dispatch_delay_avg\":null,"         \
    "\"wake_latency_min\":null,\"wake_latency_max\":null,\"wake_latency_avg\":null}"               \
    "],\"dispatcher\":{\"tick_count\":0,\"wake_jitter_min\":null,\"wake_jitter_max\":null,"        \
    "\"wake_jitter_avg\":null,\"cycle_start_time_min\":null,\"cycle_start_time_max\":null,"        \
    "\"cycle_start_time_avg\":null}}\n"

/* Formats the STATS response for one task that ran `n` of `scans`; returns the bytes written. */
static int format_single_task_response(const char *name, int64_t interval_ns,
                                       const int64_t (*scans)[4], size_t n, char *buffer,
                                       size_t buffer_size)
{
    TEST_ASSERT_EQUAL(0, dispatcher_stats_init(&plc_dispatcher_stats));
    dispatcher_stats_reset(&plc_dispatcher_stats, interval_ns);

    static PlcTaskCtx task;
    memset(&task, 0, sizeof(task));
    snprintf(task.name, sizeof(task.name), "%s", name);
    TEST_ASSERT_EQUAL(0, scan_cycle_tracker_init(&task.tracker, interval_ns));
    for (size_t i = 0; i < n; ++i)
        run_scan(&task.tracker, scans[i][0], scans[i][1], scans[i][2], scans[i][3]);

    plc_tasks      = &task;
    plc_task_count = 1;
    memset(buffer, 0, buffer_size);
    int written = format_timing_stats_response(buffer, buffer_size);
    plc_tasks      = NULL;
    plc_task_count = 0;
    scan_cycle_tracker_cleanup(&task.tracker);
    return written;
}

void test_stats_entry_after_one_scan_reports_that_scan_with_null_metrics(void)
{
    static const char expected[] = NULL_METRICS_RESPONSE("init", "1", "0");
    char buffer[4096];
    int written = format_single_task_response("init", 10000000LL, slow_first_scans, 1, buffer,
                                              sizeof(buffer));

    TEST_ASSERT_EQUAL_STRING(expected, buffer);
    TEST_ASSERT_EQUAL((int)strlen(expected), written);
}

void test_stats_entry_before_a_cycle_time_sample_reports_real_overruns(void)
{
    static const char expected[] = NULL_METRICS_RESPONSE("init", "2", "1");
    char buffer[4096];
    int written = format_single_task_response("init", 10000000LL, second_scan_overrun_scans, 2,
                                              buffer, sizeof(buffer));

    TEST_ASSERT_EQUAL_STRING(expected, buffer);
    TEST_ASSERT_EQUAL((int)strlen(expected), written);
}

void test_stats_entry_without_a_latency_sample_reports_every_scan_with_null_metrics(void)
{
    static const char expected[] = NULL_METRICS_RESPONSE("unstamped", "4", "0");
    char buffer[4096];
    int written = format_single_task_response("unstamped", 10000000LL, unstamped_scans, 4, buffer,
                                              sizeof(buffer));

    TEST_ASSERT_EQUAL_STRING(expected, buffer);
    TEST_ASSERT_EQUAL((int)strlen(expected), written);
}

void test_stats_entry_of_a_task_with_no_scan_reports_zero_counts_and_null_metrics(void)
{
    static const char expected[] = NULL_METRICS_RESPONSE("idle", "0", "0");
    char buffer[4096];
    int written = format_single_task_response("idle", 10000000LL, slow_first_scans, 0, buffer,
                                              sizeof(buffer));

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
