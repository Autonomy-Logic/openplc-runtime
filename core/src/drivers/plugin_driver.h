#ifndef PLUGIN_DRIVER_H
#define PLUGIN_DRIVER_H

#include <pthread.h>
#include "../lib/iec_types.h"
#include "../plc_app/plcapp_manager.h"
#include "plugin_config.h"
// #include "python_plugin_bridge.h"

// Maximum number of plugins
#define MAX_PLUGINS 16

typedef enum {
    PLUGIN_TYPE_PYTHON,
    PLUGIN_TYPE_NATIVE  
} plugin_type_t;

// Plugin configuration structure
// typedef struct {
//     char name[64];
//     char path[256];
//     int enabled;
//     plugin_type_t type;
// } plugin_config_t;

// Plugin instance structure
typedef struct {
    union {
        PluginManager *manager;
        // python_plugin_t *python_plugin;
    };
    pthread_t thread;
    int running;
    plugin_config_t config;
} plugin_instance_t;

// Driver structure
typedef struct {
    plugin_instance_t plugins[MAX_PLUGINS];
    int plugin_count;
    pthread_mutex_t buffer_mutex;
} plugin_driver_t;

// Buffer access functions for plugins
IEC_BOOL plugin_get_bool_input(int index, int bit);
void plugin_set_bool_output(int index, int bit, IEC_BOOL value);
IEC_BYTE plugin_get_byte_input(int index);
void plugin_set_byte_output(int index, IEC_BYTE value);
IEC_UINT plugin_get_int_input(int index);
void plugin_set_int_output(int index, IEC_UINT value);
IEC_UDINT plugin_get_dint_input(int index);
void plugin_set_dint_output(int index, IEC_UDINT value);
IEC_ULINT plugin_get_lint_input(int index);
void plugin_set_lint_output(int index, IEC_ULINT value);
IEC_UINT plugin_get_int_memory(int index);
void plugin_set_int_memory(int index, IEC_UINT value);
IEC_UDINT plugin_get_dint_memory(int index);
void plugin_set_dint_memory(int index, IEC_UDINT value);
IEC_ULINT plugin_get_lint_memory(int index);
void plugin_set_lint_memory(int index, IEC_ULINT value);

// Driver management functions
plugin_driver_t* plugin_driver_create(void);
int plugin_driver_load_config(plugin_driver_t *driver, const char *config_file);
int plugin_driver_init(plugin_driver_t *driver);
int plugin_driver_start(plugin_driver_t *driver);
int plugin_driver_stop(plugin_driver_t *driver);
void plugin_driver_destroy(plugin_driver_t *driver);

#endif // PLUGIN_DRIVER_H
