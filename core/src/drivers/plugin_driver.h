// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

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

/* Optional retain storage. load() once pre-first-scan, save() every
 * cycle while RUNNING (inside the scan, MUST NOT block), flush() at
 * stop. load() memcmps program_md5 (not NUL-terminated); save AND
 * load are both required. Protocol: on identity mismatch load reports
 * *out_len=0; load MUST NOT persist the new identity — it is committed
 * alongside the blob by the next save. */
typedef int (*plugin_retain_save_func_t)(const uint8_t *blob, uint16_t len);
typedef int (*plugin_retain_load_func_t)(const char *program_md5, uint16_t md5_len,
                                         uint8_t *out, uint16_t cap, uint16_t *out_len);
/* Optional third: commit anything still held, now. A plugin without it is
 * assumed to commit inside save(), which is where durability belongs anyway. */
typedef int (*plugin_retain_flush_func_t)(void);

/* Optional, and its PRESENCE is the capability declaration.
 *
 * The fourteen image tables no longer share one length, and
 * `plugin_runtime_args_t` carries a single `buffer_size` that cannot say so.
 * Rather than move that struct -- CON06 guarantees pre-compiled plugins keep
 * their field offsets -- the sizes travel through a symbol the loader resolves
 * with dlsym, exactly as it already does for execute_command, get_stats and
 * the three retain_* hooks.
 *
 * Exporting it means "I understand per-table sizes". A plugin without it is
 * not broken and is not refused; the runtime keeps the image SQUARE for that
 * run instead, because a plugin bounding a byte index and a word index with
 * the same number is only safe while the tables are equal.
 *
 * Called BEFORE init(), once per plugin_driver_init(). Self-contained on
 * purpose -- no args -- which is what lets it run that early. A non-zero
 * return fails the plugin exactly as a failed init() does.
 *
 * `sizes` is indexed by `image_table_id_t` and `count` is how many entries it
 * carries, so a plugin built against an older enum reads the prefix it knows
 * and ignores the rest. */
typedef int (*plugin_set_image_sizes_func_t)(const uint32_t *sizes, uint32_t count);

/**
 * Optional `int detach_image(void)`: the keep-alive contract. Its presence is
 * the capability declaration, resolved with dlsym after the other symbols.
 *
 * Called after stop_loop() on a stop, or with no start_loop() on a load
 * rollback, always while the I/O image is still allocated and before the
 * journal is cleaned up, and only on an initialised plugin.
 *
 * Return 0 to detach. The plugin then holds no image pointer in any copy of its
 * args and, until its next init(), makes no image_lock, image_unlock,
 * journal_write_* or debug_* call and dereferences no image pointer. It stays
 * initialised and keeps its threads, and may keep calling set_switch_position,
 * request_plc_start, request_plc_stop, get_plc_state and log_*. Every image
 * access must stay on threads that stop_loop joins.
 *
 * The runtime never calls start_loop on a detached plugin. The next start
 * reloads the plugin configuration, which runs cleanup(), unloads the .so,
 * loads it again and calls init() with the new image.
 *
 * @return 0 when detached. Any other value, or a missing symbol, means the
 *         runtime runs cleanup() on the stop instead.
 */
typedef int (*plugin_detach_image_func_t)(void);

/**
 * Optional `int owns_mode_switch(void)`: declares the plugin a switch owner.
 * Resolved with dlsym after the other symbols; Python plugins define
 * owns_mode_switch() returning True instead.
 *
 * Called once per load, after the symbols load and before init(). A non-zero
 * return (Python: True) makes the plugin a switch owner from that moment, so no
 * start runs a scan until it has reported a position through
 * set_switch_position since its init(). A missing symbol, a zero return or a
 * Python exception declares nothing: a plugin still becomes an owner the first
 * time it reports a position, and stays one until it leaves the plugin
 * configuration.
 */
typedef int (*plugin_owns_mode_switch_func_t)(void);

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
    /* Optional; NULL means this plugin does not understand per-table sizes and
     * the image stays square for the run. */
    plugin_set_image_sizes_func_t set_image_sizes;
    plugin_detach_image_func_t detach_image;
    plugin_owns_mode_switch_func_t owns_mode_switch;
} plugin_funct_bundle_t;

// Plugin instance structure
typedef struct plugin_instance_s
{
    PluginManager *manager;
    python_binds_t *python_plugin;
    plugin_funct_bundle_t *native_plugin;
    // pthread_t thread;
    int running;
    /* Set by init(); cleared by cleanup. Tracked apart from `running`
     * so a partial init failure can roll back only the plugins that
     * actually completed init. */
    int initialized;
    /* An enabled plugin whose symbols failed to load (e.g. missing
     * runtime dep). Slot kept but its *_plugin pointers are NULL;
     * init/start/cycle skip it. Keeps the runtime out of ERROR. */
    int degraded;
    int stop_failed;
    int detached;
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
/** Append plugins from a secondary conf without tearing down already-
 *  loaded plugins. Returns 0 on success, -1 if any enabled plugin
 *  fails to load its .so. */
int plugin_driver_append_config(plugin_driver_t *driver, const char *config_file);

#define PLUGIN_HMI_NAME "hmi"
#define PLUGIN_HMI_PATH "build/hmi/libhmi_plugin.so"

/* Registers the HMI plugin under the name "hmi" when build/hmi/libhmi_plugin.so exists (no
 * config file names it). Its seal is checked before dlopen; a plugin that fails to load is
 * marked degraded. Returns 0, or -1 for a NULL driver. */
int plugin_driver_append_hmi(plugin_driver_t *driver);

/* Sets the program MD5 that plugin_runtime_args_t.program_md5 carries from the next
 * plugin_driver_init on; NULL or "" when no program is loaded. Copies at most 32 chars. */
void plugin_driver_set_program_md5(const char *md5);
int plugin_driver_init(plugin_driver_t *driver);
/* Roll back a partial init: walks plugins[] in reverse and calls
 * cleanup on every plugin whose `initialized` flag is set. Returns
 * the count cleaned up. */
int plugin_driver_cleanup_init(plugin_driver_t *driver);

/**
 * Ask every initialised, stopped plugin that exports detach_image to detach
 * from the I/O image.
 *
 * A slot whose detach_image returns 0 (native) or True (Python) is marked
 * `detached` and logged as "Plugin <name> detached from the I/O image". Any
 * other result or an exception logs "Plugin <name> did not detach; cleaning it
 * up" and leaves the slot attached. A slot without the symbol stays attached.
 *
 * @return the number of slots detached by this call
 */
int plugin_driver_detach_images(plugin_driver_t *driver);

/**
 * Does any plugin still hold the I/O image?
 *
 * True if and only if some slot is initialised and not detached. Asked before
 * releasing the image.
 *
 * Not the same as "still running": a native plugin with no `cleanup` symbol
 * keeps its by-value args copy until it is cleaned up either way.
 */
int plugin_driver_any_holds_image(plugin_driver_t *driver);

/* Clean up every initialised slot that is not detached, in reverse order, and
 * leave detached slots initialised. Returns the count cleaned up. */
int plugin_driver_cleanup_attached(plugin_driver_t *driver);
/* Call start_loop on every enabled plugin, skipping detached slots. */
int plugin_driver_start(plugin_driver_t *driver);
/* Call stop_loop on every running plugin. Returns 0 when all stopped and
 * non-zero when a Python stop_loop returned False or raised; each such slot is
 * marked `stop_failed` and keeps its state. Any other result is a stop. */
int plugin_driver_stop(plugin_driver_t *driver);
/* Stop, clean up and unload every plugin, detached ones included, then finalise
 * Python. Leaves everything loaded when a plugin failed to stop. */
void plugin_driver_destroy(plugin_driver_t *driver);

/* Release the Python GIL and remember the thread state so
 * plugin_driver_destroy can restore it before Py_FinalizeEx. Call once
 * from the MAIN thread after plugins are loaded. */
void plugin_driver_release_gil(void);

// Cycle hook functions for native plugins (called during PLC scan cycle)
// These iterate through all active native plugins and call their cycle hooks
// Plugins opt-in by implementing cycle_start/cycle_end; opt-out by not implementing them
void plugin_driver_cycle_start(plugin_driver_t *driver);
void plugin_driver_cycle_end(plugin_driver_t *driver);

/* ---- Retain store ------------------------------------------------------- */

/* The retain-store plugin (or NULL). First plugin exporting BOTH
 * retain_save and retain_load wins; later ones are logged and ignored. */
plugin_instance_t *plugin_driver_find_retain_store(plugin_driver_t *driver);

int plugin_driver_retain_save(plugin_instance_t *store, const uint8_t *blob, uint16_t len);
int plugin_driver_retain_load(plugin_instance_t *store, const char *program_md5, uint16_t md5_len,
                              uint8_t *out, uint16_t cap, uint16_t *out_len);
int plugin_driver_retain_flush(plugin_instance_t *store);

/**
 * Does every loaded plugin understand per-table image sizes?
 *
 * Asked once per program load, BEFORE the image is allocated, because the
 * answer decides how it is allocated. If any plugin says no, the image stays
 * SQUARE for that run -- every table the same length.
 *
 * That is not a preference. The shipped VPP plugins bound a byte index into
 * bool_output and a word index into int_output with the same `buffer_size`:
 * the largest table would be an out-of-bounds read on the smaller ones, and
 * the smallest would silently drop configured I/O. Only equal lengths keep one
 * bound honest.
 *
 * `first_without` receives the name of the first plugin that does not, so the
 * decision can be logged with a reason. It is the only way an operator can
 * tell the two modes apart.
 */
bool plugin_driver_all_understand_per_table_sizes(plugin_driver_t *driver,
                                                  const char **first_without);

// Route a command to a specific plugin by name (for async commands like scan)
int plugin_driver_execute_command(plugin_driver_t *driver, const char *plugin_name,
                                  const char *command_json, char *response, size_t response_size);

// Splice plugin get_stats JSON into an existing STATS response before
// the closing `}`. Trailing newline preserved. Returns new length.
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
