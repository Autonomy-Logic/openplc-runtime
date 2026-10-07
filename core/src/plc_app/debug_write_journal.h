// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/* Serialized external write/force path. Writers ENQUEUE from any
 * thread via runtime_external_write; the dispatcher drains at the
 * no-task-running window, so IECVars are never touched concurrently. */
#ifndef DEBUG_WRITE_JOURNAL_H
#define DEBUG_WRITE_JOURNAL_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* External-write operation kinds. */
typedef enum {
    DBGW_OP_WRITE   = 0, /* soft write — set value_ (respects an active force) */
    DBGW_OP_FORCE   = 1, /* force — pin value_ to the payload                  */
    DBGW_OP_UNFORCE = 2  /* unforce — release a pinned variable                */
} debug_write_op_t;

/* Enqueue an external write/force/unforce of debug leaf (arr, elem).
 * Thread-safe. bytes/len are the payload for WRITE/FORCE (ignored for
 * UNFORCE). Returns 0, or -1 if full (dropped + logged). Applied at
 * the next dispatcher drain. */
int runtime_external_write(uint8_t arr, uint16_t elem, uint8_t op,
                           const uint8_t *bytes, uint16_t len);

/*
 * Drain + apply all queued external writes. MUST be called only by the
 * dispatcher at the no-task-running window, with the image lock held (the
 * located path commits through the image). Cheap no-op when the queue is
 * empty.
 */
void debug_write_journal_drain(void);

/* Reset the queue (called on program unload/stop). */
void debug_write_journal_reset(void);

#ifdef __cplusplus
}
#endif

#endif /* DEBUG_WRITE_JOURNAL_H */
