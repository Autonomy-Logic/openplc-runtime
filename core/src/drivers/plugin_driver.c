#define PY_SSIZE_T_CLEAN

// Suppress _POSIX_C_SOURCE redefinition warning from Python.h on MSYS2/Cygwin
// Python.h defines _POSIX_C_SOURCE to 200809L which conflicts with system headers
#if defined(__GNUC__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wcpp"
#endif

#include <Python.h>

#if defined(__GNUC__)
#pragma GCC diagnostic pop
#endif

#include "../plc_app/image_tables.h"
#include "../plc_app/journal_buffer.h"
#include "../plc_app/utils/log.h"
#include "plugin_config.h"
#include "plugin_driver.h"
#include "plugin_utils.h"
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// External buffer declarations from image_tables.c
extern IEC_BOOL *bool_input[BUFFER_SIZE][8];
extern IEC_BOOL *bool_output[BUFFER_SIZE][8];
extern IEC_BYTE *byte_input[BUFFER_SIZE];
extern IEC_BYTE *byte_output[BUFFER_SIZE];
extern IEC_UINT *int_input[BUFFER_SIZE];
extern IEC_UINT *int_output[BUFFER_SIZE];
extern IEC_UDINT *dint_input[BUFFER_SIZE];
extern IEC_UDINT *dint_output[BUFFER_SIZE];
extern IEC_ULINT *lint_input[BUFFER_SIZE];
extern IEC_ULINT *lint_output[BUFFER_SIZE];
extern IEC_UINT *int_memory[BUFFER_SIZE];
extern IEC_UDINT *dint_memory[BUFFER_SIZE];
extern IEC_ULINT *lint_memory[BUFFER_SIZE];
extern IEC_BOOL *bool_memory[BUFFER_SIZE][8];
static PyThreadState *main_tstate = NULL;
static PyGILState_STATE gstate;
static int has_python_plugin = 0;

// Prototypes
static void python_plugin_cleanup(plugin_instance_t *plugin);

// Driver management functions
plugin_driver_t *plugin_driver_create(void)
{
    plugin_driver_t *driver = calloc(1, sizeof(plugin_driver_t));
    if (!driver)
    {
        return NULL;
    }

#if !defined(__CYGWIN__) && !defined(__MSYS__)
    // Initialize mutex with priority inheritance to prevent priority inversion
    // This ensures that when a lower-priority plugin thread holds the mutex,
    // it temporarily inherits the priority of any higher-priority thread
    // (like the PLC scan cycle thread) waiting for the mutex.
    // Note: Priority inheritance is not available on MSYS2/Cygwin
    pthread_mutexattr_t mutex_attr;
    if (pthread_mutexattr_init(&mutex_attr) != 0)
    {
        free(driver);
        return NULL;
    }

    if (pthread_mutexattr_setprotocol(&mutex_attr, PTHREAD_PRIO_INHERIT) != 0)
    {
        pthread_mutexattr_destroy(&mutex_attr);
        free(driver);
        return NULL;
    }

    if (pthread_mutex_init(&driver->buffer_mutex, &mutex_attr) != 0)
    {
        pthread_mutexattr_destroy(&mutex_attr);
        free(driver);
        return NULL;
    }

    pthread_mutexattr_destroy(&mutex_attr);
#else
    // On MSYS2/Cygwin, use a regular mutex without priority inheritance
    if (pthread_mutex_init(&driver->buffer_mutex, NULL) != 0)
    {
        free(driver);
        return NULL;
    }
#endif

    return driver;
}

// Mutex helper functions for plugins
int plugin_mutex_take(pthread_mutex_t *mutex)
{
    return pthread_mutex_lock(mutex);
}

int plugin_mutex_give(pthread_mutex_t *mutex)
{
    return pthread_mutex_unlock(mutex);
}

// Journal write wrapper functions for plugins
// These match the function pointer signatures in plugin_types.h and delegate
// to the journal_buffer.h API with proper type casting.

static int plugin_journal_write_bool(int type, int index, int bit, int value)
{
    return journal_write_bool((journal_buffer_type_t)type, (uint16_t)index, (uint8_t)bit,
                              value != 0);
}

static int plugin_journal_write_byte(int type, int index, int value)
{
    return journal_write_byte((journal_buffer_type_t)type, (uint16_t)index, (uint8_t)value);
}

static int plugin_journal_write_int(int type, int index, int value)
{
    return journal_write_int((journal_buffer_type_t)type, (uint16_t)index, (uint16_t)value);
}

static int plugin_journal_write_dint(int type, int index, unsigned int value)
{
    return journal_write_dint((journal_buffer_type_t)type, (uint16_t)index, (uint32_t)value);
}

static int plugin_journal_write_lint(int type, int index, unsigned long long value)
{
    return journal_write_lint((journal_buffer_type_t)type, (uint16_t)index, (uint64_t)value);
}

// Python capsule destructor for runtime args
// Breakpoint here to debug capsule issues
static void plugin_runtime_args_capsule_destructor(PyObject *capsule)
{
    plugin_runtime_args_t *args =
        (plugin_runtime_args_t *)PyCapsule_GetPointer(capsule, "openplc_runtime_args");
    if (args)
    {
        free_structured_args(args);
    }
}

// Create Python capsule with runtime arguments
static PyObject *create_python_runtime_args_capsule(plugin_runtime_args_t *args)
{
    if (!args)
    {
        return NULL;
    }

    // Create a capsule containing the runtime args pointer
    PyObject *capsule =
        PyCapsule_New(args, "openplc_runtime_args", plugin_runtime_args_capsule_destructor);
    if (!capsule)
    {
        // If capsule creation fails, we need to free the args manually
        free_structured_args(args);
        return NULL;
    }

    return capsule;
}

int plugin_driver_update_config(plugin_driver_t *driver, const char *config_file)
{
    if (!driver || !config_file)
    {
        return -1;
    }

    // Check if config file exists, if not copy from default
    if (access(config_file, F_OK) != 0)
    {
        printf("[PLUGIN]: Config file %s not found, copying from plugins_default.conf\n",
               config_file);

        // Check if default config exists
        if (access("plugins_default.conf", F_OK) != 0)
        {
            printf("[PLUGIN]: Default config file plugins_default.conf not found\n");
            return -1;
        }

        // Copy default config to target config file
        FILE *src = fopen("plugins_default.conf", "r");
        FILE *dst = fopen(config_file, "w");

        if (!src || !dst)
        {
            printf("[PLUGIN]: Failed to copy default config\n");
            if (src)
                fclose(src);
            if (dst)
                fclose(dst);
            return -1;
        }

        char buffer[1024];
        size_t bytes;
        while ((bytes = fread(buffer, 1, sizeof(buffer), src)) > 0)
        {
            fwrite(buffer, 1, bytes, dst);
        }

        fclose(src);
        fclose(dst);
        printf("[PLUGIN]: Successfully copied default config to %s\n", config_file);
    }

    plugin_config_t configs[MAX_PLUGINS];
    int config_count = parse_plugin_config(config_file, configs, MAX_PLUGINS);
    if (config_count < 0)
    {
        return -1;
    }

    driver->plugin_count = config_count;
    has_python_plugin    = 0;
    for (int w = 0; w < config_count; w++)
    {
        memcpy(&driver->plugins[w].config, &configs[w], sizeof(plugin_config_t));
        if (configs[w].type == PLUGIN_TYPE_PYTHON)
        {
            has_python_plugin = 1;
        }
    }
    return 0;
}

int plugin_driver_load_config(plugin_driver_t *driver, const char *config_file)
{
    if (!driver || !config_file)
    {
        return -1;
    }

    plugin_driver_update_config(driver, config_file);

    // Now retrieve the function symbols and initialize
    // struct plugin_instance_t para cada plugin.
    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];

        if (plugin->config.type == PLUGIN_TYPE_PYTHON)
        {
            if (python_plugin_get_symbols(plugin) != 0)
            {
                fprintf(stderr, "Failed to get Python plugin symbols for: %s\n",
                        plugin->config.path);
                return -1;
            }
        }
        else if (plugin->config.type == PLUGIN_TYPE_NATIVE)
        {
            if (native_plugin_get_symbols(plugin) != 0)
            {
                fprintf(stderr, "Failed to get native plugin symbols for: %s\n",
                        plugin->config.path);
                return -1;
            }
        }
    }

    return 0;
}

// Send to plugin init function all args
int plugin_driver_init(plugin_driver_t *driver)
{
    if (!driver)
    {
        return -1;
    }

    // Only acquire Python GIL if we have Python plugins and Python is initialized
    // Initialize to PyGILState_LOCKED (0) to satisfy compiler warning
    PyGILState_STATE local_gstate = PyGILState_LOCKED;
    int have_gil = has_python_plugin && Py_IsInitialized();
    if (have_gil)
    {
        local_gstate = PyGILState_Ensure();
    }

    // #chamdo a função init de cada plugin aqui
    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];

        // Skip disabled plugins
        if (!plugin->config.enabled)
        {
            printf("[PLUGIN]: Skipping disabled plugin: %s\n", plugin->config.name);
            continue;
        }

        if (plugin->config.type == PLUGIN_TYPE_PYTHON && plugin->python_plugin &&
            plugin->python_plugin->pFuncInit)
        {
            // Generate structured args for Python plugin
            PyObject *args =
                (PyObject *)generate_structured_args_with_driver(PLUGIN_TYPE_PYTHON, driver, i);
            if (!args)
            {
                fprintf(stderr, "Failed to generate runtime args for plugin: %s\n",
                        plugin->config.name);

                if (have_gil)
                {
                    PyGILState_Release(local_gstate);
                }
                return -1;
            }
            // Call the Python init function with proper capsule
            PyObject *result =
                PyObject_CallFunctionObjArgs(plugin->python_plugin->pFuncInit, args, NULL);

            // Store the capsule reference for the lifetime of the plugin
            plugin->python_plugin->args_capsule = args;

            if (!result)
            {
                PyErr_Print();
                fprintf(stderr, "Python init function failed for plugin: %s\n",
                        plugin->config.name);

                if (have_gil)
                {
                    PyGILState_Release(local_gstate);
                }
                return -1;
            }
            Py_DECREF(result);
        }
        else if (plugin->config.type == PLUGIN_TYPE_NATIVE && plugin->native_plugin &&
                 plugin->native_plugin->init)
        {
            // Generate structured args for native plugin
            plugin_runtime_args_t *args =
                (plugin_runtime_args_t *)generate_structured_args_with_driver(PLUGIN_TYPE_NATIVE,
                                                                              driver, i);
            if (!args)
            {
                fprintf(stderr, "Failed to generate runtime args for native plugin: %s\n",
                        plugin->config.name);
                if (have_gil)
                {
                    PyGILState_Release(local_gstate);
                }
                return -1;
            }

            // Call the native init function
            int result = plugin->native_plugin->init(args);
            if (result != 0)
            {
                fprintf(stderr, "Native init function failed for plugin: %s (returned %d)\n",
                        plugin->config.name, result);
                free_structured_args(args);
                if (have_gil)
                {
                    PyGILState_Release(local_gstate);
                }
                return -1;
            }

            // Free the args after successful initialization
            free_structured_args(args);
        }
    }

    if (have_gil)
    {
        PyGILState_Release(local_gstate);
    }

    return 0;
}

// Call the thread function for each plugin
int plugin_driver_start(plugin_driver_t *driver)
{
    if (!driver)
    {
        return -1;
    }

    if (driver->plugin_count == 0)
    {
        printf("[PLUGIN]: No plugins to start.\n");
        return 0;
    }

    // Only manage Python GIL if we have Python plugins and Python is initialized
    if (has_python_plugin && Py_IsInitialized())
    {
        gstate      = PyGILState_Ensure();
        main_tstate = PyEval_SaveThread();
    }

    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];

        // Skip disabled plugins
        if (!plugin->config.enabled)
        {
            printf("[PLUGIN]: Skipping disabled plugin during start: %s\n", plugin->config.name);
            continue;
        }

        switch (plugin->config.type)
        {
        case PLUGIN_TYPE_PYTHON:
        {
            // Python plugins run asynchronously in their own threads.
            // NOTE: The thread is created python-side
            if (plugin->python_plugin && plugin->python_plugin->pFuncStart)
            {
                // Acquire GIL for this specific Python call
                PyGILState_STATE local_gil = PyGILState_Ensure();
                PyObject *res              = PyObject_CallNoArgs(plugin->python_plugin->pFuncStart);
                if (!res)
                {
                    PyErr_Print();
                    fprintf(stderr, "Python start call failed for plugin: %s\n",
                            plugin->config.name);
                }
                else
                {
                    printf("[PLUGIN]: Plugin %s started successfully.\n", plugin->config.name);
                }
                Py_DECREF(
                    res); // There's no problem in calling DECREF here because it only
                          // handles the returned object from start_loop, not the function itself
                PyGILState_Release(local_gil);

                plugin->running = 1;
            }
            else
            {
                fprintf(stderr, "Python plugin %s does not have a start_loop function.\n",
                        plugin->config.name);
            }
        }
        break;

        case PLUGIN_TYPE_NATIVE:
        {
            // Native plugins run synchronously - call start_loop if available
            if (plugin->native_plugin && plugin->native_plugin->start)
            {
                plugin->native_plugin->start();
                printf("[PLUGIN]: Native plugin %s started successfully.\n", plugin->config.name);
                plugin->running = 1;
            }
            else
            {
                fprintf(stderr, "Native plugin %s does not have a start_loop function.\n",
                        plugin->config.name);
            }
        }
        break;

        default:
            break;
        }
    }
    // Don't call PyGILState_Release here since we used PyEval_SaveThread
    // The GIL will be restored in plugin_driver_destroy
    return 0;
}

int plugin_driver_stop(plugin_driver_t *driver)
{
    printf("[PLUGIN]: Stopping all plugins...\n");
    if (!driver)
    {
        return -1;
    }

    if (driver->plugin_count == 0)
    {
        printf("[PLUGIN]: No plugins to stop.\n");
        return 0;
    }

    // Only acquire Python GIL if we have Python plugins and Python is initialized
    PyGILState_STATE local_gstate;
    int need_gil = has_python_plugin && Py_IsInitialized();
    if (need_gil)
    {
        local_gstate = PyGILState_Ensure();
    }

    // Signal all plugins to stop
    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];
        
        // Skip disabled plugins
        if (!plugin->config.enabled)
        {
            printf("[PLUGIN]: Skipping disabled plugin during stop: %s\n", plugin->config.name);
            continue;
        }
        
        printf("[PLUGIN]: Stopping plugin %d/%d: %s\n", i + 1, driver->plugin_count,
               driver->plugins[i].config.name);
        if (plugin->python_plugin && plugin->python_plugin->pFuncStop &&
            plugin->running)
        {
            plugin_instance_t *plugin = &driver->plugins[i];
            if (plugin->config.enabled == 0)
            {
                printf("[PLUGIN]: Plugin %s is disabled, skipping stop.\n", plugin->config.name);
                continue;
            }

            PyObject *res = PyObject_CallNoArgs(driver->plugins[i].python_plugin->pFuncStop);
            if (!res)
            {
                PyErr_Print();
                fprintf(stderr, "Python stop call failed for plugin: %s\n", plugin->config.name);
            }
            else
            {
                printf("[PLUGIN]: Plugin %s stopped successfully.\n", plugin->config.name);
            }
            Py_DECREF(res);
            printf("[PLUGIN]: Plugin %s stopped...\n", driver->plugins[i].config.name);
            plugin->running = 0;
        }

        else if (plugin->native_plugin && plugin->native_plugin->stop &&
                 plugin->running)
        {
            plugin->native_plugin->stop();
            printf("[PLUGIN]: Native plugin %s stopped successfully.\n", plugin->config.name);
            plugin->running = 0;
        }
        // Plugin manager only handles destruction, not stopping
    }

    if (need_gil)
    {
        PyGILState_Release(local_gstate);
    }

    return 0;
}

int plugin_driver_restart(plugin_driver_t *driver)
{
    if (!driver)
    {
        return -1;
    }

    printf("[PLUGIN]: Restarting all plugins...\n");

    // Stop all running plugins first
    if (plugin_driver_stop(driver) != 0)
    {
        fprintf(stderr, "[PLUGIN]: Failed to stop plugins during restart\n");
        return -1;
    }

    // Clean up plugins without destroying the driver
    // Note: No need for GIL here as stop() already handled Python operations
    if (has_python_plugin && Py_IsInitialized())
    {
        gstate = PyGILState_Ensure();
        for (int i = 0; i < driver->plugin_count; i++)
        {
            plugin_instance_t *plugin = &driver->plugins[i];
            if (plugin->python_plugin)
            {
                python_plugin_cleanup(plugin);
            }
        }
        PyGILState_Release(gstate);
    }

    // CRITICAL: Reload configuration from plugins.conf file
    printf("[PLUGIN]: Reloading plugin configuration...\n");
    if (plugin_driver_load_config(driver, "plugins.conf") != 0)
    {
        fprintf(stderr, "[PLUGIN]: Failed to reload plugin configuration during restart\n");
        return -1;
    }

    // Reinitialize all plugins (only enabled ones)
    if (plugin_driver_init(driver) != 0)
    {
        fprintf(stderr, "[PLUGIN]: Failed to reinitialize plugins during restart\n");
        return -1;
    }

    // Restart all plugins (only enabled ones)
    if (plugin_driver_start(driver) != 0)
    {
        fprintf(stderr, "[PLUGIN]: Failed to start plugins during restart\n");
        return -1;
    }

    printf("[PLUGIN]: All plugins restarted successfully\n");
    return 0;
}

void plugin_driver_destroy(plugin_driver_t *driver)
{
    if (!driver)
    {
        return;
    }

    if (driver->plugin_count == 0)
    {
        printf("[PLUGIN]: No plugins to destroy.\n");
        pthread_mutex_destroy(&driver->buffer_mutex);
        free(driver);
        return;
    }

    // Check if Python is initialized before any Python operations
    int python_initialized = has_python_plugin && Py_IsInitialized();
    // Initialize to PyGILState_LOCKED (0) to satisfy compiler warning
    PyGILState_STATE local_gstate = PyGILState_LOCKED;

    if (python_initialized)
    {
        local_gstate = PyGILState_Ensure();
    }

    plugin_driver_stop(driver);

    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];
        if (plugin->python_plugin && python_initialized)
        {
            python_plugin_cleanup(plugin);
        }
        if (plugin->native_plugin)
        {
            // Call cleanup function if available
            if (plugin->native_plugin->cleanup)
            {
                plugin->native_plugin->cleanup();
                printf("[PLUGIN]: Native plugin %s cleaned up successfully.\n",
                       plugin->config.name);
            }
            // Close the shared library handle
            if (plugin->native_plugin->handle)
            {
                dlclose(plugin->native_plugin->handle);
                plugin->native_plugin->handle = NULL;
            }

            free(plugin->native_plugin);
            plugin->native_plugin = NULL;
        }
    }

    if (python_initialized)
    {
        PyGILState_Release(local_gstate);
        if (main_tstate != NULL)
        {
            PyEval_RestoreThread(main_tstate);
        }
        Py_FinalizeEx();
    }

    pthread_mutex_destroy(&driver->buffer_mutex);

    free(driver);
}

// Runtime arguments generation functions

/**
 * @brief Generate structured arguments for plugin initialization
 *
 * This function creates a structured argument containing all runtime buffers,
 * mutex functions, and metadata needed by external plugins.
 *
 * @param type Type of plugin (PLUGIN_TYPE_PYTHON or PLUGIN_TYPE_NATIVE)
 * @param driver Pointer to plugin driver (for buffer mutex)
 * @return Pointer to allocated structure/capsule, or NULL on error
 *
 * For PLUGIN_TYPE_NATIVE: Returns plugin_runtime_args_t*
 * For PLUGIN_TYPE_PYTHON: Returns PyObject* (PyCapsule containing plugin_runtime_args_t*)
 */
void *generate_structured_args_with_driver(plugin_type_t type, plugin_driver_t *driver,
                                           int plugin_index)
{
    printf("[PLUGIN]: Generating structured args for plugin type %d\n", type);

    if (!driver)
    {
        fprintf(stderr, "[PLUGIN]: Error - driver is NULL\n");
        return NULL;
    }

    plugin_runtime_args_t *args = malloc(sizeof(plugin_runtime_args_t));
    if (!args)
    {
        fprintf(stderr, "[PLUGIN]: Error - failed to allocate memory for runtime args\n");
        return NULL;
    }

    printf("[PLUGIN]: Allocated runtime args structure (size: %zu bytes)\n",
           sizeof(plugin_runtime_args_t));

    // Initialize all buffer pointers
    args->bool_input  = bool_input;
    args->bool_output = bool_output;
    args->byte_input  = byte_input;
    args->byte_output = byte_output;
    args->int_input   = int_input;
    args->int_output  = int_output;
    args->dint_input  = dint_input;
    args->dint_output = dint_output;
    args->lint_input  = lint_input;
    args->lint_output = lint_output;
    args->int_memory  = int_memory;
    args->dint_memory = dint_memory;
    args->lint_memory = lint_memory;
    args->bool_memory = bool_memory;

    // Initialize mutex functions
    args->mutex_take = plugin_mutex_take;
    args->mutex_give = plugin_mutex_give;
    args->get_var_list = get_var_list;
    args->get_var_size = get_var_size;
    args->get_var_count = get_var_count;
    // Set buffer mutex from driver
    args->buffer_mutex = &driver->buffer_mutex;

    // Initialize plugin specific config path as empty
    memset(args->plugin_specific_config_file_path, '\0',
           sizeof(args->plugin_specific_config_file_path));

    memcpy(args->plugin_specific_config_file_path,
           driver->plugins[plugin_index].config.plugin_related_config_path,
           sizeof(driver->plugins[plugin_index].config.plugin_related_config_path));

    // Initialize buffer size info
    args->buffer_size     = BUFFER_SIZE;
    args->bits_per_buffer = 8;

    // Initialize logging functions
    args->log_info  = log_info;
    args->log_debug = log_debug;
    args->log_warn  = log_warn;
    args->log_error = log_error;

    // Initialize journal write functions for race-condition-free buffer writes
    args->journal_write_bool = plugin_journal_write_bool;
    args->journal_write_byte = plugin_journal_write_byte;
    args->journal_write_int  = plugin_journal_write_int;
    args->journal_write_dint = plugin_journal_write_dint;
    args->journal_write_lint = plugin_journal_write_lint;

    // printf("[PLUGIN]: Runtime args initialized:\n");
    // printf("[PLUGIN]:   buffer_size = %d\n", args->buffer_size);
    // printf("[PLUGIN]:   bits_per_buffer = %d\n", args->bits_per_buffer);
    // printf("[PLUGIN]:   buffer_mutex = %p\n", (void *)args->buffer_mutex);
    // printf("[PLUGIN]:   bool_input = %p\n", (void *)args->bool_input);
    // printf("[PLUGIN]:   mutex_take = %p\n", (void *)args->mutex_take);
    // printf("[PLUGIN]:   mutex_give = %p\n", (void *)args->mutex_give);

    // Validate critical pointers
    if (!args->buffer_mutex)
    {
        fprintf(stderr, "[PLUGIN]: Error - buffer_mutex is NULL\n");
        free(args);
        return NULL;
    }

    if (!args->mutex_take || !args->mutex_give)
    {
        fprintf(stderr, "[PLUGIN]: Error - mutex function pointers are NULL\n");
        free(args);
        return NULL;
    }

    switch (type)
    {
    case PLUGIN_TYPE_NATIVE:
        printf("[PLUGIN]: Returning native plugin args\n");
        // For native plugins, return the structure directly
        return args;

    case PLUGIN_TYPE_PYTHON:
        printf("[PLUGIN]: Creating Python capsule for args\n");
        // For Python plugins, wrap in a PyCapsule
        PyObject *capsule = create_python_runtime_args_capsule(args);
        if (!capsule)
        {
            fprintf(stderr, "[PLUGIN]: Error - failed to create Python capsule\n");
            // Note: create_python_runtime_args_capsule already freed args on failure
            return NULL;
        }
        printf("[PLUGIN]: Python capsule created successfully\n");
        return capsule;

    default:
        fprintf(stderr, "[PLUGIN]: Error - unknown plugin type: %d\n", type);
        // Unknown type, clean up and return NULL
        free(args);
        return NULL;
    }
}

// Free structured arguments
void free_structured_args(plugin_runtime_args_t *args)
{
    if (args)
    {
        // No dynamic allocations inside the structure to free
        // Just free the main structure
        free(args);
    }
}

int python_plugin_get_symbols(plugin_instance_t *plugin)
{
    if (!plugin || plugin->config.path[0] == '\0')
    {
        return -1;
    }

    // Allocate python binds structure
    python_binds_t *py_binds = calloc(1, sizeof(python_binds_t));
    if (!py_binds)
    {
        return -1;
    }

    // Initialize Python if not already initialized
    if (!Py_IsInitialized())
    {
        Py_Initialize();
    }

    // Extract module name from plugin path
    // Remove .py extension and directory path if present
    char module_name[256];
    const char *filename = strrchr(plugin->config.path, '/');
    if (filename)
    {
        filename++; // Skip the '/'
    }
    else
    {
        filename = plugin->config.path;
    }

    // Copy filename without .py extension
    strncpy(module_name, filename, sizeof(module_name) - 1);
    module_name[sizeof(module_name) - 1] = '\0';
    char *dot                            = strrchr(module_name, '.');
    if (dot && strcmp(dot, ".py") == 0)
    {
        *dot = '\0';
    }

    // Add plugin directory to Python path
    char python_path_cmd[512];
    const char *plugin_dir = strrchr(plugin->config.path, '/');
    if (plugin_dir)
    {
        int dir_len = plugin_dir - plugin->config.path;
        char dir_path[256];
        strncpy(dir_path, plugin->config.path, dir_len);
        dir_path[dir_len] = '\0';
        snprintf(python_path_cmd, sizeof(python_path_cmd), "import sys; sys.path.insert(0, '%s')",
                 dir_path);
    }
    else
    {
        snprintf(python_path_cmd, sizeof(python_path_cmd), "import sys; sys.path.insert(0, '.')");
    }

    PyRun_SimpleString(python_path_cmd);

    // Setup virtual environment if specified
    if (strlen(plugin->config.venv_path) > 0)
    {
        // Construct the venv site-packages path
        char venv_site_packages[512];
        snprintf(venv_site_packages, sizeof(venv_site_packages), "%s/lib/python%d.%d/site-packages",
                 plugin->config.venv_path, PY_MAJOR_VERSION, PY_MINOR_VERSION);
        // Get sys.path
        PyObject *sys_path = PySys_GetObject("path");
        if (sys_path && PyList_Check(sys_path))
        {
            PyObject *venv_path_obj = PyUnicode_FromString(venv_site_packages);
            int found               = PySequence_Contains(sys_path, venv_path_obj);
            if (found == 0)
            { // Not found
                if (PyList_Insert(sys_path, 0, venv_path_obj) != 0)
                {
                    fprintf(stderr, "Failed to insert venv path into sys.path for plugin: %s\n",
                            plugin->config.name);
                    Py_DECREF(venv_path_obj);
                    free(py_binds);
                    return -1;
                }
            }
            Py_DECREF(venv_path_obj);
        }
        else
        {
            fprintf(stderr, "Failed to get sys.path for plugin: %s\n", plugin->config.name);
            free(py_binds);
            return -1;
        }
        printf("[PLUGIN] Using venv for %s: %s\n", plugin->config.name, venv_site_packages);
    }

    // Load the Python module
    py_binds->pModule = PyImport_ImportModule(module_name);
    if (!py_binds->pModule)
    {
        fprintf(stderr, "Failed to load Python module '%s' from path '%s'\n", module_name,
                plugin->config.path);
        PyErr_Print();
        free(py_binds);
        return -1;
    }

    // Get function references based on python_binds_t structure
    py_binds->pFuncInit = PyObject_GetAttrString(py_binds->pModule, "init");
    if (!py_binds->pFuncInit || !PyCallable_Check(py_binds->pFuncInit))
    {
        fprintf(stderr,
                "Error: 'init' function not found or not callable in module '%s' - this function "
                "is required\n",
                module_name);
        Py_XDECREF(py_binds->pModule);
        free(py_binds);
        return -1;
    }

    py_binds->pFuncStart = PyObject_GetAttrString(py_binds->pModule, "start_loop");
    if (!py_binds->pFuncStart || !PyCallable_Check(py_binds->pFuncStart))
    {
        // start_loop is optional
        Py_XDECREF(py_binds->pFuncStart);
        py_binds->pFuncStart = NULL;
    }

    py_binds->pFuncStop = PyObject_GetAttrString(py_binds->pModule, "stop_loop");
    if (!py_binds->pFuncStop || !PyCallable_Check(py_binds->pFuncStop))
    {
        // stop_loop is optional
        Py_XDECREF(py_binds->pFuncStop);
        py_binds->pFuncStop = NULL;
    }

    py_binds->pFuncCleanup = PyObject_GetAttrString(py_binds->pModule, "cleanup");
    if (!py_binds->pFuncCleanup || !PyCallable_Check(py_binds->pFuncCleanup))
    {
        // cleanup is optional
        Py_XDECREF(py_binds->pFuncCleanup);
        py_binds->pFuncCleanup = NULL;
    }

    // Store the python binds in the plugin instance
    plugin->python_plugin = py_binds;

    printf("Python plugin '%s' symbols loaded successfully\n", module_name);
    printf("  - init: %s\n", py_binds->pFuncInit ? "(PASS)" : "(FAIL)");
    printf("  - start_loop: %s\n", py_binds->pFuncStart ? "(PASS)" : "(FAIL)");
    printf("  - stop_loop: %s\n", py_binds->pFuncStop ? "(PASS)" : "(FAIL)");
    printf("  - cleanup: %s\n", py_binds->pFuncCleanup ? "(PASS)" : "(FAIL)");

    return 0;
}

int native_plugin_get_symbols(plugin_instance_t *plugin)
{
    if (!plugin || plugin->config.path[0] == '\0')
    {
        return -1;
    }

    // Allocate native plugin function bundle
    plugin_funct_bundle_t *native_bundle = calloc(1, sizeof(plugin_funct_bundle_t));
    if (!native_bundle)
    {
        return -1;
    }

    // Load the shared library
    void *handle = dlopen(plugin->config.path, RTLD_LOCAL | RTLD_NOW);
    if (!handle)
    {
        const char *err = dlerror();
        fprintf(stderr, "Failed to load native plugin '%s': %s\n",
                plugin->config.path, err ? err : "unknown error");
        log_error("Failed to load native plugin '%s': %s",
                  plugin->config.path, err ? err : "unknown error");
#if defined(__CYGWIN__) || defined(_WIN32)
        if (strstr(plugin->config.name, "ethercat") != NULL)
        {
            log_error("The EtherCAT plugin requires Npcap (https://npcap.com) to access "
                      "the network interface. Please install Npcap and restart the runtime.");
        }
#endif
        free(native_bundle);
        return -1;
    }

    // Store the handle in the native bundle
    native_bundle->handle = handle;

    // Clear any existing error
    dlerror();

    // Get function pointers for required functions
    // init function is required
    native_bundle->init = (plugin_init_func_t)dlsym(handle, "init");
    if (!native_bundle->init)
    {
        fprintf(stderr, "Error: 'init' function not found in native plugin '%s': %s\n",
                plugin->config.path, dlerror());
        dlclose(handle);
        free(native_bundle);
        return -1;
    }

    // Optional functions - set to NULL if not found
    native_bundle->start = (plugin_start_loop_func_t)dlsym(handle, "start_loop");
    if (!native_bundle->start)
    {
        fprintf(stderr,
                "Warning: 'start_loop' function not found in native plugin '%s' (optional)\n",
                plugin->config.path);
    }

    native_bundle->stop = (plugin_stop_loop_func_t)dlsym(handle, "stop_loop");
    if (!native_bundle->stop)
    {
        fprintf(stderr,
                "Warning: 'stop_loop' function not found in native plugin '%s' (optional)\n",
                plugin->config.path);
    }

    native_bundle->cycle_start = (plugin_cycle_start_func_t)dlsym(handle, "cycle_start");
    if (!native_bundle->cycle_start)
    {
        fprintf(stderr,
                "Warning: 'cycle_start' function not found in native plugin '%s' (optional)\n",
                plugin->config.path);
    }

    native_bundle->cycle_end = (plugin_cycle_end_func_t)dlsym(handle, "cycle_end");
    if (!native_bundle->cycle_end)
    {
        fprintf(stderr,
                "Warning: 'cycle_end' function not found in native plugin '%s' (optional)\n",
                plugin->config.path);
    }

    native_bundle->cleanup = (plugin_cleanup_func_t)dlsym(handle, "cleanup");
    if (!native_bundle->cleanup)
    {
        fprintf(stderr, "Warning: 'cleanup' function not found in native plugin '%s' (optional)\n",
                plugin->config.path);
    }

    native_bundle->execute_command =
        (plugin_execute_command_func_t)dlsym(handle, "execute_command");
    if (!native_bundle->execute_command)
    {
        fprintf(stderr,
                "Warning: 'execute_command' function not found in native plugin '%s' (optional)\n",
                plugin->config.path);
    }

    // Store the native bundle and handle in the plugin instance
    plugin->native_plugin = native_bundle;

    printf("Native plugin '%s' symbols loaded successfully\n", plugin->config.path);
    printf("  - init: (PASS)\n");
    printf("  - start_loop: %s\n", native_bundle->start ? "(PASS)" : "(FAIL)");
    printf("  - stop_loop: %s\n", native_bundle->stop ? "(PASS)" : "(FAIL)");
    printf("  - cycle_start: %s\n", native_bundle->cycle_start ? "(PASS)" : "(FAIL)");
    printf("  - cycle_end: %s\n", native_bundle->cycle_end ? "(PASS)" : "(FAIL)");
    printf("  - cleanup: %s\n", native_bundle->cleanup ? "(PASS)" : "(FAIL)");
    printf("  - execute_command: %s\n", native_bundle->execute_command ? "(PASS)" : "(FAIL)");

    return 0;
}

// Python plugin cycle function
void python_plugin_cycle(plugin_instance_t *plugin)
{
    (void)plugin; // Suppress unused parameter warning
    // In a real implementation, you'd retrieve the python_plugin_t structure
    // and call the cycle function
}

// Call cycle_start for all active native plugins that have registered the hook
// This should be called at the beginning of each PLC scan cycle, before PLC logic execution
// Plugins opt-in by implementing cycle_start(); opt-out by not implementing it (NULL pointer)
void plugin_driver_cycle_start(plugin_driver_t *driver)
{
    if (!driver || driver->plugin_count == 0)
    {
        return;
    }

    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];

        // Skip disabled or non-running plugins
        if (!plugin->config.enabled || !plugin->running)
        {
            continue;
        }

        // Only native plugins support cycle hooks (they can run in real-time)
        if (plugin->config.type == PLUGIN_TYPE_NATIVE && plugin->native_plugin &&
            plugin->native_plugin->cycle_start)
        {
            plugin->native_plugin->cycle_start();
        }
    }
}

// Call cycle_end for all active native plugins that have registered the hook
// This should be called at the end of each PLC scan cycle, after PLC logic execution
// Plugins opt-in by implementing cycle_end(); opt-out by not implementing it (NULL pointer)
void plugin_driver_cycle_end(plugin_driver_t *driver)
{
    if (!driver || driver->plugin_count == 0)
    {
        return;
    }

    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];

        // Skip disabled or non-running plugins
        if (!plugin->config.enabled || !plugin->running)
        {
            continue;
        }

        // Only native plugins support cycle hooks (they can run in real-time)
        if (plugin->config.type == PLUGIN_TYPE_NATIVE && plugin->native_plugin &&
            plugin->native_plugin->cycle_end)
        {
            plugin->native_plugin->cycle_end();
        }
    }
}

// Route a command to a specific plugin by name
int plugin_driver_execute_command(plugin_driver_t *driver, const char *plugin_name,
                                  const char *command_json, char *response, size_t response_size)
{
    if (!driver || !plugin_name || !command_json)
    {
        snprintf(response, response_size, "{\"error\":\"invalid arguments\"}");
        return -1;
    }

    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];
        if (!plugin->config.enabled)
            continue;
        if (strcmp(plugin->config.name, plugin_name) != 0)
            continue;

        if (plugin->config.type == PLUGIN_TYPE_NATIVE && plugin->native_plugin &&
            plugin->native_plugin->execute_command)
        {
            return plugin->native_plugin->execute_command(command_json, response, response_size);
        }

        snprintf(response, response_size,
                 "{\"error\":\"plugin does not support execute_command\"}");
        return -1;
    }

    snprintf(response, response_size, "{\"error\":\"plugin not found\"}");
    return -1;
}

// Cleanup Python plugin
static void python_plugin_cleanup(plugin_instance_t *plugin)
{
    // Cleanup Python resources
    if (plugin && plugin->python_plugin && plugin->config.enabled)
    {
        // Call cleanup function if available
        if (plugin->python_plugin->pFuncCleanup)
        {
            PyObject *res = PyObject_CallFunctionObjArgs(plugin->python_plugin->pFuncCleanup, NULL);
            if (!res)
            {
                PyErr_Print();
                fprintf(stderr, "Python cleanup call failed for plugin: %s\n", plugin->config.name);
            }
            else
            {
                printf("[PLUGIN]: Plugin %s cleaned up successfully.\n", plugin->config.name);
            }
            Py_DECREF(res);
        }

        // Decrement references to Python objects
        Py_XDECREF(plugin->python_plugin->pFuncInit);
        Py_XDECREF(plugin->python_plugin->pFuncStart);
        Py_XDECREF(plugin->python_plugin->pFuncStop);
        Py_XDECREF(plugin->python_plugin->pFuncCleanup);
        Py_XDECREF(plugin->python_plugin->pModule);
        Py_XDECREF(plugin->python_plugin->args_capsule);

        free(plugin->python_plugin);
        plugin->python_plugin = NULL;
    }
}
