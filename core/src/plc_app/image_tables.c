#include <dlfcn.h>
#include <stdlib.h>

#include "image_tables.h"
#include "log.h"
#include "utils.h"

// Internal buffers for I/O and memory.
// Booleans
IEC_BOOL *bool_input[BUFFER_SIZE][8];
IEC_BOOL *bool_output[BUFFER_SIZE][8];

// Bytes
IEC_BYTE *byte_input[BUFFER_SIZE];
IEC_BYTE *byte_output[BUFFER_SIZE];

// Analog I/O
IEC_UINT *int_input[BUFFER_SIZE];
IEC_UINT *int_output[BUFFER_SIZE];

// 32bit I/O
IEC_UDINT *dint_input[BUFFER_SIZE];
IEC_UDINT *dint_output[BUFFER_SIZE];

// 64bit I/O
IEC_ULINT *lint_input[BUFFER_SIZE];
IEC_ULINT *lint_output[BUFFER_SIZE];

// Memory
IEC_UINT *int_memory[BUFFER_SIZE];
IEC_UDINT *dint_memory[BUFFER_SIZE];
IEC_ULINT *lint_memory[BUFFER_SIZE];

void (*ext_config_run__)(unsigned long tick);
void (*ext_config_init__)(void);
void (*ext_glueVars)(void);
void (*ext_updateTime)(void);
void (*ext_setBufferPointers)(
    IEC_BOOL *input_bool[BUFFER_SIZE][8], IEC_BOOL *output_bool[BUFFER_SIZE][8],
    IEC_BYTE *input_byte[BUFFER_SIZE], IEC_BYTE *output_byte[BUFFER_SIZE],
    IEC_UINT *input_int[BUFFER_SIZE], IEC_UINT *output_int[BUFFER_SIZE],
    IEC_UDINT *input_dint[BUFFER_SIZE], IEC_UDINT *output_dint[BUFFER_SIZE],
    IEC_ULINT *input_lint[BUFFER_SIZE], IEC_ULINT *output_lint[BUFFER_SIZE],
    IEC_UINT *int_memory[BUFFER_SIZE], IEC_UDINT *dint_memory[BUFFER_SIZE],
    IEC_ULINT *lint_memory[BUFFER_SIZE]);


// Debug
void (*ext_set_endianness)(uint8_t value);
uint16_t (*ext_get_var_count)(void);
size_t (*ext_get_var_size)(size_t idx);
void *(*ext_get_var_addr)(size_t idx);
void (*ext_set_trace)(size_t idx, bool forced, void *val);


int symbols_init(PluginManager *pm) 
{
    // Get pointer to external functions
    *(void **)(&ext_config_run__) =
        plugin_manager_get_func(pm, void (*)(unsigned long), "config_run__");

    *(void **)(&ext_config_init__) =
        plugin_manager_get_func(pm, void (*)(unsigned long), "config_init__");

    *(void **)(&ext_glueVars) =
        plugin_manager_get_func(pm, void (*)(unsigned long), "glueVars");

    *(void **)(&ext_updateTime) =
        plugin_manager_get_func(pm, void (*)(unsigned long), "updateTime");

    *(void **)(&ext_setBufferPointers) =
        plugin_manager_get_func(pm, void (*)(unsigned long), "setBufferPointers");

    *(void **)(&ext_common_ticktime__) =
        plugin_manager_get_func(pm, void (*)(unsigned long), "common_ticktime__");

    *(void **)(&ext_plc_program_md5) =
        plugin_manager_get_func(pm, char *(*)(unsigned long), "plc_program_md5");

    *(void **)(&ext_set_endianness) =
        plugin_manager_get_func(pm, void (*)(unsigned long), "set_endianness");

    *(void **)(&ext_get_var_count) =
        plugin_manager_get_func(pm, uint16_t (*)(uint16_t), "get_var_count");

    *(void **)(&ext_get_var_size) =
        plugin_manager_get_func(pm, size_t (*)(size_t), "get_var_size");

    *(void **)(&ext_get_var_addr) =
        plugin_manager_get_func(pm, void *(*)(unsigned long), "get_var_addr");

    *(void **)(&ext_set_trace) =
        plugin_manager_get_func(pm, void (*)(unsigned long), "set_trace");

    // Check if all symbols were loaded successfully
    if (!ext_config_run__ || !ext_config_init__ || !ext_glueVars ||
        !ext_updateTime || !ext_setBufferPointers || !ext_common_ticktime__ ||
        !ext_plc_program_md5 || !ext_set_endianness || !ext_get_var_count || 
        !ext_get_var_size || !ext_get_var_addr || !ext_set_trace) 
    {
        log_error("Failed to load all symbols");
        return -1;
    }

    // Send buffer pointers to .so
    ext_setBufferPointers(bool_input, bool_output, byte_input, byte_output,
                            int_input, int_output, dint_input, dint_output,
                            lint_input, lint_output, int_memory, dint_memory,
                            lint_memory);

    return 0;
}
