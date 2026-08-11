/**
 * @file fakevpp_plugin.c
 * @brief Test-only native plugin standing in for a board's VPP package.
 *
 * Exists to make three otherwise-unreachable conditions reproducible:
 *
 *   1. A SLOW START. init() sleeps FAKEVPP_INIT_MS (default 2500 ms), which is
 *      what a real board spends on SPI base scans and fieldbus probes. Without
 *      it, bring-up in a container lands in ~150 ms and there is no window to
 *      aim a signal or a competing request at.
 *
 *   2. A MODE SWITCH. A watcher thread polls FAKEVPP_SWITCH_FILE (default
 *      /tmp/fakevpp_switch) for the text "RUN" or "STOP" and drives the runtime
 *      exactly as the plugin contract prescribes: store the position first, then
 *      request the matching transition. Writing that file is the test's way of
 *      flipping a physical switch.
 *
 *   3. A RUNAWAY-FREE STOP. stop_loop()/cleanup() return promptly, so a slow
 *      teardown never confuses a slow start.
 *
 * Everything is env-driven so one .so covers every scenario, and nothing here
 * touches the process image -- it declares no located variables.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* Keep in step with plugin_types.h. Only the tail matters here, but the whole
 * prefix has to be laid out identically for the offsets to line up, so this
 * includes the real header rather than re-declaring the struct. */
#include "plugin_types.h"

#define SWITCH_RUN  1
#define SWITCH_STOP 0

static plugin_runtime_args_t *g_args      = NULL;
static atomic_int             g_running    = 0;
static pthread_t             g_watcher;
static int                   g_watcher_up = 0;
static int                   g_last_pos   = SWITCH_RUN;

static void plog(const char *level, const char *msg)
{
    if (g_args && g_args->log_info && strcmp(level, "info") == 0)
        g_args->log_info(msg);
    else if (g_args && g_args->log_warn && strcmp(level, "warn") == 0)
        g_args->log_warn(msg);
    else
        fprintf(stderr, "[FAKEVPP] %s: %s\n", level, msg);
}

static long env_long(const char *name, long fallback)
{
    const char *v = getenv(name);
    if (!v || !*v)
        return fallback;
    return atol(v);
}

static const char *switch_file(void)
{
    const char *v = getenv("FAKEVPP_SWITCH_FILE");
    return (v && *v) ? v : "/tmp/fakevpp_switch";
}

/* Read the switch file. Absent or unparseable means RUN, matching the runtime's
 * own default for a device with no switch-aware plugin. */
static int read_switch_file(void)
{
    FILE *f = fopen(switch_file(), "r");
    if (!f)
        return SWITCH_RUN;
    char buf[16] = {0};
    if (!fgets(buf, sizeof(buf), f))
    {
        fclose(f);
        return SWITCH_RUN;
    }
    fclose(f);
    return (strncmp(buf, "STOP", 4) == 0) ? SWITCH_STOP : SWITCH_RUN;
}

static void *watcher_thread(void *arg)
{
    (void)arg;
    const long poll_ms = env_long("FAKEVPP_POLL_MS", 5);
    struct timespec t  = { .tv_sec = poll_ms / 1000, .tv_nsec = (poll_ms % 1000) * 1000000L };

    while (atomic_load(&g_running))
    {
        const int pos = read_switch_file();
        if (pos != g_last_pos)
        {
            g_last_pos = pos;
            /* Contract order: store the position BEFORE requesting, so the start
             * gate never reads a stale STOP on a rising edge and no start can slip
             * in on a falling one. */
            if (g_args && g_args->set_switch_position)
                g_args->set_switch_position(pos);

            if (pos == SWITCH_RUN)
            {
                plog("info", "fake mode switch moved to RUN");
                if (g_args && g_args->request_plc_start)
                    g_args->request_plc_start("fake mode switch moved to RUN");
            }
            else
            {
                plog("info", "fake mode switch moved to STOP");
                if (g_args && g_args->request_plc_stop)
                    g_args->request_plc_stop("fake mode switch moved to STOP");
            }
        }
        nanosleep(&t, NULL);
    }
    return NULL;
}

int init(void *args)
{
    g_args = (plugin_runtime_args_t *)args;

    /* Adopt whatever the switch file already says before anything else, so a test
     * can boot a device with the switch in STOP. */
    g_last_pos = read_switch_file();
    if (g_args && g_args->set_switch_position)
        g_args->set_switch_position(g_last_pos);

    const long slow_ms = env_long("FAKEVPP_INIT_MS", 2500);
    if (slow_ms > 0)
    {
        char msg[128];
        snprintf(msg, sizeof(msg), "fake VPP bring-up: sleeping %ld ms in init()", slow_ms);
        plog("info", msg);
        struct timespec t = { .tv_sec = slow_ms / 1000, .tv_nsec = (slow_ms % 1000) * 1000000L };
        nanosleep(&t, NULL);
    }

    plog("info", "fake VPP initialised");
    return 0;
}

/* The watcher lives between start_loop and stop_loop, and NOT a moment longer.
 *
 * init() is the tempting place for it -- a real mode switch has to be watched
 * while the PLC is stopped too -- but the contract forbids threads there for a
 * concrete reason: plugin_driver_update_config tears every slot down and
 * re-dlopens it on each start, so a thread left running from init() ends up
 * executing code that has been unmapped. That is a SIGSEGV, and this fixture
 * earned one before being written this way. Everything the tests need still
 * works, because the runtime stops plugins only AFTER joining the PLC thread:
 * a flip during a stop is still seen. */
int start_loop(void *args)
{
    (void)args;
    if (atomic_exchange(&g_running, 1) == 1)
        return 0;
    if (pthread_create(&g_watcher, NULL, watcher_thread, NULL) != 0)
    {
        atomic_store(&g_running, 0);
        plog("warn", "failed to start the switch watcher");
        return -1;
    }
    g_watcher_up = 1;
    plog("info", "fake VPP switch watcher running");
    return 0;
}

int stop_loop(void *args)
{
    (void)args;

    /* Optional slow teardown, BEFORE the watcher is joined, so the switch is
     * still being watched while the stop is in flight. That is the only way to
     * land a flip inside a stop transition on purpose: a stop is otherwise tens
     * of milliseconds and there is nothing to aim at. */
    const long slow_ms = env_long("FAKEVPP_STOP_MS", 0);
    if (slow_ms > 0)
    {
        char msg[128];
        snprintf(msg, sizeof(msg), "fake VPP teardown: sleeping %ld ms in stop_loop()", slow_ms);
        plog("info", msg);
        struct timespec t = { .tv_sec = slow_ms / 1000, .tv_nsec = (slow_ms % 1000) * 1000000L };
        nanosleep(&t, NULL);
    }

    if (atomic_exchange(&g_running, 0) == 1 && g_watcher_up)
    {
        pthread_join(g_watcher, NULL);
        g_watcher_up = 0;
    }
    return 0;
}

int cleanup(void *args)
{
    (void)args;
    if (atomic_exchange(&g_running, 0) == 1 && g_watcher_up)
    {
        pthread_join(g_watcher, NULL);
        g_watcher_up = 0;
    }
    g_args = NULL;
    return 0;
}
