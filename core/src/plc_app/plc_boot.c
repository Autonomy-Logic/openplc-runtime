// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file plc_boot.c
 * @brief Boot-time plugin bring-up and the boot auto-start decision.
 *
 * A plugin that owns a physical run/stop switch reports its position from
 * init(). Every such plugin, including the ones the program ships in
 * vpp_plugins.conf, is therefore initialised here, before the auto-start reads
 * the switch gate, so a device powered up in STOP never runs a scan.
 */

#include "plc_boot.h"

#include "../drivers/plugin_plc_requests.h"
#include "plc_state_manager.h"
#include "plc_switch.h"
#include "unix_socket.h"
#include "utils/log.h"

int plc_boot_init_plugins(plugin_driver_t *driver, bool load_program_plugins)
{
    if (!driver)
    {
        return -1;
    }

    int rc = 0;
    plugin_driver_hold_plc_requests(true);

    if (plugin_driver_load_config(driver, PLC_BOOT_PLUGINS_CONF) != 0)
    {
        log_error("[PLUGIN]: Failed to load plugin configuration");
        rc = -1;
    }
    else
    {
        if (load_program_plugins &&
            plugin_driver_append_config(driver, PLC_BOOT_VPP_PLUGINS_CONF) != 0)
        {
            log_error("[PLUGIN]: VPP plugin configuration could not be loaded at boot");
            rc = -1;
        }
        if (plugin_driver_init(driver) != 0)
        {
            log_error("[PLUGIN]: Plugin init failed at boot");
            rc = -1;
        }
        else
        {
            log_info("[PLUGIN]: All plugins initialized (not started)");
        }
    }

    /* Released by plc_boot_autostart(), once the start is decided. */
    return rc;
}

static plc_boot_start_t decide_autostart(bool safe_mode)
{
    if (safe_mode)
    {
        return PLC_BOOT_START_SKIPPED_SAFE_MODE;
    }

    if (!plc_switch_allows_run())
    {
        return PLC_BOOT_START_HELD_BY_SWITCH;
    }

    if (!plc_begin_transition(PLC_STATE_RUNNING))
    {
        log_error("Failed to initiate PLC start");
        return PLC_BOOT_START_FAILED;
    }
    return PLC_BOOT_START_BEGUN;
}

plc_boot_start_t plc_boot_autostart(bool safe_mode)
{
    plc_boot_start_t result = decide_autostart(safe_mode);
    plugin_driver_hold_plc_requests(false);

    if (result != PLC_BOOT_START_HELD_BY_SWITCH)
    {
        return result;
    }

    /* A flip to RUN after the gate was read had its request dropped by the hold;
     * the stored position is all that is left of it, so act on it here. */
    if (plc_switch_allows_run())
    {
        log_info("Hardware mode switch moved to RUN during boot - starting the PLC");
        if (!plc_begin_transition(PLC_STATE_RUNNING))
        {
            log_warn("Boot start after the switch moved to RUN collapsed (already transitioning)");
            return PLC_BOOT_START_FAILED;
        }
        return PLC_BOOT_START_BEGUN;
    }

    log_info("Hardware mode switch is in STOP - PLC left stopped");
    log_info("Move the switch to RUN to start the PLC");
    return PLC_BOOT_START_HELD_BY_SWITCH;
}
