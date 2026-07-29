#ifndef PLC_SWITCH_H
#define PLC_SWITCH_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Physical run/stop mode-switch position.
 *
 * Every platform has a mode switch. Devices with no physical switch never call
 * plc_set_switch_position(), so the stored value stays at its RUN default and
 * every gate below is transparent -- identical to the behaviour before this
 * interface existed.
 */
typedef enum
{
    PLC_SWITCH_STOP = 0,
    PLC_SWITCH_RUN  = 1
} plc_switch_t;

/**
 * @brief Store the mode-switch position.
 *
 * Stores only -- it starts nothing and stops nothing. A VPP plugin that owns a
 * physical switch calls this on each change and then asks for the matching
 * transition through request_plc_start / request_plc_stop, so the transition
 * flow stays the same one the editor's START / STOP commands drive.
 *
 * The ordering matters and is part of the plugin contract: store the position
 * BEFORE requesting the transition. On a falling edge that closes the window
 * where a start could slip in; on a rising edge it stops the request being
 * refused by the guard still reading the stale STOP.
 *
 * A plain atomic store. Safe to call from any thread, including while the PLC
 * is stopped (the runtime keeps every plugin mapped across a stop).
 *
 * @param position PLC_SWITCH_STOP or PLC_SWITCH_RUN
 */
void plc_set_switch_position(plc_switch_t position);

/**
 * @brief Read the stored mode-switch position. Serves the SWITCH socket
 *        command and the REST status field.
 */
plc_switch_t plc_get_switch_position(void);

/**
 * @brief Whether a start is permitted right now.
 *
 * Consulted by every start path: the socket START handler, the boot auto-start,
 * and the plugin-facing request_plc_start. False while the switch reads STOP --
 * hardware is authoritative, and a start is refused rather than queued so the
 * editor can tell the user to flip the switch.
 */
bool plc_switch_allows_run(void);

#ifdef __cplusplus
}
#endif

#endif // PLC_SWITCH_H
