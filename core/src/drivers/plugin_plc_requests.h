// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#ifndef PLUGIN_PLC_REQUESTS_H
#define PLUGIN_PLC_REQUESTS_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Drop plugin request_plc_start / request_plc_stop calls while @p hold is true.
 *
 * Held across the boot-time plugin init and the boot auto-start decision, so a
 * plugin reporting its mode switch cannot start a transition that would tear
 * down plugins still being initialised, nor race the auto-start. Switch
 * positions are still reported while held.
 */
void plugin_driver_hold_plc_requests(bool hold);

/** Runtime side of plugin_runtime_args_t.request_plc_stop. */
void plugin_request_plc_stop(const char *reason);

/**
 * Runtime side of plugin_runtime_args_t.request_plc_start; gated on the mode switch.
 * Refused while plc_switch_gate() reads STOP, or UNKNOWN because a switch owner has
 * not reported since its last init. Non-blocking.
 */
void plugin_request_plc_start(const char *reason);

/** A slot's plugin_runtime_args_t.set_switch_position entry point (0 = STOP, else RUN). */
typedef void (*plugin_switch_reporter_t)(int position);

/**
 * @brief Attribute plugin slot @p slot's switch reports to identity @p id.
 *
 * Each plugin slot has its own static reporter, so a report reaches
 * plc_switch_owner_report() under the identity of the plugin that made it. The
 * binding lasts until the slot is bound again by the next init() in that slot.
 *
 * @param slot index into plugin_driver_t.plugins, below MAX_PLUGINS
 * @param id   plugin identity, "<config name>|<path>"
 * @return the slot's reporter, or NULL when @p slot is out of range
 */
plugin_switch_reporter_t plugin_switch_reporter_bind(int slot, const char *id);

#ifdef __cplusplus
}
#endif

#endif // PLUGIN_PLC_REQUESTS_H
