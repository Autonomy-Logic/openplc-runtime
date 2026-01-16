#include "plugin_utils.h"
#include "../plc_app/image_tables.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Wrapper function to get list of variable addresses
// Returns NULL for all addresses if no PLC program is loaded
void get_var_list(size_t num_vars, size_t *indexes, void **result)
{
    // Validate input parameters
    if (!indexes || !result || num_vars == 0)
    {
        return;
    }

    // Check if PLC program is loaded (function pointers are set)
    if (!ext_get_var_count || !ext_get_var_addr)
    {
        for (size_t i = 0; i < num_vars; i++)
        {
            result[i] = NULL;
        }
        return;
    }

    for (size_t i = 0; i < num_vars; i++)
    {
        size_t idx = indexes[i];
        if (idx >= ext_get_var_count())
        {
            result[i] = NULL;
        }
        else
        {
            result[i] = ext_get_var_addr(idx);
        }
    }
}

// Returns 0 if no PLC program is loaded
size_t get_var_size(size_t idx)
{
    if (!ext_get_var_size)
    {
        return 0;
    }
    return ext_get_var_size(idx);
}

// Returns 0 if no PLC program is loaded
uint16_t get_var_count(void)
{
    if (!ext_get_var_count)
    {
        return 0;
    }
    return ext_get_var_count();
}