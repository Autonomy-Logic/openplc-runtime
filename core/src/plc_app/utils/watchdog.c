#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "../plc_state_manager.h"
#include "log.h"
#include "utils.h"
#include "watchdog.h"

atomic_long plc_heartbeat;

/* Watchdog loop period, and how long a state change may plausibly take before
 * something is wrong. A start brings plugins up (SPI base scans, fieldbus
 * probes) and a stop joins task threads, so the bound is generous: it is here to
 * catch a transition that will never finish, not to police a slow one. */
#define WATCHDOG_TICK_S      2
#define TRANSITION_STUCK_S   60

void *watchdog_thread(void *arg)
{
    (void)arg;
    long last = atomic_load(&plc_heartbeat);
    int transitioning_ticks = 0;

    while (1)
    {
        sleep(WATCHDOG_TICK_S);

        PLCState current_state = plc_get_state();

        // A transition that never publishes a final state would leave the
        // runtime in TRANSITIONING forever, refusing every command but PING and
        // STATUS — the state is the interlock now, so there is no flag anyone
        // could clear to recover. Every path is meant to land a final state;
        // this is the backstop for the one that doesn't, turning a silent
        // permanent wedge into a reported fault the webserver can act on.
        if (current_state == PLC_STATE_TRANSITIONING_TO_RUN ||
            current_state == PLC_STATE_TRANSITIONING_TO_STOP)
        {
            transitioning_ticks++;
            if (transitioning_ticks * WATCHDOG_TICK_S > TRANSITION_STUCK_S)
            {
                log_error("Watchdog: state change stuck in progress for over %d s — "
                          "forcing ERROR so the runtime accepts commands again",
                          TRANSITION_STUCK_S);
                plc_force_error_state();
                transitioning_ticks = 0;
            }
            continue;
        }
        transitioning_ticks = 0;

        if (current_state != PLC_STATE_RUNNING)
        {
            // Reset tracking when not running so we get a fresh
            // baseline when the PLC starts again
            if (current_state == PLC_STATE_ERROR)
            {
                last = 0;
                atomic_store(&plc_heartbeat, 0);
            }
            continue;
        }

        long now = atomic_load(&plc_heartbeat);
        if (now == last)
        {
            log_error("Watchdog: No heartbeat detected - PLC program is unresponsive");
            log_error("The loaded PLC program may contain an infinite loop. "
                      "Upload a corrected program to recover.");

            // Transition to ERROR state instead of killing the process.
            // This keeps the runtime alive so the webserver can still
            // communicate with it and upload a new program.
            plc_force_error_state();
            continue;
        }

        last = now;
    }

    return NULL;
}

int watchdog_init(void)
{
    pthread_t wd_thread;
    if (pthread_create(&wd_thread, NULL, watchdog_thread, NULL) != 0)
    {
        log_error("Failed to create watchdog thread");
        return -1;
    }
    pthread_detach(wd_thread); // Detach the thread to avoid memory leaks
    return 0;
}
