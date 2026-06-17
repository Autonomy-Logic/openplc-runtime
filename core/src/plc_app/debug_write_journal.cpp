/*
 * debug_write_journal.cpp — see debug_write_journal.h.
 *
 * A mutex-protected queue of external write/force requests, drained by the
 * dispatcher at the no-task-running window. Producers (debugger socket
 * thread, OPC-UA plugin thread) only enqueue; the single dispatcher consumer
 * applies them while no IEC worker is mid-scan and while it holds the image
 * lock, so no per-variable locking is needed and the application is race-free.
 *
 * Routing, decided per entry via ext_strucpp_debug_locate:
 *
 *   - GLOBAL / program-internal leaf (or an older .so without the classifier):
 *     applied straight to the IECVar through the strucpp debug exports
 *     (ext_strucpp_debug_write / _set). This is the OPC-UA global-corruption /
 *     bug #3 case — the write that used to race the workers now lands here,
 *     serialized.
 *
 *   - LOCATED variable (%I/%Q/%M): routed through the image journal and the
 *     forced-slot bitmap, because on v4 the image is decoupled from the IECVar
 *     (copy_in/copy_out) and a direct IECVar poke would be clobbered by the
 *     next copy_in. A WRITE becomes a journal_write_* (transient — program
 *     logic / the driver own the slot); a FORCE seeds + pins the slot via
 *     journal_force_set (drop-on-write keeps it pinned all cycle) AND forces
 *     the IECVar so the program's own view is forced too; UNFORCE reverses
 *     both.
 */
#include "debug_write_journal.h"

#include "image_tables.h"  /* ext_strucpp_debug_set / _write / _locate */
#include "journal_buffer.h" /* journal_write_* / journal_force_set/clear  */

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
 * drain; transient — program logic / the driver own the slot). */
void journal_write_located(journal_buffer_type_t type, uint8_t size,
                           uint16_t idx, uint8_t bit, uint64_t v)
{
    switch (size) {
    case SZ_BIT:   journal_write_bool(type, idx, bit, (v & 1) != 0); break;
    case SZ_BYTE:  journal_write_byte(type, idx, (uint8_t)v);        break;
    case SZ_WORD:  journal_write_int(type, idx, (uint16_t)v);        break;
    case SZ_DWORD: journal_write_dint(type, idx, (uint32_t)v);       break;
    case SZ_LWORD: journal_write_lint(type, idx, v);                 break;
    default: break;
    }
}

/* Apply one drained entry to a LOCATED variable. */
void apply_located(const DbgwEntry *e, uint8_t area, uint8_t size,
                   uint16_t byte_index, uint8_t bit_index)
{
    journal_buffer_type_t jt;
    if (!located_journal_type(area, size, &jt)) {
        return; /* unmappable (e.g. %MB) — copy_out can't emit it either */
    }
    const uint64_t val = decode_value(e->bytes, e->len, size);

    switch (e->op) {
    case DBGW_OP_WRITE:
        journal_write_located(jt, size, byte_index, bit_index, val);
        break;
    case DBGW_OP_FORCE:
        /* Program view: pin the IECVar so get() returns the forced value. */
        if (ext_strucpp_debug_set)
            ext_strucpp_debug_set(e->arr, e->elem, true, e->bytes, e->len);
        /* Image view: seed + pin the slot; copy_out and plugin writes drop. */
        journal_force_set(jt, byte_index, bit_index, val);
        break;
    case DBGW_OP_UNFORCE:
        if (ext_strucpp_debug_set)
            ext_strucpp_debug_set(e->arr, e->elem, false, nullptr, 0);
        journal_force_clear(jt, byte_index, bit_index);
        break;
    default:
        break;
    }
}

/* Apply one drained entry to a GLOBAL / program-internal leaf via the IECVar
 * debug exports (the OPC-UA bug #3 case). */
void apply_global(const DbgwEntry *e)
{
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
        const DbgwEntry *e = &g_dbgw[i];

        uint8_t  area = 0, size = 0, bit_index = 0;
        uint16_t byte_index = 0;
        const bool located =
            ext_strucpp_debug_locate &&
            ext_strucpp_debug_locate(e->arr, e->elem, &area, &size,
                                     &byte_index, &bit_index) != 0;

        if (located) {
            apply_located(e, area, size, byte_index, bit_index);
        } else {
            apply_global(e);
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
