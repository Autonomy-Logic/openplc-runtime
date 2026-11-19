// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file plugin_plc_requests.c
 * @brief Runtime side of the plugin run/stop requests and the per-slot switch reporters.
 *
 * Kept apart from plugin_driver.c so the request gate links without Python
 * or dlopen.
 */

#include "plugin_plc_requests.h"

#include <pthread.h>
#include <stdatomic.h>
#include <string.h>

#include "../plc_app/plc_state_manager.h"
#include "../plc_app/plc_switch.h"
#include "../plc_app/unix_socket.h"
#include "../plc_app/utils/log.h"
#include "plugin_driver.h"

/* While set, plugin start/stop requests are dropped; the boot decides the
 * transition itself once the plugins have reported. */
static atomic_bool plc_requests_held = false;

void plugin_driver_hold_plc_requests(bool hold)
{
    atomic_store(&plc_requests_held, hold);
}

// Plugin-invoked async PLC stop. Routes through the same transition
// path as the socket STOP; non-blocking. The claim is atomic under the
// state lock, so no pre-check here.
void plugin_request_plc_stop(const char *reason)
{
    if (atomic_load(&plc_requests_held))
    {
        log_info("[PLUGIN] stop request not acted on during plugin bring-up (%s)",
                 reason ? reason : "(no reason given)");
        return;
    }

    log_info("[PLUGIN] stop requested: %s", reason ? reason : "(no reason given)");
    if (!plc_begin_transition(PLC_STATE_STOPPED))
    {
        // Already stopping/stopped, or another transition is in flight;
        // the switch-movement reconciliation picks it up later (see
        // transition_worker in unix_socket.c), so a switch-driven stop cannot be
        // lost here.
        log_warn("[PLUGIN] stop request collapsed (already transitioning or not running)");
    }
}

// Plugin-invoked PLC start. Gated on the hardware mode switch: a plugin
// cannot start a PLC whose switch reads STOP or whose position is unknown.
void plugin_request_plc_start(const char *reason)
{
    if (atomic_load(&plc_requests_held))
    {
        log_info("[PLUGIN] start request not acted on during plugin bring-up (%s)",
                 reason ? reason : "(no reason given)");
        return;
    }

    const plc_switch_gate_t gate = plc_switch_gate();
    if (gate == PLC_SWITCH_GATE_STOP)
    {
        log_warn("[PLUGIN] start request refused — mode switch is in STOP (%s)",
                 reason ? reason : "(no reason given)");
        return;
    }
    if (gate == PLC_SWITCH_GATE_UNKNOWN)
    {
        log_warn("[PLUGIN] start request refused — no switch owner has reported a position "
                 "since its last init (%s)",
                 reason ? reason : "(no reason given)");
        return;
    }

    log_info("[PLUGIN] start requested: %s", reason ? reason : "(no reason given)");
    if (!plc_begin_transition(PLC_STATE_RUNNING))
    {
        log_warn("[PLUGIN] start request collapsed (already transitioning or already running)");
    }
}

static char slot_ids[MAX_PLUGINS][PLC_SWITCH_OWNER_ID_SIZE];
static pthread_mutex_t slot_ids_lock = PTHREAD_MUTEX_INITIALIZER;

/** Reports @p position under the identity bound to @p slot. */
static void report_from_slot(int slot, int position)
{
    char id[PLC_SWITCH_OWNER_ID_SIZE];
    pthread_mutex_lock(&slot_ids_lock);
    memcpy(id, slot_ids[slot], sizeof(id));
    pthread_mutex_unlock(&slot_ids_lock);

    if (id[0] == '\0')
    {
        log_warn("[PLUGIN] switch position from unbound plugin slot %d ignored", slot);
        return;
    }
    plc_switch_owner_report(id, position == PLC_SWITCH_STOP ? PLC_SWITCH_STOP : PLC_SWITCH_RUN);
}

/** Defines the static reporter of slot @p n. */
#define SLOT_REPORTER(n)                                                                           \
    static void slot_reporter_##n(int position)                                                    \
    {                                                                                              \
        report_from_slot(n, position);                                                             \
    }

SLOT_REPORTER(0)
SLOT_REPORTER(1)
SLOT_REPORTER(2)
SLOT_REPORTER(3)
SLOT_REPORTER(4)
SLOT_REPORTER(5)
SLOT_REPORTER(6)
SLOT_REPORTER(7)
SLOT_REPORTER(8)
SLOT_REPORTER(9)
SLOT_REPORTER(10)
SLOT_REPORTER(11)
SLOT_REPORTER(12)
SLOT_REPORTER(13)
SLOT_REPORTER(14)
SLOT_REPORTER(15)

static const plugin_switch_reporter_t slot_reporters[] = {
    slot_reporter_0,  slot_reporter_1,  slot_reporter_2,  slot_reporter_3,
    slot_reporter_4,  slot_reporter_5,  slot_reporter_6,  slot_reporter_7,
    slot_reporter_8,  slot_reporter_9,  slot_reporter_10, slot_reporter_11,
    slot_reporter_12, slot_reporter_13, slot_reporter_14, slot_reporter_15,
};

_Static_assert(sizeof(slot_reporters) / sizeof(slot_reporters[0]) == MAX_PLUGINS,
               "one switch reporter per plugin slot");

plugin_switch_reporter_t plugin_switch_reporter_bind(int slot, const char *id)
{
    if (slot < 0 || slot >= MAX_PLUGINS)
        return NULL;

    pthread_mutex_lock(&slot_ids_lock);
    memset(slot_ids[slot], 0, sizeof(slot_ids[slot]));
    if (id)
        strncpy(slot_ids[slot], id, sizeof(slot_ids[slot]) - 1);
    pthread_mutex_unlock(&slot_ids_lock);
    return slot_reporters[slot];
}
