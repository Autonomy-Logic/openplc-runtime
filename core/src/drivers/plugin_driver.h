#ifndef PLUGIN_DRIVER_H
#define PLUGIN_DRIVER_H

#include "../lib/iec_types.h"
#include "../plc_app/plcapp_manager.h"
#include "plugin_config.h"
#include "python_plugin_bridge.h"
#include <pthread.h>

// Maximum number of plugins
#define MAX_PLUGINS 16

typedef enum
{
    PLUGIN_TYPE_PYTHON,
    PLUGIN_TYPE_NATIVE
} plugin_type_t;

typedef int (*plugin_init_func_t)(void *);
typedef void (*plugin_start_loop_func_t)();
typedef void (*plugin_stop_loop_func_t)();
typedef void (*plugin_run_cycle_func_t)();
typedef void (*plugin_cleanup_func_t)();

typedef struct
{
    plugin_init_func_t init;
    plugin_start_loop_func_t start;
    plugin_stop_loop_func_t stop;
    plugin_run_cycle_func_t run_cycle;
    plugin_cleanup_func_t cleanup;
} plugin_funct_bundle_t;

// Runtime buffer access structure for plugins
typedef struct
{
    // Buffer pointers
    IEC_BOOL *(*bool_input)[8];
    IEC_BOOL *(*bool_output)[8];
    IEC_BYTE **byte_input;
    IEC_BYTE **byte_output;
    IEC_UINT **int_input;
    IEC_UINT **int_output;
    IEC_UDINT **dint_input;
    IEC_UDINT **dint_output;
    IEC_ULINT **lint_input;
    IEC_ULINT **lint_output;
    IEC_UINT **int_memory;
    IEC_UDINT **dint_memory;
    IEC_ULINT **lint_memory;

    // Mutex functions
    int (*mutex_take)(pthread_mutex_t *mutex);
    int (*mutex_give)(pthread_mutex_t *mutex);
    pthread_mutex_t *buffer_mutex;
    char plugin_specific_config_file_path[256];

    // Buffer size information
    int buffer_size;
    int bits_per_buffer;
} plugin_runtime_args_t;

// Plugin instance structure
typedef struct plugin_instance_s
{
    PluginManager *manager;
    python_binds_t *python_plugin;
    // pthread_t thread;
    int running;
    plugin_config_t config;
} plugin_instance_t;

// Driver structure
typedef struct
{
    plugin_instance_t plugins[MAX_PLUGINS];
    int plugin_count;
    pthread_mutex_t buffer_mutex;
} plugin_driver_t;

// Driver management functions
plugin_driver_t *plugin_driver_create(void);
int plugin_driver_load_config(plugin_driver_t *driver, const char *config_file);
int plugin_driver_init(plugin_driver_t *driver);
int plugin_driver_start(plugin_driver_t *driver);
int plugin_driver_stop(plugin_driver_t *driver);
void plugin_driver_destroy(plugin_driver_t *driver);
int plugin_mutex_take(pthread_mutex_t *mutex);
int plugin_mutex_give(pthread_mutex_t *mutex);

// Python plugin functions
int python_plugin_get_symbols(plugin_instance_t *plugin);

// Runtime arguments generation
void *generate_structured_args_with_driver(plugin_type_t type, plugin_driver_t *driver,
                                           int plugin_index);
void free_structured_args(plugin_runtime_args_t *args);

#endif // PLUGIN_DRIVER_H
