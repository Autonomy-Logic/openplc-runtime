#ifndef __PYTHON_PLUGIN_BRIDGE_H
#define __PYTHON_PLUGIN_BRIDGE_H

#include <Python.h>

// Python plugin bridge structure
typedef struct {
    PyObject *pModule;
    PyObject *pFuncInit; // Driver Init function 
    PyObject *pFuncStartLoop;
    PyObject *pFuncStopLoop;
    PyObject *pFuncCycleRun;
    PyObject *pFuncCleanup;
} python_plugin_t;

int python_plugin_init(plugin_instance_t *plugin);
void python_plugin_cycle(plugin_instance_t *plugin);
void python_plugin_cleanup(plugin_instance_t *plugin);


#endif // __PYTHON_PLUGIN_BRIDGE_H