// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

// Host test: boot plugin bring-up and the boot auto-start against the mode switch.
// Links the real plc_boot.c, plc_switch.c and plugin_plc_requests.c (the request
// gate plugins call); the plugin driver, the transition entry point and logging
// are faked here.

#include "drivers/plugin_plc_requests.h"
#include "plc_boot.h"
#include "plc_state_manager.h"
#include "plc_switch.h"

#include <algorithm>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <mutex>
#include <string>
#include <thread>
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
    ReportsRun,
    DeclaredSilent,
    DeclaredReportsRunLater,
    DeclaredReportsStopLater
};

static const char *const VPP_ID = "fakevpp|./build/plugins/libfakevpp_plugin.so";

static const int LATE_REPORT_MS = 100;

static SwitchPlugin g_switch_plugin; // the switch-aware plugin, shipped in vpp_plugins.conf
static bool g_vpp_loaded;
static std::vector<std::string> g_calls;
static std::vector<PLCState> g_transitions; // every transition the runtime began
static PLCState g_state;                    // fake transitions land at once
static std::vector<std::string> g_errors;
static std::vector<std::string> g_warns;
static std::vector<std::string> g_infos;
static std::mutex g_mu;
static std::thread g_reporter;

static bool logged(const std::vector<std::string> &log, const char *needle)
{
    std::lock_guard<std::mutex> lock(g_mu);
    for (const std::string &line : log)
        if (line.find(needle) != std::string::npos)
            return true;
    return false;
}

/* Whether any level of the journal holds @p needle. */
static bool logged_any(const char *needle)
{
    return logged(g_infos, needle) || logged(g_warns, needle) || logged(g_errors, needle);
}

static void join_reporter()
{
    if (g_reporter.joinable())
        g_reporter.join();
}

/* Back to the power-on default: no owner, RUN stored, no movement on record. */
static void reset(SwitchPlugin plugin)
{
    join_reporter();
    g_switch_plugin = plugin;
    g_vpp_loaded    = false;
    g_calls.clear();
    g_transitions.clear();
    g_errors.clear();
    g_warns.clear();
    g_infos.clear();
    g_state = PLC_STATE_STOPPED;
    plugin_driver_hold_plc_requests(false);
    plc_switch_owners_forget_absent(nullptr, 0);
    plc_switch_owner_report("reset|reset", PLC_SWITCH_RUN);
    plc_switch_owners_forget_absent(nullptr, 0);
    (void)plc_switch_take_movement();
}

static int count(PLCState target)
{
    int n = 0;
    for (PLCState t : g_transitions)
        if (t == target)
            n++;
    return n;
}

/* ---- fakes: transition entry point and logging -------------------------- */

extern "C" bool plc_begin_transition(PLCState target)
{
    std::lock_guard<std::mutex> lock(g_mu);
    if (g_state == target)
        return false; // claim refused, as plc_claim_transition does
    g_transitions.push_back(target);
    g_state = target;
    return true;
}

static void capture(std::vector<std::string> *sink, const char *fmt, va_list ap)
{
    char buf[512];
    std::vsnprintf(buf, sizeof(buf), fmt, ap);
    std::lock_guard<std::mutex> lock(g_mu);
    if (sink)
        sink->push_back(buf);
}

extern "C" void log_info(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    capture(&g_infos, fmt, ap);
    va_end(ap);
}
extern "C" void log_debug(const char *, ...) {}
extern "C" void log_warn(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    capture(&g_warns, fmt, ap);
    va_end(ap);
}
extern "C" void log_error(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    capture(&g_errors, fmt, ap);
    va_end(ap);
}

/* A switch-aware plugin reporting a position as its own slot does: an attributed
 * report, then the request through the real runtime entry point. */
static void plugin_reports(plc_switch_t position, const char *reason)
{
    plc_switch_owner_report(VPP_ID, position);
    if (position == PLC_SWITCH_RUN)
        plugin_request_plc_start(reason);
    else
        plugin_request_plc_stop(reason);
}

/* ---- fakes: plugin driver ----------------------------------------------- */

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

extern "C" int plugin_driver_append_hmi(plugin_driver_t *)
{
    g_calls.push_back("hmi");
    return 0;
}

/* The plugin's watcher thread: reports @p position LATE_REPORT_MS after init(). */
static void report_later(plc_switch_t position)
{
    g_reporter = std::thread([position] {
        std::this_thread::sleep_for(std::chrono::milliseconds(LATE_REPORT_MS));
        plugin_reports(position, "watcher saw the switch");
    });
}

extern "C" int plugin_driver_init(plugin_driver_t *)
{
    g_calls.push_back("init");
    if (!g_vpp_loaded)
        return 0;
    switch (g_switch_plugin)
    {
    case SwitchPlugin::ReportsStop:
        plugin_reports(PLC_SWITCH_STOP, "switch is STOP at init");
        break;
    case SwitchPlugin::ReportsRun:
        plugin_reports(PLC_SWITCH_RUN, "switch is RUN at init");
        break;
    case SwitchPlugin::DeclaredSilent:
        plc_switch_owner_declare(VPP_ID, true);
        break;
    case SwitchPlugin::DeclaredReportsRunLater:
        plc_switch_owner_declare(VPP_ID, true);
        report_later(PLC_SWITCH_RUN);
        break;
    case SwitchPlugin::DeclaredReportsStopLater:
        plc_switch_owner_declare(VPP_ID, true);
        report_later(PLC_SWITCH_STOP);
        break;
    case SwitchPlugin::None:
        break;
    }
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
    CHECK(g_transitions.empty(), "no transition begun: never RUNNING, no start/stop pair");
    CHECK(plc_get_switch_position() == PLC_SWITCH_STOP, "gate reads the reported STOP");
    CHECK(logged(g_infos, "stop request not acted on during plugin bring-up"),
          "the init() stop request went through the real gate and was held");
    CHECK(logged(g_infos, "Hardware mode switch is in STOP - PLC left stopped"),
          "journal says the PLC was left stopped");
    CHECK(g_errors.empty(), "no error logged");

    /* The switch moved to RUN after boot: the plugin's watcher reports it. */
    plugin_reports(PLC_SWITCH_RUN, "switch moved to RUN");
    CHECK(count(PLC_STATE_RUNNING) == 1 && g_transitions.size() == 1,
          "switch moved to RUN after boot starts the PLC once");
}

static void test_switch_run_at_boot()
{
    std::printf("switch in RUN at power-on\n");
    reset(SwitchPlugin::ReportsRun);
    plc_boot_start_t r = boot(false);

    CHECK(r == PLC_BOOT_START_BEGUN, "auto-start begun");
    CHECK(count(PLC_STATE_RUNNING) == 1 && g_transitions.size() == 1,
          "exactly one start, from the auto-start");
    CHECK(logged(g_infos, "start request not acted on during plugin bring-up"),
          "the init() start request was held by the real gate");
    CHECK(g_errors.empty(), "no error logged");
}

static void test_no_switch_plugin()
{
    std::printf("no switch-aware plugin\n");
    reset(SwitchPlugin::None);
    plc_boot_start_t r = boot(false);

    CHECK(r == PLC_BOOT_START_BEGUN, "auto-start begun");
    CHECK(count(PLC_STATE_RUNNING) == 1 && g_transitions.size() == 1, "exactly one start");
    CHECK(plc_get_switch_position() == PLC_SWITCH_RUN, "switch stays at its RUN default");
    CHECK(!plc_switch_take_movement(), "no switch movement recorded");
    CHECK(g_errors.empty(), "no error logged");
}

static void test_flip_to_run_while_held()
{
    std::printf("switch flips to RUN between plugin init and the auto-start\n");
    reset(SwitchPlugin::ReportsStop);
    plc_boot_init_plugins(&g_driver, true);

    /* The plugin's watcher sees the flip while the socket is being set up. */
    plugin_reports(PLC_SWITCH_RUN, "switch moved to RUN");
    CHECK(g_transitions.empty(), "the plugin's start request is held until the decision");

    plc_boot_start_t r = plc_boot_autostart(false);
    CHECK(r == PLC_BOOT_START_BEGUN, "auto-start begun on the stored RUN");
    CHECK(count(PLC_STATE_RUNNING) == 1 && g_transitions.size() == 1, "exactly one start");
    CHECK(!logged(g_errors, "Failed to initiate PLC start"), "no false start failure logged");
    CHECK(g_errors.empty(), "no error logged");
}

static void test_hold_released_on_every_path()
{
    std::printf("request hold released after the decision\n");
    const struct
    {
        SwitchPlugin plugin;
        bool safe_mode;
        const char *what;
    } paths[] = {
        {SwitchPlugin::ReportsStop, false, "held-by-switch path releases the hold"},
        {SwitchPlugin::ReportsRun, false, "start path releases the hold"},
        {SwitchPlugin::ReportsRun, true, "safe-mode path releases the hold"},
    };
    for (const auto &p : paths)
    {
        reset(p.plugin);
        boot(p.safe_mode);
        g_infos.clear();
        plugin_request_plc_stop("probe");
        CHECK(!logged(g_infos, "not acted on during plugin bring-up"), p.what);
    }
}

static void test_bring_up_order()
{
    std::printf("plugin bring-up order\n");
    reset(SwitchPlugin::None);
    plc_boot_init_plugins(&g_driver, true);

    const std::vector<std::string> want = {"load:" PLC_BOOT_PLUGINS_CONF,
                                           "append:" PLC_BOOT_VPP_PLUGINS_CONF, "hmi", "init"};
    CHECK(g_calls == want, "plugins.conf, then vpp_plugins.conf, then the HMI, then init");

    g_infos.clear();
    plugin_request_plc_start("probe");
    CHECK(logged(g_infos, "start request not acted on during plugin bring-up"),
          "requests still held after init, until the auto-start decides");
    plc_boot_autostart(false);
}

static void test_safe_mode()
{
    std::printf("safe mode\n");
    reset(SwitchPlugin::ReportsRun);
    plc_boot_start_t r = boot(true);

    CHECK(r == PLC_BOOT_START_SKIPPED_SAFE_MODE, "auto-start skipped");
    CHECK(!g_vpp_loaded, "program plugins (vpp_plugins.conf) not loaded");
    CHECK(std::find(g_calls.begin(), g_calls.end(), "hmi") == g_calls.end(),
          "the HMI plugin is not loaded in safe mode");
    CHECK(g_transitions.empty(), "no transition begun");
}

static const char NO_REPORT_LINE[] = "no switch owner has reported a position";

static void test_owner_unreported_at_boot()
{
    std::printf("switch owner that never reports at power-on\n");
    reset(SwitchPlugin::DeclaredSilent);
    const auto t0      = std::chrono::steady_clock::now();
    plc_boot_start_t r = boot(false);
    const auto took    = std::chrono::steady_clock::now() - t0;
    const long took_ms = (long)std::chrono::duration_cast<std::chrono::milliseconds>(took).count();

    CHECK(r != PLC_BOOT_START_BEGUN, "auto-start not begun");
    CHECK(g_transitions.empty(), "no transition begun: the PLC stays STOPPED");
    CHECK(logged_any(NO_REPORT_LINE), "journal says no switch owner has reported a position");
    CHECK(took_ms < PLC_SWITCH_REPORT_WAIT_MS + 1500, "the boot wait is bounded");
}

static void test_owner_reports_run_within_the_wait()
{
    std::printf("switch owner reports RUN within the boot wait\n");
    reset(SwitchPlugin::DeclaredReportsRunLater);
    plc_boot_start_t r = boot(false);
    join_reporter();

    CHECK(r == PLC_BOOT_START_BEGUN, "auto-start begun on the late RUN report");
    CHECK(count(PLC_STATE_RUNNING) == 1 && g_transitions.size() == 1, "exactly one start");
    CHECK(!logged_any(NO_REPORT_LINE), "no unknown-switch line");
}

static void test_owner_reports_stop_within_the_wait()
{
    std::printf("switch owner reports STOP within the boot wait\n");
    reset(SwitchPlugin::DeclaredReportsStopLater);
    plc_boot_start_t r = boot(false);
    join_reporter();

    CHECK(r == PLC_BOOT_START_HELD_BY_SWITCH, "auto-start held by the switch");
    CHECK(g_transitions.empty(), "no transition begun");
}

static void test_plugin_start_refused_while_unknown()
{
    std::printf("plugin start request while the switch is unknown\n");
    reset(SwitchPlugin::None);
    plc_switch_owner_declare(VPP_ID, true);
    plugin_request_plc_start("probe");
    CHECK(g_transitions.empty(), "start request refused while the owner has not reported");

    plc_switch_owner_report(VPP_ID, PLC_SWITCH_RUN);
    plugin_request_plc_start("probe");
    CHECK(count(PLC_STATE_RUNNING) == 1, "the same request starts once the owner reports RUN");
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
    test_flip_to_run_while_held();
    test_hold_released_on_every_path();
    test_bring_up_order();
    test_safe_mode();
    test_null_driver();
    test_owner_unreported_at_boot();
    test_owner_reports_run_within_the_wait();
    test_owner_reports_stop_within_the_wait();
    test_plugin_start_refused_while_unknown();
    join_reporter();

    if (g_failures == 0)
        std::printf("all cases passed\n");
    return g_failures == 0 ? 0 : 1;
}
