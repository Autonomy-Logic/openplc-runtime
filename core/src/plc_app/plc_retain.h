/**
 * @file plc_retain.h
 * @brief Retain-variable persistence — the runtime's half (NODE-94).
 *
 * The runtime MARSHALS and the platform STORES, exactly as on baremetal. The
 * marshalling itself lives inside the loaded .so (STruC++'s `iec_retain.hpp`,
 * reached through the `strucpp_retain_*` exports), because that is where the
 * debug tables live and because one copy of a wire format is better than two.
 * What this file owns is the buffer, the call sites, and the handover to a
 * plugin.
 *
 * WHY A PLUGIN AND NOT A FILE HERE
 * --------------------------------
 * There is no default backend. Retention hardware is a property of the device,
 * not of the runtime: an SLM-RP4 has a data partition, another box has FRAM or
 * battery-backed SRAM, a third has nothing at all. A file-backed default in the
 * runtime would look like support on every device and be wrong on most of them.
 * With no plugin the calls are no-ops and retain degrades to NON_RETAIN, which
 * is what the runtime did before this file existed.
 *
 * The plugin surface mirrors baremetal's `openplc_retain.h` name for name, so a
 * vendor writing retain support reads one contract and implements the same
 * shape twice.
 *
 * CADENCE IS NOT OURS TO DECIDE
 * ----------------------------
 * `plc_retain_save()` is called once per scan cycle, unconditionally. It does
 * not diff, does not rate-limit and does not judge whether a value is worth
 * keeping — the plugin holds the bytes and flushes on whatever schedule its
 * medium can sustain. A driver over flash that wrote through on every call
 * would consume its endurance budget in hours.
 */

#ifndef PLC_RETAIN_H
#define PLC_RETAIN_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Decide once, after the program is loaded, whether retain can run.
 *
 * Checks that the .so exports the retain entry points (a program built by an
 * older STruC++ does not), that the program retains anything at all, and that
 * the blob fits the runtime's buffer. Logs what it found, once — including the
 * layout hash, which is the thing to compare when a restore is unexpectedly
 * refused.
 *
 * Call after symbols are resolved and `g_config` is constructed, before the
 * first task is released.
 */
void plc_retain_init(void);

/**
 * @brief Restore retained values from the plugin's store.
 *
 * Safe and cheap when nothing is retained or no plugin provides storage. The
 * blob is validated inside the .so (magic, format, layout hash, crc32) and a
 * blob that fails leaves every variable at its declared initial value — a
 * machine starting from its defaults is recoverable, one starting from
 * plausible-looking garbage is not.
 *
 * Call once per program start, after plc_retain_init().
 */
void plc_retain_load(void);

/**
 * @brief Hand the current retained values to the plugin.
 *
 * Called ONCE PER SCAN CYCLE from the dispatcher's quiescent window, where
 * `g_tasks_running == 0` and no worker is inside a body — the same guarantee
 * `image_tables_copy_config_globals_out()` relies on. Reading the leaves
 * anywhere else would race the task threads.
 */
void plc_retain_save(void);

/**
 * @brief Discard the stored blob, so the next start uses the declared
 *        initialisers.
 *
 * Called on program upload (CODESYS clears retained memory on download, and a
 * new program's values have no business surviving into it) and by the
 * `RETAIN:CLEAR` socket command.
 */
void plc_retain_clear(void);

/**
 * @brief Whether retain is actually live: a program that retains something,
 *        exports the entry points, and a plugin willing to store the bytes.
 *
 * Reported to the editor so "retain is configured but this device cannot do
 * it" is visible rather than silent.
 */
bool plc_retain_active(void);

/**
 * @brief Which backend is holding the bytes, for the editor's Persistent
 *        Storage screen.
 *
 * One of "none", "plugin" or "file". The screen needs to distinguish them
 * because a VPP plugin OVERRIDES the built-in file store: on such a device the
 * file settings are still there and still saved, and are simply not what is
 * being used. Showing "enabled" while a plugin quietly handles retention would
 * be a lie the operator only discovers by looking for a file that never grows.
 */
const char *plc_retain_backend(void);

/** @brief The store's target for display — a path, or a plugin name. */
const char *plc_retain_backend_detail(void);

#ifdef __cplusplus
}
#endif

#endif /* PLC_RETAIN_H */
