// SPDX-License-Identifier: MIT

/* Run the production watchdog with a deterministic sleep/state boundary.
 * cc -std=c11 -D_DEFAULT_SOURCE -Wall -Wextra -Werror -Icore/src/plc_app \
 *    tests/host/test_watchdog_progress.c -pthread -o /tmp/watchdog-test
 */
#include <assert.h>
#include <limits.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#define sleep test_sleep
#include "../../core/src/plc_app/utils/watchdog.c"
#undef sleep

static jmp_buf finished;
static int tick, mode, errors;
static PLCState test_state;
PLCState plc_get_state(void) { return test_state; }
void plc_force_error_state(void) { ++errors; test_state = PLC_STATE_ERROR; }
void log_error(const char *format, ...) { (void)format; }

unsigned int test_sleep(unsigned int seconds)
{
    assert(seconds == WATCHDOG_TICK_S);
    ++tick;
    if (mode == 0) { // progress spans unsigned rollover, no wall clock involved
        if (tick > 4) longjmp(finished, 1);
        test_state = PLC_STATE_RUNNING;
        watchdog_feed();
    } else if (mode == 1) { // genuinely stopped dispatcher
        if (tick > 1) longjmp(finished, 1);
        test_state = PLC_STATE_RUNNING;
    } else if (mode == 2) { // STOP -> transition -> RUN; do not reset shared progress
        if (tick > 3) longjmp(finished, 1);
        test_state = tick == 1 ? PLC_STATE_STOPPED :
                     tick == 2 ? PLC_STATE_TRANSITIONING_TO_RUN : PLC_STATE_RUNNING;
        watchdog_feed();
    } else { // original stuck-transition protection is preserved
        if (tick > TRANSITION_STUCK_S / WATCHDOG_TICK_S + 1) longjmp(finished, 1);
        test_state = PLC_STATE_TRANSITIONING_TO_RUN;
    }
    return 0;
}
int main(void)
{
    for (mode = 0; mode < 4; ++mode) {
        tick = errors = 0;
        test_state = PLC_STATE_STOPPED;
        atomic_store(&plc_heartbeat, ULONG_MAX - 1UL);
        if (setjmp(finished) == 0) watchdog_thread(NULL);
        assert(errors == ((mode == 1 || mode == 3) ? 1 : 0));
    }
    puts("PASS: progress/rollover, stalled tick, restart, stuck transition");
    return 0;
}
