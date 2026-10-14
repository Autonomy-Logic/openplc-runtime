// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file plugin_plc_requests.c
 * @brief Runtime side of the plugin run/stop requests and the switch report.
 *
 * Kept apart from plugin_driver.c so the request gate links without Python
 * or dlopen.
 */

#include "plugin_plc_requests.h"

#include <stdatomic.h>

#include "../plc_app/plc_state_manager.h"
#include "../plc_app/plc_switch.h"
#include "../plc_app/unix_socket.h"
#include "../plc_app/utils/log.h"

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
// cannot start a PLC whose switch reads STOP.
void plugin_request_plc_start(const char *reason)
{
    if (atomic_load(&plc_requests_held))
    {
        log_info("[PLUGIN] start request not acted on during plugin bring-up (%s)",
                 reason ? reason : "(no reason given)");
        return;
    }

    if (!plc_switch_allows_run())
    {
        log_warn("[PLUGIN] start request refused — mode switch is in STOP (%s)",
                 reason ? reason : "(no reason given)");
        return;
    }

    log_info("[PLUGIN] start requested: %s", reason ? reason : "(no reason given)");
    if (!plc_begin_transition(PLC_STATE_RUNNING))
    {
        log_warn("[PLUGIN] start request collapsed (already transitioning or already running)");
    }
}

// Thin adapter so plugin_types.h can declare the position as a plain int and
// stay free of the plc_app include tree.
void plugin_set_switch_position(int position)
{
    plc_set_switch_position(position == PLC_SWITCH_STOP ? PLC_SWITCH_STOP : PLC_SWITCH_RUN);
}
