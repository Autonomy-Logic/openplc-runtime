// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Host test: boot plugin bring-up and the boot auto-start against the mode switch.
// Links the real plc_boot.c and plc_switch.c; the plugin driver and the
// transition entry point are faked here.

#include "plc_boot.h"
#include "plc_state_manager.h"
#include "plc_switch.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

static int g_failures = 0;

#define CHECK(cond, what)                                                                          \
    do                                                                                             \
    {                                                                                              \
        if (!(cond))                                                                               \
        {                                                                                          \
            std::printf("  FAIL  %s\n", what);                                                     \
            g_failures++;                                                                          \
        }                                                                                          \
    } while (0)

/* ---- scenario and observations ------------------------------------------ */

enum class SwitchPlugin
{
    None,
    ReportsStop,
    ReportsRun
};

static SwitchPlugin g_switch_plugin; // the switch-aware plugin, shipped in vpp_plugins.conf
static bool g_vpp_loaded;
static bool g_held;
static bool g_request_while_not_held;
static std::vector<std::string> g_calls;
static std::vector<PLCState> g_transitions; // every transition the runtime began
static bool g_running;                     // a start was begun: the first scan would follow
static int g_scans;

static void reset(SwitchPlugin plugin)
{
    g_switch_plugin          = plugin;
    g_vpp_loaded             = false;
    g_held                   = false;
    g_request_while_not_held = false;
    g_calls.clear();
    g_transitions.clear();
    g_running = false;
    g_scans   = 0;
    /* Back to the power-on default with no movement on record. */
    plc_set_switch_position(PLC_SWITCH_RUN);
    (void)plc_switch_take_movement();
}

/* ---- fakes: transition entry point and logging -------------------------- */

extern "C" bool plc_begin_transition(PLCState target)
{
    g_transitions.push_back(target);
    if (target == PLC_STATE_RUNNING)
    {
        g_running = true;
        g_scans++; // stands in for the scan that a begun start always reaches
    }
    else
    {
        g_running = false;
    }
    return true;
}

extern "C" void log_info(const char *, ...) {}
extern "C" void log_debug(const char *, ...) {}
extern "C" void log_warn(const char *, ...) {}
extern "C" void log_error(const char *, ...) {}

/* The runtime's request_plc_start / request_plc_stop, as a plugin sees them. */
static void request_plc_start(void)
{
    if (g_held)
        return;
    g_request_while_not_held = true;
    if (plc_switch_allows_run())
        plc_begin_transition(PLC_STATE_RUNNING);
}

static void request_plc_stop(void)
{
    if (g_held)
        return;
    g_request_while_not_held = true;
    if (g_running)
        plc_begin_transition(PLC_STATE_STOPPED);
}

/* A switch-aware plugin's init(): store the position, then request the matching transition. */
static void switch_plugin_init(void)
{
    if (g_switch_plugin == SwitchPlugin::ReportsStop)
    {
        plc_set_switch_position(PLC_SWITCH_STOP);
        request_plc_stop();
    }
    else if (g_switch_plugin == SwitchPlugin::ReportsRun)
    {
        plc_set_switch_position(PLC_SWITCH_RUN);
        request_plc_start();
    }
}

/* ---- fakes: plugin driver ----------------------------------------------- */

extern "C" void plugin_driver_hold_plc_requests(bool hold)
{
    g_held = hold;
    g_calls.push_back(hold ? "hold" : "release");
}

extern "C" int plugin_driver_load_config(plugin_driver_t *, const char *config_file)
{
    g_calls.push_back(std::string("load:") + config_file);
    return 0;
}

extern "C" int plugin_driver_append_config(plugin_driver_t *, const char *config_file)
{
    g_calls.push_back(std::string("append:") + config_file);
    g_vpp_loaded = true;
    return 0;
}

extern "C" int plugin_driver_init(plugin_driver_t *)
{
    g_calls.push_back("init");
    if (g_vpp_loaded)
        switch_plugin_init();
    return 0;
}

/* ---- cases -------------------------------------------------------------- */

static plugin_driver_t g_driver;

static plc_boot_start_t boot(bool safe_mode)
{
    plc_boot_init_plugins(&g_driver, !safe_mode);
    return plc_boot_autostart(safe_mode);
}

static void test_switch_stop_at_boot()
{
    std::printf("switch in STOP at power-on\n");
    reset(SwitchPlugin::ReportsStop);
    plc_boot_start_t r = boot(false);

    CHECK(r == PLC_BOOT_START_HELD_BY_SWITCH, "auto-start held by the switch");
    CHECK(g_transitions.empty(), "no transition begun (no start/stop pair)");
    CHECK(!g_running, "PLC never RUNNING");
    CHECK(g_scans == 0, "zero scan cycles");
    CHECK(plc_get_switch_position() == PLC_SWITCH_STOP, "gate reads the reported STOP");
    CHECK(!g_request_while_not_held, "the plugin's init() request was held");

    /* The switch moved to RUN after boot: the plugin's watcher reports it. */
    plc_set_switch_position(PLC_SWITCH_RUN);
    request_plc_start();
    CHECK(g_transitions.size() == 1 && g_transitions[0] == PLC_STATE_RUNNING,
          "switch moved to RUN starts the PLC once");
    CHECK(g_running, "PLC started after the switch moved to RUN");
}

static void test_switch_run_at_boot()
{
    std::printf("switch in RUN at power-on\n");
    reset(SwitchPlugin::ReportsRun);
    plc_boot_start_t r = boot(false);

    CHECK(r == PLC_BOOT_START_BEGUN, "auto-start begun");
    CHECK(g_transitions.size() == 1 && g_transitions[0] == PLC_STATE_RUNNING,
          "exactly one start, from the auto-start");
    CHECK(g_running, "PLC goes RUNNING");
    CHECK(!g_request_while_not_held, "the plugin's init() start request did not race the boot");
}

static void test_no_switch_plugin()
{
    std::printf("no switch-aware plugin\n");
    reset(SwitchPlugin::None);
    plc_boot_start_t r = boot(false);

    CHECK(r == PLC_BOOT_START_BEGUN, "auto-start begun");
    CHECK(g_transitions.size() == 1 && g_transitions[0] == PLC_STATE_RUNNING,
          "exactly one start");
    CHECK(plc_get_switch_position() == PLC_SWITCH_RUN, "switch stays at its RUN default");
    CHECK(!plc_switch_take_movement(), "no switch movement recorded");
}

static void test_bring_up_order()
{
    std::printf("plugin bring-up order\n");
    reset(SwitchPlugin::None);
    plc_boot_init_plugins(&g_driver, true);

    const std::vector<std::string> want = {
        "hold", "load:" PLC_BOOT_PLUGINS_CONF, "append:" PLC_BOOT_VPP_PLUGINS_CONF, "init",
        "release"};
    CHECK(g_calls == want, "hold, plugins.conf, vpp_plugins.conf, init, release");
    CHECK(!g_held, "requests released after bring-up");
}

static void test_safe_mode()
{
    std::printf("safe mode\n");
    reset(SwitchPlugin::ReportsRun);
    plc_boot_start_t r = boot(true);

    CHECK(r == PLC_BOOT_START_SKIPPED_SAFE_MODE, "auto-start skipped");
    CHECK(!g_vpp_loaded, "program plugins (vpp_plugins.conf) not loaded");
    CHECK(g_transitions.empty(), "no transition begun");
}

static void test_null_driver()
{
    std::printf("no plugin driver\n");
    reset(SwitchPlugin::None);
    CHECK(plc_boot_init_plugins(nullptr, true) == -1, "NULL driver rejected");
    CHECK(g_calls.empty(), "nothing touched");
}

int main()
{
    test_switch_stop_at_boot();
    test_switch_run_at_boot();
    test_no_switch_plugin();
    test_bring_up_order();
    test_safe_mode();
    test_null_driver();

    if (g_failures == 0)
        std::printf("all cases passed\n");
    return g_failures == 0 ? 0 : 1;
}
