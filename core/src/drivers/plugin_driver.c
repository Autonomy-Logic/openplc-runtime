#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include "../plc_app/image_tables.h"
#include "plugin_config.h"
#include "plugin_driver.h"
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
static PyThreadState *main_tstate = NULL;
static PyGILState_STATE gstate;

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

    // Initialize mutex
    if (pthread_mutex_init(&driver->buffer_mutex, NULL) != 0)
    {
        free(driver);
        return NULL;
    }

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

int plugin_driver_load_config(plugin_driver_t *driver, const char *config_file)
{
    if (!driver || !config_file)
    {
        return -1;
    }

    plugin_config_t configs[MAX_PLUGINS];
    int config_count = parse_plugin_config(config_file, configs, MAX_PLUGINS);
    if (config_count < 0)
    {
        return -1;
    }

    driver->plugin_count = config_count;
    for (int w = 0; w < config_count; w++)
    {
        memcpy(&driver->plugins[w].config, &configs[w], sizeof(plugin_config_t));
    }

    // Agora leio todos os simbolos que preciso (init, start, stop, cycle, cleanup) e adiciono na
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
                plugin_manager_destroy(plugin->manager);
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

    // #chamdo a função init de cada plugin aqui
    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];
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
                return -1;
            }
            // Call the Python init function with proper capsule
            PyObject *result =
                PyObject_CallFunctionObjArgs(plugin->python_plugin->pFuncInit, args, NULL);
            Py_SET_REFCNT(args, UINT64_MAX);

            if (!result)
            {
                PyErr_Print();
                fprintf(stderr, "Python init function failed for plugin: %s\n",
                        plugin->config.name);
                return -1;
            }
            Py_DECREF(result);
        }
        else if (plugin->config.type == PLUGIN_TYPE_NATIVE && plugin->manager)
        {
            // TODO: Implement native plugin initialization
        }
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

    main_tstate = PyEval_SaveThread();
    gstate      = PyGILState_Ensure();

    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];
        switch (plugin->config.type)
        {
        case PLUGIN_TYPE_PYTHON:
        {
            // Python plugins run asynchronously in their own threads.
            // NOTE: The thread is created python-side
            if (plugin->python_plugin && plugin->python_plugin->pFuncStart)
            {
                PyObject *res = PyObject_CallNoArgs(plugin->python_plugin->pFuncStart);
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
            // TODO: Implement native plugin start logic
        }
        break;

        default:
            break;
        }
    }
    PyGILState_Release(gstate);
    return 0;
}

int plugin_driver_stop(plugin_driver_t *driver)
{
    printf("[PLUGIN]: Stopping all plugins...\n");
    if (!driver)
    {
        return -1;
    }

    // Signal all plugins to stop
    for (int i = 0; i < driver->plugin_count; i++)
    {
        printf("[PLUGIN]: Stopping plugin %d/%d: %s\n", i + 1, driver->plugin_count,
               driver->plugins[i].config.name);
        if (driver->plugins[i].python_plugin && driver->plugins[i].python_plugin->pFuncStop &&
            driver->plugins[i].running)
        {
            plugin_instance_t *plugin = &driver->plugins[i];

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

            plugin->running = 0;
        }

        printf("[PLUGIN]: Plugin %s stopped...\n", driver->plugins[i].config.name);
        // Plugin manager only handles destruction, not stopping
        // TODO: Implement native plugin stop logic if needed
    }

    return 0;
}

void plugin_driver_destroy(plugin_driver_t *driver)
{
    if (!driver)
    {
        return;
    }

    gstate = PyGILState_Ensure();

    plugin_driver_stop(driver);

    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];
        if (plugin->manager)
        {
            plugin_manager_destroy(plugin->manager);
            plugin->manager = NULL;
        }
        if (plugin->python_plugin)
        {
            python_plugin_cleanup(plugin);
        }
    }

    PyGILState_Release(gstate);
    PyEval_RestoreThread(main_tstate);
    Py_FinalizeEx();
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

    // Initialize mutex functions
    args->mutex_take = plugin_mutex_take;
    args->mutex_give = plugin_mutex_give;
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
    printf("  - init: %s\n", py_binds->pFuncInit ? "✓" : "✗");
    printf("  - start_loop: %s\n", py_binds->pFuncStart ? "✓" : "✗");
    printf("  - stop_loop: %s\n", py_binds->pFuncStop ? "✓" : "✗");
    printf("  - cleanup: %s\n", py_binds->pFuncCleanup ? "✓" : "✗");

    return 0;
}

// Python plugin cycle function
void python_plugin_cycle(plugin_instance_t *plugin)
{
    (void)plugin; // Suppress unused parameter warning
    // In a real implementation, you'd retrieve the python_plugin_t structure
    // and call the cycle function
}

// Cleanup Python plugin
static void python_plugin_cleanup(plugin_instance_t *plugin)
{
    (void)plugin; // Suppress unused parameter warning
    // Cleanup Python resources
    if (plugin && plugin->python_plugin)
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

        free(plugin->python_plugin);
        plugin->python_plugin = NULL;
    }
}
