/*
 * debug_write_journal.cpp — see debug_write_journal.h.
 *
 * A mutex-protected queue of external write/force requests, drained by the
 * dispatcher at the no-task-running window. Producers (debugger socket
 * thread, OPC-UA plugin thread) only enqueue; the single dispatcher consumer
 * applies them while no IEC worker is mid-scan, so no per-variable locking is
 * needed and the IECVar poke is race-free.
 *
 * This commit applies every entry through the strucpp debug exports
 * (ext_strucpp_debug_write / _set), which is correct for globals and
 * program-internal leaves — the OPC-UA global-corruption / bug #3 case.
 * Located variables are also written this way for now (transient: clobbered
 * by the next copy_in, matching today's behaviour); the follow-up routes
 * located writes/forces through the image journal + forced-slot bitmap.
 */
#include "debug_write_journal.h"

#include "image_tables.h" /* ext_strucpp_debug_set / ext_strucpp_debug_write */

extern "C" {
#include "utils/log.h"
}

#include <atomic>
#include <cstring>
#include <pthread.h>

namespace {

/* External writes are exceptional (a human at the debugger, or an OPC-UA
 * client issuing a write). A small fixed queue is plenty; payload is sized
 * for the widest debug value (the WSTRING wire width). */
constexpr size_t  DBGW_MAX_ENTRIES = 128;
constexpr uint16_t DBGW_MAX_BYTES  = 256;

struct DbgwEntry {
    uint8_t  arr;
    uint16_t elem;
    uint8_t  op;
    uint16_t len;
    uint8_t  bytes[DBGW_MAX_BYTES];
};

DbgwEntry           g_dbgw[DBGW_MAX_ENTRIES];
std::atomic<size_t> g_dbgw_count{0};
pthread_mutex_t     g_dbgw_lock = PTHREAD_MUTEX_INITIALIZER;
bool                g_overflow_logged = false;

} // namespace

extern "C" int runtime_external_write(uint8_t arr, uint16_t elem, uint8_t op,
                                      const uint8_t *bytes, uint16_t len)
{
    pthread_mutex_lock(&g_dbgw_lock);
    size_t n = g_dbgw_count.load(std::memory_order_relaxed);
    if (n >= DBGW_MAX_ENTRIES) {
        pthread_mutex_unlock(&g_dbgw_lock);
        if (!g_overflow_logged) {
            log_warn("[debug-write] queue full (%zu) — external write dropped; "
                     "applied next cycle once it drains", DBGW_MAX_ENTRIES);
            g_overflow_logged = true;
        }
        return -1;
    }
    DbgwEntry *e = &g_dbgw[n];
    e->arr = arr;
    e->elem = elem;
    e->op = op;
    uint16_t cl = (len > DBGW_MAX_BYTES) ? DBGW_MAX_BYTES : len;
    e->len = cl;
    if (bytes && cl) memcpy(e->bytes, bytes, cl);
    /* release: the entry contents happen-before the count the drainer reads */
    g_dbgw_count.store(n + 1, std::memory_order_release);
    pthread_mutex_unlock(&g_dbgw_lock);
    return 0;
}

extern "C" void debug_write_journal_drain(void)
{
    /* Lock-free fast path: an idle cycle (no external writes) pays nothing. */
    if (g_dbgw_count.load(std::memory_order_acquire) == 0) return;

    pthread_mutex_lock(&g_dbgw_lock);
    size_t n = g_dbgw_count.load(std::memory_order_relaxed);
    for (size_t i = 0; i < n; ++i) {
        DbgwEntry *e = &g_dbgw[i];
        switch (e->op) {
        case DBGW_OP_WRITE:
            if (ext_strucpp_debug_write)
                ext_strucpp_debug_write(e->arr, e->elem, e->bytes, e->len);
            break;
        case DBGW_OP_FORCE:
            if (ext_strucpp_debug_set)
                ext_strucpp_debug_set(e->arr, e->elem, true, e->bytes, e->len);
            break;
        case DBGW_OP_UNFORCE:
            if (ext_strucpp_debug_set)
                ext_strucpp_debug_set(e->arr, e->elem, false, nullptr, 0);
            break;
        default:
            break;
        }
    }
    g_dbgw_count.store(0, std::memory_order_release);
    g_overflow_logged = false;
    pthread_mutex_unlock(&g_dbgw_lock);
}

extern "C" void debug_write_journal_reset(void)
{
    pthread_mutex_lock(&g_dbgw_lock);
    g_dbgw_count.store(0, std::memory_order_release);
    g_overflow_logged = false;
    pthread_mutex_unlock(&g_dbgw_lock);
}
