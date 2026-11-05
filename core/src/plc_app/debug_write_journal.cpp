// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/* Queue of external write/force requests; dispatcher drains at the
 * no-task-running window under image_lock. ext_strucpp_debug_locate
 * routes each: GLOBAL → IECVar; LOCATED → journal_write_* / force. */
#include "debug_write_journal.h"

#include "../lib/strucpp_abi.hpp" /* strucpp::debug::STATUS_OK */
#include "image_tables.h"  /* ext_strucpp_debug_set / _write / _locate */
#include "journal_buffer.h" /* journal_write_* / journal_force_set/clear  */
#include "utils/rt_mutex.h"

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
constexpr size_t   DBGW_MAX_ENTRIES = 128;
constexpr uint16_t DBGW_MAX_BYTES   = 256;
constexpr uint8_t DBGW_STATUS_OK = strucpp::debug::STATUS_OK;

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
std::atomic<bool>   g_direct_window{false};
std::atomic<bool>   g_direct_refusal_logged{false};

__attribute__((constructor)) void dbgw_lock_init_pi(void)
{
    rt_mutex_upgrade_static(&g_dbgw_lock, "g_dbgw_lock");
}

/* LocatedArea (strucpp_abi.hpp): Input=0, Output=1, Memory=2.
 * LocatedSize: Bit=0, Byte=1, Word=2, DWord=3, LWord=4. */
enum { AREA_INPUT = 0, AREA_OUTPUT = 1, AREA_MEMORY = 2 };
enum { SZ_BIT = 0, SZ_BYTE = 1, SZ_WORD = 2, SZ_DWORD = 3, SZ_LWORD = 4 };

/* Map a located (area, size) to its journal buffer type. Mirrors the copy_out
 * mapping in image_tables.cpp exactly (Byte has no per-area memory slot — it
 * always rides the BYTE_* arrays). Returns false for an unmappable pair. */
bool located_journal_type(uint8_t area, uint8_t size, journal_buffer_type_t *out)
{
    const bool is_in = (area == AREA_INPUT);
    const bool is_out = (area == AREA_OUTPUT);
    switch (size) {
    case SZ_BIT:
        *out = is_in ? JOURNAL_BOOL_INPUT : is_out ? JOURNAL_BOOL_OUTPUT
                                                   : JOURNAL_BOOL_MEMORY;
        return true;
    case SZ_BYTE:
        *out = is_in ? JOURNAL_BYTE_INPUT : JOURNAL_BYTE_OUTPUT;
        return true;
    case SZ_WORD:
        *out = is_in ? JOURNAL_INT_INPUT : is_out ? JOURNAL_INT_OUTPUT
                                                  : JOURNAL_INT_MEMORY;
        return true;
    case SZ_DWORD:
        *out = is_in ? JOURNAL_DINT_INPUT : is_out ? JOURNAL_DINT_OUTPUT
                                                   : JOURNAL_DINT_MEMORY;
        return true;
    case SZ_LWORD:
        *out = is_in ? JOURNAL_LINT_INPUT : is_out ? JOURNAL_LINT_OUTPUT
                                                   : JOURNAL_LINT_MEMORY;
        return true;
    default:
        return false;
    }
}

/* Little-endian decode of a debug value payload to a u64, sized by located
 * size. The strucpp debug wire format is native-endian little (same as the
 * image), so a straight LE gather is correct for all integer/bit widths. */
uint64_t decode_value(const uint8_t *b, uint16_t len, uint8_t size)
{
    uint16_t n = (size == SZ_BIT || size == SZ_BYTE) ? 1
               : (size == SZ_WORD)                   ? 2
               : (size == SZ_DWORD)                  ? 4
                                                     : 8;
    if (n > len) n = len;
    uint64_t v = 0;
    for (uint16_t i = 0; i < n && i < 8; ++i) {
        v |= (uint64_t)b[i] << (8 * i);
    }
    return (size == SZ_BIT) ? (v & 1) : v;
}

/* Enqueue a located WRITE into the image journal (applied at the next image
 * drain; transient — program logic / the driver own the slot). Returns the
 * journal's result: 0 when queued. */
int journal_write_located(journal_buffer_type_t type, uint8_t size,
                          uint16_t idx, uint8_t bit, uint64_t v)
{
    switch (size) {
    case SZ_BIT:   return journal_write_bool(type, idx, bit, (v & 1) != 0);
    case SZ_BYTE:  return journal_write_byte(type, idx, (uint8_t)v);
    case SZ_WORD:  return journal_write_int(type, idx, (uint16_t)v);
    case SZ_DWORD: return journal_write_dint(type, idx, (uint32_t)v);
    case SZ_LWORD: return journal_write_lint(type, idx, v);
    default:       return -1;
    }
}

/* Apply one write/force/unforce to a LOCATED variable. Returns false when the
 * slot is unmappable or the op is unknown. */
bool apply_located(uint8_t arr, uint16_t elem, uint8_t op, const uint8_t *bytes,
                   uint16_t len, uint8_t area, uint8_t size, uint16_t byte_index,
                   uint8_t bit_index)
{
    journal_buffer_type_t jt;
    if (!located_journal_type(area, size, &jt)) {
        return false; /* unmappable (e.g. %MB) — copy_out can't emit it either */
    }
    const uint64_t val = decode_value(bytes, len, size);

    switch (op) {
    case DBGW_OP_WRITE:
        return journal_write_located(jt, size, byte_index, bit_index, val) == 0;
    case DBGW_OP_FORCE:
        /* IMAGE FIRST, AND ONLY THEN THE PROGRAM VIEW.
         *
         * These two pins have to agree or the debugger lies. The image slot
         * can refuse -- the address is outside the table, or the forced-slot
         * map was never allocated -- while `ext_strucpp_debug_set` always
         * accepts, because the IECVar exists whatever the image is sized to.
         * Pinning the IECVar first therefore showed the variable as FORCED in
         * the editor and over OPC UA while the image slot took nothing, so the
         * program kept driving it and the displayed value was fiction. A
         * refusal the user is told is a success is worse than the refusal.
         *
         * There is no channel back to the requester from here: the write was
         * enqueued by `runtime_external_write`, which already returned to the
         * caller a cycle ago. Leaving the variable visibly unforced is the
         * only honest signal this path still owns, and it is the one the
         * person who asked for the force is looking at. */
        if (journal_force_set(jt, byte_index, bit_index, val) != 0)
            return false;
        if (ext_strucpp_debug_set)
            ext_strucpp_debug_set(arr, elem, true, bytes, len);
        return true;
    case DBGW_OP_UNFORCE:
        /* Unforce releases both regardless. A refused clear means the slot
         * could not have been forced in the first place, and leaving the
         * IECVar pinned because of it would strand the variable forced with
         * no way to release it. */
        journal_force_clear(jt, byte_index, bit_index);
        if (ext_strucpp_debug_set)
            ext_strucpp_debug_set(arr, elem, false, nullptr, 0);
        return true;
    default:
        return false;
    }
}

/* Apply one write/force/unforce to a GLOBAL / program-internal leaf via the
 * IECVar debug exports (the OPC-UA bug #3 case). Returns false when the .so
 * lacks the export or refuses the value. */
bool apply_global(uint8_t arr, uint16_t elem, uint8_t op, const uint8_t *bytes,
                  uint16_t len)
{
    switch (op) {
    case DBGW_OP_WRITE:
        return ext_strucpp_debug_write &&
               ext_strucpp_debug_write(arr, elem, bytes, len) == DBGW_STATUS_OK;
    case DBGW_OP_FORCE:
        return ext_strucpp_debug_set &&
               ext_strucpp_debug_set(arr, elem, true, bytes, len) == DBGW_STATUS_OK;
    case DBGW_OP_UNFORCE:
        return ext_strucpp_debug_set &&
               ext_strucpp_debug_set(arr, elem, false, nullptr, 0) == DBGW_STATUS_OK;
    default:
        return false;
    }
}

/* Route one write by the leaf's classification, exactly as the drain does. */
bool apply_entry(uint8_t arr, uint16_t elem, uint8_t op, const uint8_t *bytes,
                 uint16_t len)
{
    uint8_t  area = 0, size = 0, bit_index = 0;
    uint16_t byte_index = 0;
    const bool located =
        ext_strucpp_debug_locate &&
        ext_strucpp_debug_locate(arr, elem, &area, &size, &byte_index, &bit_index) != 0;

    if (located) {
        return apply_located(arr, elem, op, bytes, len, area, size, byte_index, bit_index);
    }
    return apply_global(arr, elem, op, bytes, len);
}

} // namespace

extern "C" int runtime_external_write(uint8_t arr, uint16_t elem, uint8_t op,
                                      const uint8_t *bytes, uint16_t len)
{
    pthread_mutex_lock(&g_dbgw_lock);
    size_t n = g_dbgw_count.load(std::memory_order_relaxed);
    if (n >= DBGW_MAX_ENTRIES) {
        pthread_mutex_unlock(&g_dbgw_lock);
        if (!g_overflow_logged) {
            log_warn("[debug-write] queue full (%zu) — external write dropped and not "
                     "retried; further drops this cycle are not logged", DBGW_MAX_ENTRIES);
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
        const DbgwEntry *e = &g_dbgw[i];
        apply_entry(e->arr, e->elem, e->op, e->bytes, e->len);
    }
    g_dbgw_count.store(0, std::memory_order_release);
    g_overflow_logged = false;
    pthread_mutex_unlock(&g_dbgw_lock);
}

extern "C" int runtime_external_write_now(uint8_t arr, uint16_t elem, uint8_t op,
                                          const uint8_t *bytes, uint16_t len)
{
    if (!g_direct_window.load(std::memory_order_acquire)) {
        if (!g_direct_refusal_logged.exchange(true)) {
            log_error("[debug-write] direct write refused: no direct window is open");
        }
        return -1;
    }
    return apply_entry(arr, elem, op, bytes, len) ? 0 : -1;
}

extern "C" void debug_write_journal_set_direct_window(bool open)
{
    g_direct_window.store(open, std::memory_order_release);
}

extern "C" void debug_write_journal_reset(void)
{
    pthread_mutex_lock(&g_dbgw_lock);
    g_dbgw_count.store(0, std::memory_order_release);
    g_overflow_logged = false;
    pthread_mutex_unlock(&g_dbgw_lock);
}
