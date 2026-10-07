// SPDX-License-Identifier: MIT
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
 * load are both required. */
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
    /* Set by init(); cleared by cleanup. Tracked apart from `running`
     * so a partial init failure can roll back only the plugins that
     * actually completed init. */
    int initialized;
    /* An enabled plugin whose symbols failed to load (e.g. missing
     * runtime dep). Slot kept but its *_plugin pointers are NULL;
     * init/start/cycle skip it. Keeps the runtime out of ERROR. */
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
/** Append plugins from a secondary conf without tearing down already-
 *  loaded plugins. Returns 0 on success, -1 if any enabled plugin
 *  fails to load its .so. */
int plugin_driver_append_config(plugin_driver_t *driver, const char *config_file);
int plugin_driver_init(plugin_driver_t *driver);
/* Roll back a partial init: walks plugins[] in reverse and calls
 * cleanup on every plugin whose `initialized` flag is set. Returns
 * the count cleaned up. */
int plugin_driver_cleanup_init(plugin_driver_t *driver);
int plugin_driver_start(plugin_driver_t *driver);
int plugin_driver_stop(plugin_driver_t *driver);
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
