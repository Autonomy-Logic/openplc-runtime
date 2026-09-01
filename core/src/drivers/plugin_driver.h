#ifndef PLUGIN_DRIVER_H
#define PLUGIN_DRIVER_H

#include "../plc_app/plcapp_manager.h"
#include "plugin_config.h"
#include "plugin_types.h"
#include "python_plugin_bridge.h"

#ifdef __cplusplus
extern "C" {
#endif

// Maximum number of plugins
#define MAX_PLUGINS 16

typedef enum
{
    PLUGIN_TYPE_PYTHON,
    PLUGIN_TYPE_NATIVE
} plugin_type_t;

typedef int (*plugin_init_func_t)(void *);
typedef int (*plugin_start_loop_func_t)(void);
typedef void (*plugin_stop_loop_func_t)(void);
typedef void (*plugin_cycle_start_func_t)(void);
typedef void (*plugin_cycle_end_func_t)(void);
typedef void (*plugin_cleanup_func_t)(void);
typedef int (*plugin_execute_command_func_t)(const char *command_json, char *response,
                                             size_t response_size);
// Optional: fills `out` with a JSON object describing plugin-specific
// statistics. Called from the STATS response path so it MUST be
// non-blocking (atomic reads or trivial copies only).
// Return 0 on success; any other value means "skip me this cycle."
typedef int (*plugin_get_stats_func_t)(char *out, size_t out_size);

/* ---- Optional: retain-variable storage (NODE-94) -------------------------
 *
 * A plugin that owns retention hardware exports these and becomes the device's
 * retain store, displacing the runtime's built-in file store. Same names, same
 * status meaning and the same contract text as baremetal's `openplc_retain.h`,
 * and the two runtimes call them at the same points in the PLC lifecycle, so a
 * vendor writes one shape twice rather than learning two interfaces for one job.
 *
 *     start   retain_load()    once, before the first scan
 *     scan    retain_save()    every cycle, WHILE RUNNING ONLY
 *     stop    retain_flush()   once, as the program is unloaded
 *
 * The runtime MARSHALS and the plugin STORES: what arrives is an opaque blob,
 * already validated on the way back in (magic, format, layout hash, crc32), so
 * a backend needs no understanding of retained variables at all.
 *
 * SAVE IS THE DURABILITY PATH; FLUSH IS ONLY A HINT. Retention exists for the
 * power cut nobody schedules, and a power cut does not call flush(). A plugin
 * that commits solely in flush() therefore loses everything in exactly the case
 * it was written for. Decide durability in save().
 *
 * `retain_save` is called ONCE PER SCAN CYCLE, unconditionally, from the
 * dispatcher's quiescent window, for as long as the PLC is running. The runtime
 * does not diff and does not rate-limit — holding the bytes and committing on a
 * schedule the medium can sustain is the plugin's job, and the reason the call
 * exists at that cadence is so a plugin that CAN write every cycle (FRAM,
 * battery-backed SRAM) is free to. It MUST return promptly and MUST NOT block:
 * this runs inside the scan, so time spent here is time the PLC is not scanning.
 *
 * `retain_load` is handed the running program's identity — `md5_len` characters
 * of lower-case hex, NOT guaranteed NUL-terminated, so compare with memcmp.
 * THE PLUGIN DECIDES whether the bytes it holds still belong to this program:
 * identity differs → discard them, log one line saying storage was cleared, and
 * report empty (`*out_len = 0`), so every retained variable starts at its
 * declared initial value. Do NOT persist the new identity here; hold it and
 * commit it alongside the blob on the next `retain_save`, so a load never
 * mutates storage and identity and bytes are written as one unit.
 *
 * Both save and load must be exported for the plugin to be used as the store; a
 * plugin exporting only one is ignored, since a store that can save and not
 * load is worse than none. Return 0 on success, non-zero otherwise.
 */
typedef int (*plugin_retain_save_func_t)(const uint8_t *blob, uint16_t len);
typedef int (*plugin_retain_load_func_t)(const char *program_md5, uint16_t md5_len,
                                         uint8_t *out, uint16_t cap, uint16_t *out_len);
/* Optional third: commit anything still held, now. A plugin without it is
 * assumed to commit inside save(), which is where durability belongs anyway. */
typedef int (*plugin_retain_flush_func_t)(void);

typedef struct
{
    void *handle; // Handle to the loaded shared library
    plugin_init_func_t init;
    plugin_start_loop_func_t start;
    plugin_stop_loop_func_t stop;
    plugin_cycle_start_func_t cycle_start;
    plugin_cycle_end_func_t cycle_end;
    plugin_cleanup_func_t cleanup;
    plugin_execute_command_func_t execute_command;
    plugin_get_stats_func_t get_stats;
    /* Optional retain-store hooks; NULL unless the plugin exports them. */
    plugin_retain_save_func_t  retain_save;
    plugin_retain_load_func_t  retain_load;
    plugin_retain_flush_func_t retain_flush;
} plugin_funct_bundle_t;

// Plugin instance structure
typedef struct plugin_instance_s
{
    PluginManager *manager;
    python_binds_t *python_plugin;
    plugin_funct_bundle_t *native_plugin;
    // pthread_t thread;
    int running;
    /* Set after a successful init() call; cleared by cleanup. Tracked
     * separately from `running` so a partial init failure (e.g.,
     * pthread_create on the cycle thread fails AFTER plugin_driver_init
     * succeeded) can roll back only the plugins that actually got
     * initialised, not those still untouched. */
    int initialized;
    /* Set when an *enabled* plugin failed to load its symbols (e.g. a
     * native .so whose runtime dependency is missing, such as the
     * EtherCAT plugin without Npcap on Windows). A degraded plugin keeps
     * its config slot but has a NULL native_plugin/python_plugin, so it
     * is skipped by init/start/cycle. The runtime stays out of ERROR;
     * commands routed to it return a clear "unavailable" response instead
     * of crashing the whole runtime on boot. */
    int degraded;
    plugin_config_t config;
} plugin_instance_t;

// Driver structure
typedef struct
{
    plugin_instance_t plugins[MAX_PLUGINS];
    int plugin_count;
} plugin_driver_t;

// Driver management functions
plugin_driver_t *plugin_driver_create(void);
int plugin_driver_load_config(plugin_driver_t *driver, const char *config_file);
int plugin_driver_update_config(plugin_driver_t *driver, const char *config_file);
/** Append plugins from a secondary config file without tearing down the
 *  plugins already loaded by plugin_driver_update_config. Used to load
 *  VPP plugins from vpp_plugins.conf after built-ins from plugins.conf.
 *  Returns 0 on success, -1 if any enabled plugin fails to load its .so. */
int plugin_driver_append_config(plugin_driver_t *driver, const char *config_file);
int plugin_driver_init(plugin_driver_t *driver);
/* Mirror of plugin_driver_init: walks plugins[] in reverse order and calls
 * the matching cleanup hook on every plugin whose `initialized` flag is
 * set, then clears the flag. Used to roll back a partial init when a
 * later step (e.g., spawning the cycle thread) fails — without this, a
 * subsequent INIT cycle re-runs plugin init() on top of half-allocated
 * state and duplicates threads/sockets. Safe to call when no plugins are
 * initialised. Returns the count of plugins it cleaned up. */
int plugin_driver_cleanup_init(plugin_driver_t *driver);
int plugin_driver_start(plugin_driver_t *driver);
int plugin_driver_stop(plugin_driver_t *driver);
void plugin_driver_destroy(plugin_driver_t *driver);

/* Release the Python GIL held by the calling thread after plugin loading, and
 * remember the thread state so plugin_driver_destroy() can restore it before
 * Py_FinalizeEx().
 *
 * Call from the MAIN thread, once, after plugins are loaded. The runtime used to
 * call PyEval_SaveThread() directly and drop the returned state on the floor,
 * which left the driver with nothing to restore: shutdown then finalised the
 * interpreter with no GIL held and segfaulted -- on every graceful shutdown of a
 * runtime whose PLC had never started, safe mode included. Keeping the
 * bookkeeping next to the code that consumes it is what makes that
 * unrepresentable. No-op when Python was never initialised. */
void plugin_driver_release_gil(void);

// Cycle hook functions for native plugins (called during PLC scan cycle)
// These iterate through all active native plugins and call their cycle hooks
// Plugins opt-in by implementing cycle_start/cycle_end; opt-out by not implementing them
void plugin_driver_cycle_start(plugin_driver_t *driver);
void plugin_driver_cycle_end(plugin_driver_t *driver);

/* ---- Retain store ------------------------------------------------------- */

/* The plugin acting as this device's retain store, or NULL.
 *
 * The FIRST plugin that exports both retain_save and retain_load wins, and any
 * others are logged and ignored. Two plugins writing the same retained values
 * to different places would both appear to work and disagree on the next boot,
 * which is a far worse failure than refusing the second. */
plugin_instance_t *plugin_driver_find_retain_store(plugin_driver_t *driver);

int plugin_driver_retain_save(plugin_instance_t *store, const uint8_t *blob, uint16_t len);
int plugin_driver_retain_load(plugin_instance_t *store, const char *program_md5, uint16_t md5_len,
                              uint8_t *out, uint16_t cap, uint16_t *out_len);
int plugin_driver_retain_flush(plugin_instance_t *store);

// Route a command to a specific plugin by name (for async commands like scan)
int plugin_driver_execute_command(plugin_driver_t *driver, const char *plugin_name,
                                  const char *command_json, char *response, size_t response_size);

// Splice plugin-contributed statistics into an already-formatted STATS
// response. Walks loaded native plugins, calls each get_stats, and
// inserts a "plugin_stats":{...} member before the closing `}` of the
// existing STATS JSON. A trailing newline in `buffer` is preserved.
// No-op if no plugin provides stats. Returns the new string length.
size_t plugin_driver_append_stats_json(plugin_driver_t *driver, char *buffer,
                                       size_t buffer_size);

// Python plugin functions
int python_plugin_get_symbols(plugin_instance_t *plugin);

// Native plugin functions
int native_plugin_get_symbols(plugin_instance_t *plugin);

// Runtime arguments generation
void *generate_structured_args_with_driver(plugin_type_t type, plugin_driver_t *driver,
                                           int plugin_index);
void free_structured_args(plugin_runtime_args_t *args);

#ifdef __cplusplus
}
#endif

#endif // PLUGIN_DRIVER_H
