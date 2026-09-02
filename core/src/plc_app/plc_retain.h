/**
 * @file plc_retain.h
 * @brief Retain-variable persistence — the runtime's half (NODE-94).
 *
 * The runtime MARSHALS and the platform STORES, exactly as on baremetal. The
 * marshalling itself lives inside the loaded .so (STruC++'s `iec_retain.hpp`,
 * reached through the `strucpp_retain_*` exports), because that is where the
 * debug tables live and because one copy of a wire format is better than two.
 * What this file owns is the buffer and the call sites.
 *
 * ONE DRIVER INTERFACE, SEVERAL DRIVERS
 * ------------------------------------
 * Storage reaches this file as a `retain_driver_t` — three function pointers,
 * filled in once at init by whichever driver claimed the role. Nothing below
 * that point knows or asks which kind of store it got: a VPP plugin holding
 * FRAM and the runtime's own file store are the same shape, called at the same
 * points, and neither is a special case in an `if`.
 *
 * The built-in file store is therefore a DRIVER, not a fallback branch. It is
 * also the lowest-ranked one and the only one that has to be switched on: it
 * declares itself available only when `retain.conf` enables it, which the
 * editor emits from the project's Persistent Storage settings. A VPP that owns
 * retention declares `hidesNativeScreens: ['persistent-storage']`, the editor
 * emits no `retain.conf`, the upload removes any stale copy, and the file store
 * simply does not offer itself — leaving the vendor's driver as the only
 * candidate. Replacement needs no priority table because a correctly declared
 * device presents exactly one store.
 *
 * With no driver at all the calls are no-ops and retain degrades to NON_RETAIN,
 * which is what the runtime did before any of this existed.
 *
 * THE THREE CALLS, AND WHEN THEY HAPPEN
 * ------------------------------------
 *
 *     start   plc_retain_read()    once, before the first scan
 *     scan    plc_retain_save()    every cycle, WHILE RUNNING ONLY
 *     stop    plc_retain_flush()   once, as the program is unloaded
 *
 * Identical, deliberately, to baremetal's `openplc_retain.h`: same names, same
 * order, same meaning, so a vendor reads one contract and implements the same
 * shape twice.
 *
 * CADENCE IS NOT OURS TO DECIDE
 * ----------------------------
 * `plc_retain_save()` is called once per scan cycle, unconditionally, for as
 * long as the PLC is running. It does not diff, does not rate-limit and does
 * not judge whether a value is worth keeping — the driver holds the bytes and
 * commits on whatever schedule its medium can sustain. A driver over flash that
 * wrote through on every call would consume its endurance budget in hours; one
 * over FRAM is free to write every cycle, which is why the call happens at that
 * cadence at all.
 *
 * SAVE IS THE DURABILITY PATH; FLUSH IS ONLY A HINT. A power cut does not call
 * flush(), and retention exists for the power cut nobody schedules.
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
 * @brief Length of the program identity passed to a driver's read hook.
 *
 * An MD5 as lower-case hex: exactly 32 characters, and NOT NUL-terminated —
 * compare with memcmp over this length, never strcmp.
 *
 * Defined once, here, because three copies of the number existed and were tied
 * together only by comments. Baremetal's `OPLC_RETAIN_PROGRAM_ID_LEN` is the
 * fourth, and deliberately stays separate: it sits across a process and
 * toolchain boundary where a shared header would be worse than a documented
 * constant. It must carry the same value.
 */
#define PLC_RETAIN_PROGRAM_ID_LEN 32

/**
 * @brief Decide once, after the program is loaded, whether retain can run, and
 *        bind whichever driver claimed the store.
 *
 * Checks that the .so exports the retain entry points (a program built by an
 * older STruC++ does not), that the program retains anything at all, and that
 * the blob fits the runtime's buffer. Then asks the drivers, in rank order,
 * which of them will hold the bytes. Logs what it found, once — including the
 * layout hash, which is the thing to compare when a restore is unexpectedly
 * refused, and the name of the store that won.
 *
 * Call after symbols are resolved and `g_config` is constructed, before the
 * first task is released.
 */
void plc_retain_init(void);

/**
 * @brief Restore the retained values the driver is holding FOR THIS PROGRAM.
 *
 * The running program's identity (`strucpp_program_md5`) goes to the driver,
 * which decides whether what it holds still belongs here. A driver that finds a
 * different program's values discards them, logs that storage was cleared, and
 * reports empty — so every retained variable starts at its declared initial
 * value, which is what a new program means. That decision is the driver's
 * because it is inseparable from how the driver stores things, and because it
 * is the only way a store can be correct without the runtime having to be told
 * when an upload happened.
 *
 * On top of that the blob is validated inside the .so (magic, format, layout
 * hash, crc32), and one that fails leaves every variable at its initial value —
 * a machine starting from its defaults is recoverable, one starting from
 * plausible-looking garbage is not.
 *
 * Safe and cheap when nothing is retained or no driver claimed the store, and
 * idempotent: call once per program start, after plc_retain_init().
 */
void plc_retain_read(void);

/**
 * @brief Hand the current retained values to the driver.
 *
 * Called ONCE PER SCAN CYCLE from the dispatcher's quiescent window, where
 * `g_tasks_running == 0` and no worker is inside a body — the same guarantee
 * `image_tables_copy_config_globals_out()` relies on. Reading the leaves
 * anywhere else would race the task threads.
 */
void plc_retain_save(void);

/**
 * @brief Ask the driver to commit anything it is still holding, now.
 *
 * Called once as the program is unloaded, after the cycle thread has been
 * joined so no scan is mid-save, and before the plugins are stopped so a
 * plugin-backed store is still alive to answer.
 *
 * A hint, not the durability mechanism — see the note at the top of this file.
 * What it buys is that a CLEAN stop loses nothing on a driver that buffers.
 */
void plc_retain_flush(void);

#ifdef __cplusplus
}
#endif

#endif /* PLC_RETAIN_H */
