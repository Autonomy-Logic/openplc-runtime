// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#ifndef LIFECYCLE_PLUGIN_DETACH_H
#define LIFECYCLE_PLUGIN_DETACH_H

#include <stddef.h>

#include "plugin_types.h"

/** Clears every I/O image table, image lock, debug and journal entry point in an args copy. */
static inline void plugin_args_drop_image(plugin_runtime_args_t *a)
{
    a->bool_input         = NULL;
    a->bool_output        = NULL;
    a->byte_input         = NULL;
    a->byte_output        = NULL;
    a->int_input          = NULL;
    a->int_output         = NULL;
    a->dint_input         = NULL;
    a->dint_output        = NULL;
    a->lint_input         = NULL;
    a->lint_output        = NULL;
    a->int_memory         = NULL;
    a->dint_memory        = NULL;
    a->lint_memory        = NULL;
    a->bool_memory        = NULL;
    a->image_lock         = NULL;
    a->image_unlock       = NULL;
    a->debug_array_count  = NULL;
    a->debug_elem_count   = NULL;
    a->debug_size         = NULL;
    a->debug_read         = NULL;
    a->debug_set          = NULL;
    a->debug_write        = NULL;
    a->journal_write_bool = NULL;
    a->journal_write_byte = NULL;
    a->journal_write_int  = NULL;
    a->journal_write_dint = NULL;
    a->journal_write_lint = NULL;
}

/** Returns 1 when any of the fourteen I/O image tables in a runtime args copy is non-null. */
static inline int plugin_args_hold_image(const plugin_runtime_args_t *a)
{
    return a->bool_input || a->bool_output || a->byte_input || a->byte_output || a->int_input ||
           a->int_output || a->dint_input || a->dint_output || a->lint_input || a->lint_output ||
           a->int_memory || a->dint_memory || a->lint_memory || a->bool_memory;
}

#endif /* LIFECYCLE_PLUGIN_DETACH_H */
