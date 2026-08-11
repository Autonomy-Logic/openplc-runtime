/**
 * @file plc_switch.c
 * @brief Storage for the run/stop mode-switch position.
 *
 * Deliberately tiny: one atomic and three accessors. The runtime never reads
 * hardware and never polls -- a VPP plugin pushes the position whenever it
 * changes, on whatever schedule that package decides (GPIO interrupt, sysfs
 * poll, fieldbus callback, its own thread). The runtime's only use for the
 * value is to refuse a start while the switch reads STOP, and to report the
 * position to the editor.
 *
 * The default is RUN so that a runtime with no switch-aware plugin behaves
 * exactly as it did before this file existed: the boot auto-start is
 * unguarded and every START succeeds.
 */

#include "plc_switch.h"

#include <stdatomic.h>

#include "utils/log.h"

/* Default RUN: no plugin implementing the interface means no gating. */
static atomic_int switch_position = PLC_SWITCH_RUN;

/**
 * Set when the switch moves, cleared when someone acts on it.
 *
 * Exists because state-change requests are dropped while a transition is in
 * flight: a flip during a start or stop is refused, and without a record of it
 * the switch and the PLC end up disagreeing with nobody retrying. Only the fact
 * of movement is kept, never a queue of requests -- `switch_position` above
 * already holds where the switch came to rest, which is the only position that
 * matters once the dust settles.
 *
 * Deliberately platform-agnostic: every VPP that owns a switch reports through
 * plc_set_switch_position(), so no plugin needs to know reconciliation exists.
 */
static atomic_bool switch_moved = false;

void plc_set_switch_position(plc_switch_t position)
{
    const int normalized = (position == PLC_SWITCH_STOP) ? PLC_SWITCH_STOP : PLC_SWITCH_RUN;
    const int previous   = atomic_exchange(&switch_position, normalized);

    /* Log and record edges only. A plugin sampling a GPIO on a fast timer may
     * call this on every sample; logging each one would flood the journal, and
     * treating each one as movement would make the runtime reconcile forever. */
    if (previous != normalized)
    {
        atomic_store(&switch_moved, true);
        log_info("Mode switch moved to %s", normalized == PLC_SWITCH_RUN ? "RUN" : "STOP");
    }
}

bool plc_switch_take_movement(void)
{
    return atomic_exchange(&switch_moved, false);
}

void plc_switch_note_movement(void)
{
    atomic_store(&switch_moved, true);
}

plc_switch_t plc_get_switch_position(void)
{
    return (plc_switch_t)atomic_load(&switch_position);
}

bool plc_switch_allows_run(void)
{
    return atomic_load(&switch_position) == PLC_SWITCH_RUN;
}
