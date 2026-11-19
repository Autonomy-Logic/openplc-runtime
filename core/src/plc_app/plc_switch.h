// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

#ifndef PLC_SWITCH_H
#define PLC_SWITCH_H

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Physical run/stop mode-switch position.
 *
 * Every platform has a mode switch. A device with no switch-owner plugin never
 * reports a position, so the gate stays at its RUN default and no start path is
 * gated.
 */
typedef enum
{
    PLC_SWITCH_STOP = 0,
    PLC_SWITCH_RUN  = 1
} plc_switch_t;

/**
 * @brief What the start paths may do, from the switch owners' reports.
 *
 * - RUN: no switch owner exists, or every owner has reported since its last
 *   init and the stored position is RUN.
 * - STOP: every owner has reported since its last init and the stored position
 *   is STOP.
 * - UNKNOWN: at least one owner has not reported since its last init. Every
 *   start path fails closed on it.
 */
typedef enum
{
    PLC_SWITCH_GATE_RUN,
    PLC_SWITCH_GATE_STOP,
    PLC_SWITCH_GATE_UNKNOWN
} plc_switch_gate_t;

#define PLC_SWITCH_REPORT_WAIT_MS 2000

#define PLC_SWITCH_OWNER_ID_SIZE 320

#define PLC_SWITCH_MAX_OWNERS 32

/**
 * @brief Record whether plugin @p id declares itself a switch owner.
 *
 * Called by the plugin driver once per load, after the plugin's symbols load and
 * before its init(), with the result of its optional owns_mode_switch. A true
 * declaration registers the owner and marks it as not reported since this init;
 * false changes nothing, so an owner by an earlier report stays one.
 *
 * @param id   plugin identity, "<config name>|<path>"
 * @param owns true when the plugin declared ownership
 */
void plc_switch_owner_declare(const char *id, bool owns);

/**
 * @brief Store the position reported by plugin @p id.
 *
 * Reporting makes the plugin an owner for the rest of the process (implicit
 * ownership) and marks it reported since its last init. Stores only -- it
 * starts nothing and stops nothing. A switch-owner plugin calls its
 * set_switch_position on each change, which lands here attributed to its slot,
 * and then asks for the matching transition through request_plc_start /
 * request_plc_stop.
 *
 * Part of the plugin contract: report the position BEFORE requesting the
 * transition.
 *
 * A change of the stored position logs once and records movement (see
 * plc_switch_take_movement). Re-reporting the stored value after an invalidation
 * is not a change. Safe to call from any thread, including while the PLC is
 * stopped.
 *
 * @param id       plugin identity, "<config name>|<path>"
 * @param position PLC_SWITCH_STOP or PLC_SWITCH_RUN
 */
void plc_switch_owner_report(const char *id, plc_switch_t position);

/**
 * @brief Mark plugin @p id as not reported since its last init.
 *
 * Called when the plugin is de-initialised (cleanup or teardown), not when it
 * detaches from the I/O image. The plugin stays an owner, so the gate reads
 * UNKNOWN until it reports again. Records no movement. A plugin that never
 * declared nor reported is not made an owner.
 */
void plc_switch_owner_invalidate(const char *id);

/**
 * @brief Forget every owner whose identity is not in @p present_ids.
 *
 * Called on each plugin configuration reload with the identities of the
 * configured plugins. (NULL, 0) forgets every owner. The stored position is
 * kept.
 */
void plc_switch_owners_forget_absent(const char *const *present_ids, size_t count);

/** @brief The gate every start path consults; see plc_switch_gate_t. */
plc_switch_gate_t plc_switch_gate(void);

/**
 * @brief Whether at least one switch owner is registered.
 *
 * Counts a declared owner that has not reported, an owner by an earlier report
 * and an owner whose report was invalidated, until plc_switch_owners_forget_absent
 * drops it. True as well when the registry overflowed. Safe from any thread.
 *
 * @return true when an owner is registered or the registry overflowed
 */
bool plc_switch_has_owner(void);

/**
 * @brief Wait until the gate is no longer UNKNOWN, for at most @p timeout_ms.
 *
 * Returns at once when the gate is RUN or STOP, and as soon as the last missing
 * owner reports.
 *
 * @return true when the gate left UNKNOWN, false when the wait ran out
 */
bool plc_switch_await_report(unsigned timeout_ms);

/**
 * @brief Read the stored mode-switch position, whatever the owners' state.
 */
plc_switch_t plc_get_switch_position(void);

/**
 * @brief Whether a start is permitted right now: plc_switch_gate() is RUN.
 *
 * False while the switch reads STOP (a start is refused, not queued) and while
 * an owner has not reported since its last init.
 */
bool plc_switch_allows_run(void);

/**
 * @brief Consume the "switch has moved" record. True if it moved since the last call.
 *
 * State-change requests are dropped while a transition is in flight, so a flip
 * during a start or stop is refused outright. This is how that intent survives:
 * the runtime notes only that the switch moved, and the transition-completion
 * path compares where it came to rest against the state actually reached,
 * correcting a mismatch. Consuming the record is what stops it correcting the
 * same movement twice.
 *
 * Movement-gated on purpose. An editor stop with the switch untouched records no
 * movement, so nothing reverses it; comparing position against state
 * unconditionally would make Stop impossible whenever the switch sits in RUN.
 */
bool plc_switch_take_movement(void);

/**
 * @brief Re-arm the "switch has moved" record after a correction could not start.
 *
 * The counterpart to plc_switch_take_movement(): consuming the record commits the
 * caller to acting on it, and this hands it back when that action was refused --
 * another transition claimed in the gap, or a spawn that failed. Without it the
 * switch's intent is lost silently and the PLC stays in the state the switch
 * disagrees with, which is the exact failure the record exists to prevent.
 *
 * Not for plugins: a plugin reporting a position goes through
 * plc_switch_owner_report(), which records movement on an edge by itself.
 */
void plc_switch_note_movement(void);

#ifdef __cplusplus
}
#endif

#endif // PLC_SWITCH_H
