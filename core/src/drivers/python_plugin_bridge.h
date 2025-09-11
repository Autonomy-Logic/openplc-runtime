#ifndef __PYTHON_PLUGIN_BRIDGE_H
#define __PYTHON_PLUGIN_BRIDGE_H

#include <Python.h>

// Forward declaration
struct plugin_instance_s;

// Python plugin bridge structure
typedef struct {
    PyObject *pModule;
    PyObject *pFuncInit; // Driver Init function 
    PyObject *pFuncStartLoop;
    PyObject *pFuncStopLoop;
    PyObject *pFuncCycleRun;
    PyObject *pFuncCleanup;
} python_binds_t;

#endif // __PYTHON_PLUGIN_BRIDGE_H