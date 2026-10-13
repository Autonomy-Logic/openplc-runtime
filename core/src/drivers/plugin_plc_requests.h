// SPDX-License-Identifier: MIT
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
 * positions are still stored while held.
 */
void plugin_driver_hold_plc_requests(bool hold);

/** Runtime side of plugin_runtime_args_t.request_plc_stop. */
void plugin_request_plc_stop(const char *reason);

/** Runtime side of plugin_runtime_args_t.request_plc_start; gated on the mode switch. */
void plugin_request_plc_start(const char *reason);

/** Runtime side of plugin_runtime_args_t.set_switch_position (0 = STOP, else RUN). */
void plugin_set_switch_position(int position);

#ifdef __cplusplus
}
#endif

#endif // PLUGIN_PLC_REQUESTS_H
