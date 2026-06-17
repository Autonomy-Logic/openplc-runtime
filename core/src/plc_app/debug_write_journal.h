/*
 * debug_write_journal.h — serialized external variable write/force path.
 *
 * The editor debugger and the OPC-UA plugin both need to write or force PLC
 * variables addressed by the strucpp debug (arr, elem) namespace. Doing that
 * straight from their own threads (the unix-socket thread, the OPC-UA asyncio
 * thread) pokes the IECVar concurrently with the IEC task workers — a data
 * race (OpenPLC bug #3) and the mechanism behind the OPC-UA global-corruption
 * bug.
 *
 * This journal makes every external write/force an ENQUEUE from any thread
 * (runtime_external_write), drained exactly once per cycle by the dispatcher
 * at the no-task-running window. Because the dispatcher is the only consumer
 * and it drains while no worker is mid-scan (and, for v4, while it owns the
 * image double-buffer), the application is race-free without per-variable
 * locking. The queue is mutex-protected (low rate — external writes are
 * exceptional), and the drain has a lock-free skip-if-empty fast path so an
 * idle cycle pays nothing.
 *
 * Routing of located variables (through the image journal + forced-slot
 * bitmap) is layered on top of this; globals/program-internal leaves apply
 * straight to the IECVar via the strucpp debug exports.
 */
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

/*
 * Enqueue an external write/force/unforce of the debug leaf (arr, elem).
 * Safe to call from any thread (debugger socket thread, OPC-UA plugin
 * thread). `bytes`/`len` carry the value payload for WRITE/FORCE (ignored
 * for UNFORCE). Returns 0 on success, -1 if the queue is full (dropped +
 * logged). The write is applied at the next dispatcher drain.
 */
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
