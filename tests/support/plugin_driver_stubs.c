#include "plugin_config.h"
#include "plugin_driver.h"

// Mock implementations for external buffer variables
// These are normally defined in image_tables.c
IEC_BOOL *bool_input[BUFFER_SIZE][8];
IEC_BOOL *bool_output[BUFFER_SIZE][8];
IEC_BYTE *byte_input[BUFFER_SIZE];
IEC_BYTE *byte_output[BUFFER_SIZE];
IEC_UINT *int_input[BUFFER_SIZE];
IEC_UINT *int_output[BUFFER_SIZE];
IEC_UDINT *dint_input[BUFFER_SIZE];
IEC_UDINT *dint_output[BUFFER_SIZE];
IEC_ULINT *lint_input[BUFFER_SIZE];
IEC_ULINT *lint_output[BUFFER_SIZE];
IEC_UINT *int_memory[BUFFER_SIZE];
IEC_UDINT *dint_memory[BUFFER_SIZE];
IEC_ULINT *lint_memory[BUFFER_SIZE];

// Mock implementation for plugin_manager_destroy
// This is normally defined in plcapp_manager.c
void plugin_manager_destroy(PluginManager *manager)
{
    (void)manager; // Suppress unused parameter warning
    // Mock implementation - do nothing
}