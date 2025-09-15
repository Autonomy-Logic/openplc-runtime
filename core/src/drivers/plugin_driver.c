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

// Plugin thread function
void *plugin_thread_function(void *arg)
{
    plugin_instance_t *plugin = (plugin_instance_t *)arg;

    PyObject *res = PyObject_CallFunctionObjArgs(plugin->python_plugin->pFuncStartLoop, NULL);
    if (!res)
    {
        PyErr_Print();
        fprintf(stderr, "Python start loop function failed for plugin: %s\n", plugin->config.name);
        // return -1;
    }
    else
    {
        printf("[PLUGIN]: Plugin %s started successfully.\n", plugin->config.name);
    }
    Py_DECREF(res);

    plugin->running = 1;

    // Main plugin loop
    while (plugin->running)
    {
        // Here plugins can do their work
        // They can call the buffer access functions
        usleep(10000); // 10ms sleep to prevent busy waiting
    }

    return NULL;
}

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
static int plugin_mutex_take(pthread_mutex_t *mutex)
{
    return pthread_mutex_lock(mutex);
}

static int plugin_mutex_give(pthread_mutex_t *mutex)
{
    return pthread_mutex_unlock(mutex);
}

// Python capsule destructor for runtime args
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
            PyObject *args = generate_structured_args_with_driver(PLUGIN_TYPE_PYTHON, driver);
            if (!args)
            {
                fprintf(stderr, "Failed to generate runtime args for plugin: %s\n",
                        plugin->config.name);
                return -1;
            }
            // Call the Python init function with proper capsule
            PyObject *result =
                PyObject_CallFunctionObjArgs(plugin->python_plugin->pFuncInit, args, NULL);
            Py_DECREF(args);

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

    for (int i = 0; i < driver->plugin_count; i++)
    {
        plugin_instance_t *plugin = &driver->plugins[i];
        switch (plugin->config.type)
        {
        case PLUGIN_TYPE_PYTHON:
        {
            // Python plugins run asynchronously in their own threads
            if (plugin->python_plugin && plugin->python_plugin->pFuncStartLoop)
            {
                // Create a thread to run the plugin thread function
                if (pthread_create(&plugin->thread, NULL, plugin_thread_function, plugin) != 0)
                {
                    fprintf(stderr, "Failed to create thread for plugin: %s\n",
                            plugin->config.name);
                    return -1;
                }
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
        driver->plugins[i].running = 0;
    }

    // Wait for all threads to finish
    for (int i = 0; i < driver->plugin_count; i++)
    {
        if (driver->plugins[i].thread)
        {
            printf("[PLUGIN]: Plugin %s thread canceling.\n", driver->plugins[i].config.name);
            pthread_cancel(driver->plugins[i].thread);
            driver->plugins[i].thread = 0;
            printf("[PLUGIN]: Plugin %s thread canceled.\n", driver->plugins[i].config.name);
            // pthread_join(driver->plugins[i].thread, NULL);
        }
        if (driver->plugins[i].manager)
        {
            plugin_manager_destroy(driver->plugins[i].manager);
            driver->plugins[i].manager = NULL;
        }
    }

    return 0;
}

void plugin_driver_destroy(plugin_driver_t *driver)
{
    if (!driver)
    {
        return;
    }

    plugin_driver_stop(driver);
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
void *generate_structured_args_with_driver(plugin_type_t type, plugin_driver_t *driver)
{
    plugin_runtime_args_t *args = malloc(sizeof(plugin_runtime_args_t));
    if (!args)
    {
        return NULL;
    }

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
    args->buffer_mutex = driver ? &driver->buffer_mutex : NULL;

    // Initialize buffer size info
    args->buffer_size     = BUFFER_SIZE;
    args->bits_per_buffer = 8;

    switch (type)
    {
    case PLUGIN_TYPE_NATIVE:
        // For native plugins, return the structure directly
        return args;

    case PLUGIN_TYPE_PYTHON:
        // For Python plugins, wrap in a PyCapsule
        return create_python_runtime_args_capsule(args);

    default:
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
    if (!plugin || !plugin->config.path)
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

    PyRun_SimpleString("import sys");
    PyRun_SimpleString(python_path_cmd);

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

    py_binds->pFuncStartLoop = PyObject_GetAttrString(py_binds->pModule, "start_loop");
    if (!py_binds->pFuncStartLoop || !PyCallable_Check(py_binds->pFuncStartLoop))
    {
        // start_loop is optional
        Py_XDECREF(py_binds->pFuncStartLoop);
        py_binds->pFuncStartLoop = NULL;
    }

    py_binds->pFuncStopLoop = PyObject_GetAttrString(py_binds->pModule, "stop_loop");
    if (!py_binds->pFuncStopLoop || !PyCallable_Check(py_binds->pFuncStopLoop))
    {
        // stop_loop is optional
        Py_XDECREF(py_binds->pFuncStopLoop);
        py_binds->pFuncStopLoop = NULL;
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
    printf("  - start_loop: %s\n", py_binds->pFuncStartLoop ? "✓" : "✗");
    printf("  - stop_loop: %s\n", py_binds->pFuncStopLoop ? "✓" : "✗");
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
void python_plugin_cleanup(plugin_instance_t *plugin)
{
    (void)plugin; // Suppress unused parameter warning
    // Cleanup Python resources
    if (plugin && plugin->python_plugin)
    {
        // Clean up Python objects
        Py_XDECREF(plugin->python_plugin->pFuncInit);
        Py_XDECREF(plugin->python_plugin->pFuncStartLoop);
        Py_XDECREF(plugin->python_plugin->pFuncStopLoop);
        Py_XDECREF(plugin->python_plugin->pFuncCleanup);
        Py_XDECREF(plugin->python_plugin->pModule);

        free(plugin->python_plugin);
        plugin->python_plugin = NULL;
    }
}
