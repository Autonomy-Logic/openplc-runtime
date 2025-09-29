#ifndef PLC_STATE_MANAGER_H
#define PLC_STATE_MANAGER_H

#include "plcapp_manager.h"
#include <stdbool.h>

typedef enum
{
    PLC_STATE_INIT,
    PLC_STATE_RUNNING,
    PLC_STATE_STOPPED,
    PLC_STATE_ERROR,
    PLC_STATE_EMPTY
} PLCState;

/**
 * @brief Initialize the PLC state manager and creates the plugin manager.
 * @return int 0 on success, -1 on failure
 */
int plc_state_manager_init(void);

/**
 * @brief Get the current PLC state.
 * @return PLCState The current PLC state
 */
PLCState plc_get_state(void);

/**
 * @brief Set the PLC state. In case of a state change, it will load or unload the PLC program as needed.
 * @param new_state The new PLC state to set
 * @return true if the state was changed, false if it was already in the desired state
 */
bool plc_set_state(PLCState new_state);

/**
 * @brief Cleanup the PLC state manager and unloads the plugin manager.
 * @return void
 */
void plc_state_manager_cleanup(void);

#endif // PLC_STATE_MANAGER_H