/**
 * @file plc_retain_file_store.h
 * @brief The runtime's own retain backend: a file on the data partition.
 *
 * WHY THE RUNTIME SHIPS ONE AT ALL
 * --------------------------------
 * Retention hardware is a property of the device, so the storage decision
 * belongs to the platform — that has not changed, and a VPP that provides its
 * own backend still wins outright (see plc_retain_init). But "the platform has
 * no backend" and "the platform has FRAM" are not the only two cases: a Linux
 * box running runtime v4 has a filesystem, and a file on it is a perfectly good
 * place to keep retained values. Making every vendor write that same file
 * backend to get retain at all is a poor trade.
 *
 * So this is the default, and it is DISABLED by default. An operator turns it
 * on for a given device; until then retain is a no-op exactly as it was, and
 * every retained variable starts at its declared initial value.
 *
 * DISABLED BY DEFAULT IS DELIBERATE
 * ---------------------------------
 * Writing to the data partition on a cadence the operator did not choose is not
 * something to switch on for everyone. An SD-card-backed box has an endurance
 * budget its owner may be counting on; a read-mostly image may not want the
 * partition written at all. Opt-in keeps the decision where it belongs.
 *
 * CONFIGURATION
 * -------------
 * Read once, at program load, from a small key=value file that arrives with the
 * program upload (`./retain.conf` beside `plugins.conf`) — the editor emits it
 * from the project's Persistent Storage settings and the upload installs it:
 *
 *     enabled=1
 *     path=/var/lib/openplc-runtime/retain.bin
 *     flush_seconds=5
 *
 * `flush_seconds` bounds how much retained state a power cut costs, against how
 * hard the storage is worked. It is a real trade and it belongs to whoever
 * installs the machine, which is why it is configuration and not a constant.
 */

#ifndef PLC_RETAIN_FILE_STORE_H
#define PLC_RETAIN_FILE_STORE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Read `retain.conf` and start the flush thread if enabled.
 *
 * Safe to call repeatedly; a second call re-reads the file, so settings that
 * arrived with a program upload take effect on the next PLC start without a
 * daemon restart. Returns true when the store is enabled and usable.
 */
bool plc_retain_file_store_start(const char *config_path);

/** @brief Final flush, then stop the flush thread. */
void plc_retain_file_store_stop(void);

/** @brief Whether the store is enabled and willing to hold bytes. */
bool plc_retain_file_store_active(void);

/** @brief The configured path, for logging. Empty when disabled. */
const char *plc_retain_file_store_path(void);

/* The three hooks, shaped exactly like the plugin ones (plugin_driver.h) so
 * plc_retain.cpp routes to either through one uniform driver record and never
 * learns which kind of store it got. 0 on success.
 *
 * `load` is handed the running program's identity and decides for itself
 * whether the bytes on disk still belong to it — a file labelled with a
 * different program is removed and reported empty. See the plugin contract in
 * plugin_driver.h; this store is one implementation of it, not a special case
 * beside it. */
int plc_retain_file_store_save(const uint8_t *blob, uint16_t len);
int plc_retain_file_store_load(const char *program_md5, uint16_t md5_len, uint8_t *out,
                               uint16_t cap, uint16_t *out_len);
int plc_retain_file_store_flush(void);

#ifdef __cplusplus
}
#endif

#endif /* PLC_RETAIN_FILE_STORE_H */
