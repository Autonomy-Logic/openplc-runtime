// plc_io_cycle.cpp — per-cycle I/O work, split into pre/post halves
//                    around the fastest IEC task's body.
//
// Decoupled from plc_state_manager.cpp so the housekeeping is in one
// place. Both halves run inside the image-tables critical section.

#include <atomic>
#include <ctime>

extern "C" {
#include "../drivers/plugin_driver.h"
}

#include "image_tables.h"
#include "journal_buffer.h"
#include "plc_io_cycle.h"
#include "utils/utils.h"

extern std::atomic<long>  plc_heartbeat;
extern plugin_driver_t   *plugin_driver;

// --- Threaded (process-image) model housekeeping ---------------------------
// The drain runs at every task's copy-in (under the image mutex) so each task
// sees freshly-applied plugin/peer writes. The pre/post halves run only on the
// fastest task: pre opens the plugin cycle window before bodies, post advances
// the scan clock, closes the plugin window, and bumps the global heartbeat /
// scan counter once per scan.

extern "C" void plc_run_io_cycle_threaded_drain(void)
{
    journal_apply_and_clear();
}

extern "C" void plc_run_io_cycle_threaded_pre(void)
{
    if (plugin_driver) plugin_driver_cycle_start(plugin_driver);
}

extern "C" void plc_run_io_cycle_threaded_post(void)
{
    if (ext_strucpp_advance_time) ext_strucpp_advance_time(base_tick_ns);
    if (plugin_driver) plugin_driver_cycle_end(plugin_driver);
    plc_heartbeat.store((long)time(nullptr));
    ++scan_counter;
}
