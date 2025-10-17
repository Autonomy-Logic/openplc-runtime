#ifndef IMAGE_TABLES_H
#define IMAGE_TABLES_H

#include "../lib/iec_types.h"
#include "plcapp_manager.h"

#define BUFFER_SIZE 1024
#define libplc_file "./build/libplc.so"

// Internal buffers for I/O and memory.
// Booleans
extern IEC_BOOL *bool_input[BUFFER_SIZE][8];
extern IEC_BOOL *bool_output[BUFFER_SIZE][8];

// Bytes
extern IEC_BYTE *byte_input[BUFFER_SIZE];
extern IEC_BYTE *byte_output[BUFFER_SIZE];

// Analog I/O
extern IEC_UINT *int_input[BUFFER_SIZE];
extern IEC_UINT *int_output[BUFFER_SIZE];

// 32bit I/O
extern IEC_UDINT *dint_input[BUFFER_SIZE];
extern IEC_UDINT *dint_output[BUFFER_SIZE];

// 64bit I/O
extern IEC_ULINT *lint_input[BUFFER_SIZE];
extern IEC_ULINT *lint_output[BUFFER_SIZE];

// Memory
extern IEC_UINT *int_memory[BUFFER_SIZE];
extern IEC_UDINT *dint_memory[BUFFER_SIZE];
extern IEC_ULINT *lint_memory[BUFFER_SIZE];

/**
 * @brief Set the buffer pointers for the plugin manager
 *
 * @param[in]  IEC  The IEC data types
 */
extern void (*ext_setBufferPointers)(
    IEC_BOOL *input_bool[BUFFER_SIZE][8], IEC_BOOL *output_bool[BUFFER_SIZE][8],
    IEC_BYTE *input_byte[BUFFER_SIZE], IEC_BYTE *output_byte[BUFFER_SIZE],
    IEC_UINT *input_int[BUFFER_SIZE], IEC_UINT *output_int[BUFFER_SIZE],
    IEC_UDINT *input_dint[BUFFER_SIZE], IEC_UDINT *output_dint[BUFFER_SIZE],
    IEC_ULINT *input_lint[BUFFER_SIZE], IEC_ULINT *output_lint[BUFFER_SIZE],
    IEC_UINT *int_memory[BUFFER_SIZE], IEC_UDINT *dint_memory[BUFFER_SIZE],
    IEC_ULINT *lint_memory[BUFFER_SIZE]);

/**
 * @brief Common ticktime variable from the PLC program
 *
 * @param[in]  tick  The current tick value
 */
extern void (*ext_config_run__)(unsigned long tick);

/**
 * @brief Initialize the configuration
 */
extern void (*ext_config_init__)(void);

/**
 * @brief Glue variables together
 */
extern void (*ext_glueVars)(void);
extern void (*ext_updateTime)(void);

/**
 * @brief Debug functions
 */
extern void (*ext_set_endianness)(uint8_t value);
extern uint16_t (*ext_get_var_count)(void);
extern size_t (*ext_get_var_size)(size_t idx);
extern void *(*ext_get_var_addr)(size_t idx);
extern void (*ext_set_trace)(size_t idx, bool forced, void *val);

/**
 * @brief Initialize symbols for the plugin manager
 *
 * @param[in]  pm  The plugin manager to initialize symbols for
 * @return 0 on success, -1 on failure
 */
int symbols_init(PluginManager *pm);

#endif // IMAGE_TABLES_H
