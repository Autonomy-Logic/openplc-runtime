// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#ifndef __PYTHON_PLUGIN_BRIDGE_H
#define __PYTHON_PLUGIN_BRIDGE_H

#define PY_SSIZE_T_CLEAN

// Suppress _POSIX_C_SOURCE redefinition warning from Python.h on MSYS2/Cygwin
// Python.h defines _POSIX_C_SOURCE to 200809L which conflicts with system headers
#if defined(__GNUC__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wcpp"
#endif

// pyconfig.h defines these unconditionally. A libc header seen earlier may have set other values
// (glibc with POSIX.1-2024: 202405L / 800), and the redefinition is an error under -Werror.
#undef _POSIX_C_SOURCE
#undef _XOPEN_SOURCE

#include <Python.h>

#if defined(__GNUC__)
#pragma GCC diagnostic pop
#endif

// Forward declaration
struct plugin_instance_s;

// Python plugin bridge structure
typedef struct
{
    PyObject *pModule;
    PyObject *pFuncInit; // Driver Init function
    PyObject *pFuncStart;
    PyObject *pFuncStop;
    PyObject *pFuncCleanup;
    /* Optional: set_image_sizes(sizes). NULL when the module does not define
     * it, which declares that it does not understand per-table image sizes
     *. See plugin_driver.h for why presence is the declaration. */
    PyObject *pFuncSetImageSizes;
    PyObject *args_capsule; // Capsule containing plugin_runtime_args_t for lifetime management
} python_binds_t;

#endif // __PYTHON_PLUGIN_BRIDGE_H
