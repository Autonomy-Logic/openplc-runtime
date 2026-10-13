// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef PLC_BOOT_H
#define PLC_BOOT_H

#include <stdbool.h>

#include "../drivers/plugin_driver.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Plugin configuration files read at boot, relative to the runtime root. */
#define PLC_BOOT_PLUGINS_CONF "./plugins.conf"
#define PLC_BOOT_VPP_PLUGINS_CONF "./vpp_plugins.conf"

/** Outcome of the boot auto-start decision. */
typedef enum
{
    PLC_BOOT_START_SKIPPED_SAFE_MODE = 0,
    PLC_BOOT_START_HELD_BY_SWITCH,
    PLC_BOOT_START_BEGUN,
    PLC_BOOT_START_FAILED
} plc_boot_start_t;

/**
 * @brief Load and init() the plugins before the boot auto-start decision.
 *
 * Loads plugins.conf and, when @p load_program_plugins is true (not safe mode),
 * the program's vpp_plugins.conf, then calls init() on all of them. Plugin
 * start/stop requests are held from here until plc_boot_autostart() has
 * decided, so a plugin that reports its mode switch from init() only stores the
 * position and plc_boot_autostart() acts on it. Always pair this call with
 * plc_boot_autostart(), which releases the hold. The start transition
 * re-initialises the plugins exactly as before.
 *
 * @return 0 when every step succeeded, -1 otherwise (already logged; the boot
 *         continues, as it did before).
 */
int plc_boot_init_plugins(plugin_driver_t *driver, bool load_program_plugins);

/**
 * @brief Boot auto-start: begin a start unless in safe mode or the switch reads STOP.
 *
 * Must run after plc_boot_init_plugins(), so the switch gate holds the position
 * the plugins reported rather than its RUN default. Releases the plugin request
 * hold on every path. When the switch read STOP, the gate is checked again after
 * the release: a flip to RUN while requests were held then starts the PLC here,
 * since its own request was dropped. A flip to STOP after a begun start is
 * corrected by the switch-movement reconciliation once the start lands.
 */
plc_boot_start_t plc_boot_autostart(bool safe_mode);

#ifdef __cplusplus
}
#endif

#endif // PLC_BOOT_H
