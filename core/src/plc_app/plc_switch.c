// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file plc_switch.c
 * @brief The run/stop mode-switch position, its owners and the start gate.
 *
 * The runtime never reads hardware and never polls -- a plugin that owns a
 * switch pushes the position whenever it changes, on whatever schedule that
 * package decides (GPIO interrupt, sysfs poll, fieldbus callback, its own
 * thread). Each report is attributed to the plugin that made it, so the gate
 * knows whether every owner has reported since its last init.
 *
 * With no owner the gate is RUN, so a runtime with no switch-owner plugin has
 * no start path gated.
 */

#include "plc_switch.h"

#include <pthread.h>
#include <stdatomic.h>
#include <string.h>

#include "utils/log.h"
#include "utils/utils.h"

/** One remembered switch owner. */
typedef struct
{
    char id[PLC_SWITCH_OWNER_ID_SIZE];
    bool reported;
} switch_owner_t;

static switch_owner_t owners[PLC_SWITCH_MAX_OWNERS];
static size_t owner_count            = 0;
static bool owner_overflow           = false;
static pthread_mutex_t registry_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t registry_changed;
static pthread_once_t registry_once = PTHREAD_ONCE_INIT;

static atomic_int switch_position = PLC_SWITCH_RUN;

static atomic_bool switch_moved = false;

/** Creates registry_changed on CLOCK_MONOTONIC. */
static void registry_init(void)
{
    monotonic_cond_init(&registry_changed);
}

/** Locks the registry, creating its condition variable on first use. */
static void registry_enter(void)
{
    pthread_once(&registry_once, registry_init);
    pthread_mutex_lock(&registry_lock);
}

/** Wakes every waiter and unlocks the registry. */
static void registry_leave_changed(void)
{
    pthread_cond_broadcast(&registry_changed);
    pthread_mutex_unlock(&registry_lock);
}

/** The owner registered as @p id, or NULL. Caller holds registry_lock. */
static switch_owner_t *find_owner(const char *id)
{
    for (size_t i = 0; i < owner_count; i++)
    {
        if (strcmp(owners[i].id, id) == 0)
            return &owners[i];
    }
    return NULL;
}

/** The owner registered as @p id, registered unreported when new. Caller holds registry_lock. */
static switch_owner_t *find_or_add_owner(const char *id)
{
    switch_owner_t *owner = find_owner(id);
    if (owner)
        return owner;

    if (owner_count >= PLC_SWITCH_MAX_OWNERS)
    {
        if (!owner_overflow)
            log_error("Mode switch: more than %d switch owners; every start is refused until the "
                      "plugin configuration changes",
                      PLC_SWITCH_MAX_OWNERS);
        owner_overflow = true;
        return NULL;
    }

    owner = &owners[owner_count++];
    strncpy(owner->id, id, sizeof(owner->id) - 1);
    owner->id[sizeof(owner->id) - 1] = '\0';
    owner->reported                  = false;
    return owner;
}

/** The gate from the registry and the stored position. Caller holds registry_lock. */
static plc_switch_gate_t gate_locked(void)
{
    if (owner_overflow)
        return PLC_SWITCH_GATE_UNKNOWN;
    if (owner_count == 0)
        return PLC_SWITCH_GATE_RUN;
    for (size_t i = 0; i < owner_count; i++)
    {
        if (!owners[i].reported)
            return PLC_SWITCH_GATE_UNKNOWN;
    }
    return atomic_load(&switch_position) == PLC_SWITCH_RUN ? PLC_SWITCH_GATE_RUN
                                                           : PLC_SWITCH_GATE_STOP;
}

/** Whether @p id is one of the @p count identities in @p ids. */
static bool id_present(const char *id, const char *const *ids, size_t count)
{
    for (size_t i = 0; ids && i < count; i++)
    {
        if (ids[i] && strcmp(ids[i], id) == 0)
            return true;
    }
    return false;
}

void plc_switch_owner_declare(const char *id, bool owns)
{
    if (!id || !owns)
        return;

    registry_enter();
    switch_owner_t *owner = find_or_add_owner(id);
    if (owner)
        owner->reported = false;
    registry_leave_changed();
}

void plc_switch_owner_report(const char *id, plc_switch_t position)
{
    if (!id)
        return;

    const int normalized = (position == PLC_SWITCH_STOP) ? PLC_SWITCH_STOP : PLC_SWITCH_RUN;

    registry_enter();
    switch_owner_t *owner = find_or_add_owner(id);
    if (owner)
        owner->reported = true;
    const int previous = atomic_exchange(&switch_position, normalized);
    registry_leave_changed();

    if (previous != normalized)
    {
        atomic_store(&switch_moved, true);
        log_info("Mode switch moved to %s", normalized == PLC_SWITCH_RUN ? "RUN" : "STOP");
    }
}

void plc_switch_owner_invalidate(const char *id)
{
    if (!id)
        return;

    registry_enter();
    switch_owner_t *owner = find_owner(id);
    if (owner)
        owner->reported = false;
    registry_leave_changed();
}

void plc_switch_owners_forget_absent(const char *const *present_ids, size_t count)
{
    registry_enter();
    size_t kept = 0;
    for (size_t i = 0; i < owner_count; i++)
    {
        if (id_present(owners[i].id, present_ids, count))
        {
            if (kept != i)
                owners[kept] = owners[i];
            kept++;
        }
        else
        {
            log_info("Mode switch: %s left the plugin configuration and no longer owns the "
                     "switch",
                     owners[i].id);
        }
    }
    owner_count    = kept;
    owner_overflow = false;
    registry_leave_changed();
}

plc_switch_gate_t plc_switch_gate(void)
{
    registry_enter();
    const plc_switch_gate_t gate = gate_locked();
    pthread_mutex_unlock(&registry_lock);
    return gate;
}

bool plc_switch_has_owner(void)
{
    registry_enter();
    const bool has_owner = owner_count > 0 || owner_overflow;
    pthread_mutex_unlock(&registry_lock);
    return has_owner;
}

bool plc_switch_await_report(unsigned timeout_ms)
{
    const struct timespec deadline = monotonic_deadline_ms(timeout_ms);

    registry_enter();
    int rc = 0;
    while (gate_locked() == PLC_SWITCH_GATE_UNKNOWN && rc == 0)
        rc = pthread_cond_timedwait(&registry_changed, &registry_lock, &deadline);
    const bool known = gate_locked() != PLC_SWITCH_GATE_UNKNOWN;
    pthread_mutex_unlock(&registry_lock);
    return known;
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
    return plc_switch_gate() == PLC_SWITCH_GATE_RUN;
}
