/**
 * @file journal_buffer.c
 * @brief Journal Buffer Implementation for Race-Condition-Free Plugin Writes
 *
 * Producers (plugin threads, EtherCAT bus thread, etc.) enqueue writes; a
 * single consumer (the fastest PLC task, holding the image-tables mutex)
 * drains them into the image at the scan boundary, last-writer-wins by order.
 *
 * Two implementations selected at compile time:
 *
 *   - LOCK-FREE (default on targets with always-lock-free 32-bit + 8-bit
 *     atomics): a double-buffer "flip" MPSC. Producers never block and never
 *     take a lock; the consumer flips the active bank with a single atomic
 *     exchange and drains the retired bank. Overflow within a cycle drops the
 *     excess write(s) and is reported (see journal_apply_and_clear). This is
 *     the path used on x86/x64/arm64/armv7/riscv-with-A.
 *
 *   - MUTEX FALLBACK (when the required atomics are not lock-free, e.g. a
 *     RISC-V core without the 'A' extension, or when JOURNAL_FORCE_MUTEX is
 *     defined): the original mutex-protected ring with an emergency flush.
 *
 * The public API and the per-entry apply semantics are identical for both.
 */

#include "journal_buffer.h"
#include "utils/log.h"
#include "utils/utils.h"
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
#include <sched.h>

/* The lock-free path needs 32-bit (control word, atomic_uint) and 8-bit
 * (per-slot publish flag, atomic_uchar) atomics to be ALWAYS lock-free.
 * ATOMIC_*_LOCK_FREE == 2 means "always lock-free" per the C11 standard. */
#if !defined(JOURNAL_FORCE_MUTEX) && (ATOMIC_INT_LOCK_FREE == 2) && (ATOMIC_CHAR_LOCK_FREE == 2)
#define JOURNAL_LOCKFREE 1
#else
#define JOURNAL_LOCKFREE 0
#endif

/*
 * =============================================================================
 * Shared State
 * =============================================================================
 */

/* Buffer pointers used by apply_entry() to write into the image tables. */
static journal_buffer_ptrs_t g_buffer_ptrs;

/* Forward declaration: per-mode enqueue. Returns 0 on success, -1 on
 * drop/overflow/uninitialised. Fills one entry from the validated args. */
static int journal_add(uint8_t type, uint16_t index, uint8_t bit, uint64_t value);

/*
 * =============================================================================
 * Apply (shared by both implementations)
 * =============================================================================
 */

/* ---------------------------------------------------------------------------
 * Forced-slot bitmap.
 *
 * A located variable that the debugger / OPC-UA has FORCED must keep its
 * forced value in the image regardless of what plugins (or the program's own
 * copy_out) write to that slot. journal_force_set() seeds the slot with the
 * forced value and marks it; every subsequent journal write to a forced slot
 * is then DROPPED at apply time, so the force wins 100% of the cycle — the
 * proper "force locks out external writes" semantic. (For globals/internals
 * forcing lives in the IECVar; this bitmap is the located/image leg.)
 *
 * Mutated only from the dispatcher's debug-write drain and read only from
 * apply_entry() — both under image_lock — so no atomics are required.
 * JBUF_FORCE_SIZE mirrors the image BUFFER_SIZE; a runtime guard keeps this
 * safe even if the two ever diverge.
 * --------------------------------------------------------------------------- */
#define JBUF_FORCE_SIZE 1024
static uint8_t g_forced[JOURNAL_TYPE_COUNT][JBUF_FORCE_SIZE];
static int     g_force_count = 0;

static inline int type_is_bool(uint8_t t)
{
    return t == JOURNAL_BOOL_INPUT || t == JOURNAL_BOOL_OUTPUT ||
           t == JOURNAL_BOOL_MEMORY;
}

static inline int is_slot_forced(uint8_t type, uint16_t idx, uint8_t bit)
{
    if (g_force_count == 0) return 0;          /* fast path: nothing forced */
    if (type >= JOURNAL_TYPE_COUNT || idx >= JBUF_FORCE_SIZE) return 0;
    if (type_is_bool(type)) {
        if (bit >= 8) return 0;
        return (g_forced[type][idx] >> bit) & 1;
    }
    return g_forced[type][idx] != 0;
}

/* Write a value straight into the image slot — NO forced-slot check. Shared
 * by apply_entry (after its drop check) and journal_force_set (the seed). */
static void apply_write_raw(const journal_entry_t *entry)
{
    uint16_t idx = entry->index;

    /* Bounds check */
    if (idx >= (uint16_t)g_buffer_ptrs.buffer_size) {
        return;
    }

    /* bit_index is only meaningful for the three BOOL cases, where it indexes
     * the inner [8] dimension of the bool_* pointer rows. A non-bool write sets
     * the 0xFF sentinel (journal_write_byte/int/dint/lint), and the lock-free
     * path can hand the consumer a torn or stale-recycled slot whose
     * buffer_type reads as BOOL while bit_index carries that sentinel. An
     * unchecked bool_*[idx][0xFF] reads a pointer 247 slots past the row,
     * harvesting a wild pointer that the store below would write through --
     * corrupting unrelated storage (observed: VAR_GLOBALs in the .so). Reject
     * any bool entry whose bit_index is out of range so a torn/stale entry can
     * never escalate into an out-of-bounds pointer write. */
    if ((entry->buffer_type == JOURNAL_BOOL_INPUT ||
         entry->buffer_type == JOURNAL_BOOL_OUTPUT ||
         entry->buffer_type == JOURNAL_BOOL_MEMORY) &&
        entry->bit_index >= 8) {
        return;
    }

    switch ((journal_buffer_type_t)entry->buffer_type) {
        case JOURNAL_BOOL_INPUT: {
            IEC_BOOL *ptr = g_buffer_ptrs.bool_input[idx][entry->bit_index];
            if (ptr != NULL) { *ptr = (IEC_BOOL)(entry->value & 1); }
            break;
        }
        case JOURNAL_BOOL_OUTPUT: {
            IEC_BOOL *ptr = g_buffer_ptrs.bool_output[idx][entry->bit_index];
            if (ptr != NULL) { *ptr = (IEC_BOOL)(entry->value & 1); }
            break;
        }
        case JOURNAL_BOOL_MEMORY: {
            IEC_BOOL *ptr = g_buffer_ptrs.bool_memory[idx][entry->bit_index];
            if (ptr != NULL) { *ptr = (IEC_BOOL)(entry->value & 1); }
            break;
        }
        case JOURNAL_BYTE_INPUT: {
            IEC_BYTE *ptr = g_buffer_ptrs.byte_input[idx];
            if (ptr != NULL) { *ptr = (IEC_BYTE)(entry->value & 0xFF); }
            break;
        }
        case JOURNAL_BYTE_OUTPUT: {
            IEC_BYTE *ptr = g_buffer_ptrs.byte_output[idx];
            if (ptr != NULL) { *ptr = (IEC_BYTE)(entry->value & 0xFF); }
            break;
        }
        case JOURNAL_INT_INPUT: {
            IEC_UINT *ptr = g_buffer_ptrs.int_input[idx];
            if (ptr != NULL) { *ptr = (IEC_UINT)(entry->value & 0xFFFF); }
            break;
        }
        case JOURNAL_INT_OUTPUT: {
            IEC_UINT *ptr = g_buffer_ptrs.int_output[idx];
            if (ptr != NULL) { *ptr = (IEC_UINT)(entry->value & 0xFFFF); }
            break;
        }
        case JOURNAL_INT_MEMORY: {
            IEC_UINT *ptr = g_buffer_ptrs.int_memory[idx];
            if (ptr != NULL) { *ptr = (IEC_UINT)(entry->value & 0xFFFF); }
            break;
        }
        case JOURNAL_DINT_INPUT: {
            IEC_UDINT *ptr = g_buffer_ptrs.dint_input[idx];
            if (ptr != NULL) { *ptr = (IEC_UDINT)(entry->value & 0xFFFFFFFF); }
            break;
        }
        case JOURNAL_DINT_OUTPUT: {
            IEC_UDINT *ptr = g_buffer_ptrs.dint_output[idx];
            if (ptr != NULL) { *ptr = (IEC_UDINT)(entry->value & 0xFFFFFFFF); }
            break;
        }
        case JOURNAL_DINT_MEMORY: {
            IEC_UDINT *ptr = g_buffer_ptrs.dint_memory[idx];
            if (ptr != NULL) { *ptr = (IEC_UDINT)(entry->value & 0xFFFFFFFF); }
            break;
        }
        case JOURNAL_LINT_INPUT: {
            IEC_ULINT *ptr = g_buffer_ptrs.lint_input[idx];
            if (ptr != NULL) { *ptr = (IEC_ULINT)entry->value; }
            break;
        }
        case JOURNAL_LINT_OUTPUT: {
            IEC_ULINT *ptr = g_buffer_ptrs.lint_output[idx];
            if (ptr != NULL) { *ptr = (IEC_ULINT)entry->value; }
            break;
        }
        case JOURNAL_LINT_MEMORY: {
            IEC_ULINT *ptr = g_buffer_ptrs.lint_memory[idx];
            if (ptr != NULL) { *ptr = (IEC_ULINT)entry->value; }
            break;
        }
        default:
            break;
    }
}

/* Apply one drained journal entry, honoring the forced-slot bitmap: a write
 * to a forced slot is dropped so the force owns the slot for the whole cycle.
 * (copy_out's journal writes and plugin journal writes both flow through here,
 * so a forced located output stays pinned no matter who writes it.) */
static void apply_entry(const journal_entry_t *entry)
{
    if (is_slot_forced(entry->buffer_type, entry->index, entry->bit_index)) {
        return;
    }
    apply_write_raw(entry);
}

/* Pin an image slot to `value` and mark it forced. Seeds the slot immediately
 * (bypassing the drop), then every later journal write to it is dropped until
 * journal_force_clear. Called only from the dispatcher's debug-write drain,
 * under image_lock — the same serialization domain as apply_entry. */
void journal_force_set(journal_buffer_type_t type, uint16_t index,
                       uint8_t bit, uint64_t value)
{
    if ((uint8_t)type >= JOURNAL_TYPE_COUNT || index >= JBUF_FORCE_SIZE) {
        return;
    }
    if (type_is_bool((uint8_t)type) && bit >= 8) {
        return;
    }
    uint8_t mask = type_is_bool((uint8_t)type) ? (uint8_t)(1u << bit)
                                               : (uint8_t)0x01;
    if (!(g_forced[type][index] & mask)) {
        g_forced[type][index] |= mask;
        g_force_count++;
    }
    journal_entry_t e;
    e.sequence    = 0;
    e.buffer_type = (uint8_t)type;
    e.bit_index   = type_is_bool((uint8_t)type) ? bit : (uint8_t)0xFF;
    e.index       = index;
    e.value       = value;
    apply_write_raw(&e); /* seed — must land, so it bypasses the drop check */
}

/* Release a forced image slot. The next journal write (program copy_out or a
 * plugin) is no longer dropped, so the slot tracks the live value again. */
void journal_force_clear(journal_buffer_type_t type, uint16_t index, uint8_t bit)
{
    if ((uint8_t)type >= JOURNAL_TYPE_COUNT || index >= JBUF_FORCE_SIZE) {
        return;
    }
    if (type_is_bool((uint8_t)type) && bit >= 8) {
        return;
    }
    uint8_t mask = type_is_bool((uint8_t)type) ? (uint8_t)(1u << bit)
                                               : (uint8_t)0x01;
    if (g_forced[type][index] & mask) {
        g_forced[type][index] &= (uint8_t)~mask;
        if (g_force_count > 0) {
            g_force_count--;
        }
    }
}

#if JOURNAL_LOCKFREE

/*
 * =============================================================================
 * Lock-free double-buffer-flip implementation (MPSC)
 * =============================================================================
 *
 * Control word layout (32-bit):
 *   bit  31    : active bank index (0 or 1)
 *   bits 0..30 : write count claimed in the active bank this cycle
 *
 * Producer: fetch_add(1) atomically claims (bank, slot). It writes the entry
 * (plain stores) then publishes with a release store on the per-slot flag.
 *
 * Consumer (single, under image mutex): one atomic exchange flips the active
 * bank and resets the count; the returned old value gives the retired bank and
 * its final count. The consumer drains [0, count), acquiring each publish flag
 * (a bounded wait covers a producer caught mid-write at the instant of flip).
 *
 * Only the consumer ever changes the bank bit, and the runtime calls the
 * consumer from a single thread, so read-active-then-exchange is race-free.
 */

#define JOURNAL_NBANKS          2
#define JOURNAL_BANK_SHIFT      31u
#define JOURNAL_COUNT_MASK      0x7FFFFFFFu
/* Bounded wait for an in-flight producer's publish at flip time. Each spin is
 * one acquire load; this caps the consumer's wait so a dead/stalled producer
 * can never hang the scan. ~one yield every 64 spins helps on single-core. */
#define JOURNAL_PUBLISH_SPIN_MAX 200000u

typedef struct {
    journal_entry_t entries[JOURNAL_MAX_ENTRIES];
    atomic_uchar    published[JOURNAL_MAX_ENTRIES]; /* 0 = empty, 1 = ready */
} journal_bank_t;

static journal_bank_t g_banks[JOURNAL_NBANKS];
static atomic_uint    g_control;          /* [bank:1][count:31] */
static atomic_bool    g_initialized = false;

int journal_init(const journal_buffer_ptrs_t *buffer_ptrs)
{
    if (buffer_ptrs == NULL) {
        log_error("Journal: buffer_ptrs is NULL");
        return -1;
    }
    if (buffer_ptrs->image_mutex == NULL) {
        log_error("Journal: image_mutex is NULL");
        return -1;
    }

    memcpy(&g_buffer_ptrs, buffer_ptrs, sizeof(journal_buffer_ptrs_t));

    for (int b = 0; b < JOURNAL_NBANKS; b++) {
        memset(g_banks[b].entries, 0, sizeof(g_banks[b].entries));
        for (size_t i = 0; i < JOURNAL_MAX_ENTRIES; i++) {
            atomic_init(&g_banks[b].published[i], 0);
        }
    }
    atomic_store_explicit(&g_control, 0u, memory_order_relaxed);
    atomic_store_explicit(&g_initialized, true, memory_order_release);

    log_info("[JOURNAL] lock-free double-buffer mode (capacity=%d entries/cycle/bank)",
             JOURNAL_MAX_ENTRIES);
    return 0;
}

void journal_cleanup(void)
{
    atomic_store_explicit(&g_initialized, false, memory_order_release);
    atomic_store_explicit(&g_control, 0u, memory_order_relaxed);
    memset(&g_buffer_ptrs, 0, sizeof(g_buffer_ptrs));
}

bool journal_is_initialized(void)
{
    return atomic_load_explicit(&g_initialized, memory_order_acquire);
}

static int journal_add(uint8_t type, uint16_t index, uint8_t bit, uint64_t value)
{
    if (!atomic_load_explicit(&g_initialized, memory_order_acquire)) {
        return -1;
    }

    /* Claim a unique (bank, slot). relaxed: publication ordering is carried by
     * the per-slot release/acquire below, not by the counter itself. */
    uint32_t ctrl = atomic_fetch_add_explicit(&g_control, 1u, memory_order_relaxed);
    uint32_t bank = ctrl >> JOURNAL_BANK_SHIFT;
    uint32_t slot = ctrl & JOURNAL_COUNT_MASK;

    if (slot >= JOURNAL_MAX_ENTRIES) {
        /* Overflow: the active bank is full for this cycle. Drop. The consumer
         * detects and reports the drop count from the raw control count. */
        return -1;
    }

    journal_entry_t *e = &g_banks[bank].entries[slot];
    e->sequence    = slot;
    e->buffer_type = type;
    e->bit_index   = bit;
    e->index       = index;
    e->value       = value;

    /* Publish: release pairs with the consumer's acquire so the full entry is
     * visible before the flag is observed set. */
    atomic_store_explicit(&g_banks[bank].published[slot], 1u, memory_order_release);
    return 0;
}

void journal_apply_and_clear(void)
{
    if (!atomic_load_explicit(&g_initialized, memory_order_acquire)) {
        return;
    }

    /* Fast path: nothing pending -> nothing to apply, so skip the bank flip.
     * The image already reflects every committed write. A producer that adds an
     * entry after this load is simply applied on the next drain (one cycle
     * later) -- the same ordering guarantee a flush-on-lock read offers. This
     * keeps a read-heavy plugin (locking every cycle to read %Q via image_lock)
     * from flipping the journal needlessly and racing producers mid-publish. */
    if ((atomic_load_explicit(&g_control, memory_order_relaxed) & JOURNAL_COUNT_MASK) == 0) {
        return;
    }

    uint32_t cur     = atomic_load_explicit(&g_control, memory_order_relaxed);
    uint32_t active  = cur >> JOURNAL_BANK_SHIFT;
    uint32_t newbank = active ^ 1u;

    /* Flip + reset count in one RMW. Linearizes producers into either the
     * retired bank (counted in `old`) or the fresh bank (count from 0). */
    uint32_t old = atomic_exchange_explicit(&g_control,
                                            newbank << JOURNAL_BANK_SHIFT,
                                            memory_order_acq_rel);
    uint32_t retired = old >> JOURNAL_BANK_SHIFT;   /* == active */
    uint32_t raw     = old & JOURNAL_COUNT_MASK;
    uint32_t count   = raw;

    if (count > JOURNAL_MAX_ENTRIES) {
        log_warn("[JOURNAL] overflow: %u write(s) dropped this cycle "
                 "(capacity=%d) -- increase JOURNAL_MAX_ENTRIES or reduce the "
                 "plugin write rate",
                 raw - JOURNAL_MAX_ENTRIES, JOURNAL_MAX_ENTRIES);
        count = JOURNAL_MAX_ENTRIES;
    }

    journal_bank_t *bank = &g_banks[retired];
    for (uint32_t i = 0; i < count; i++) {
        uint32_t spins = 0;
        while (atomic_load_explicit(&bank->published[i], memory_order_acquire) == 0) {
            if (++spins >= JOURNAL_PUBLISH_SPIN_MAX) {
                break;
            }
            if ((spins & 0x3Fu) == 0) {
                sched_yield();
            }
        }
        if (atomic_load_explicit(&bank->published[i], memory_order_acquire) != 0) {
            apply_entry(&bank->entries[i]);
        } else {
            /* Producer claimed the slot before the flip but never published
             * (died / pathologically delayed). Skip to keep the scan bounded. */
            log_warn("[JOURNAL] slot %u unpublished at flip; skipped", i);
        }
        atomic_store_explicit(&bank->published[i], 0u, memory_order_relaxed);
    }
}

size_t journal_pending_count(void)
{
    uint32_t c = atomic_load_explicit(&g_control, memory_order_relaxed) & JOURNAL_COUNT_MASK;
    return (c > JOURNAL_MAX_ENTRIES) ? JOURNAL_MAX_ENTRIES : c;
}

uint32_t journal_get_sequence(void)
{
    return atomic_load_explicit(&g_control, memory_order_relaxed) & JOURNAL_COUNT_MASK;
}

#else /* !JOURNAL_LOCKFREE -- mutex fallback */

/*
 * =============================================================================
 * Mutex fallback implementation
 * =============================================================================
 */

static journal_entry_t g_entries[JOURNAL_MAX_ENTRIES];
static size_t          g_count = 0;
static uint32_t        g_next_sequence = 0;
static pthread_mutex_t g_journal_mutex;
static bool            g_initialized = false;

static void emergency_flush_locked(void);

int journal_init(const journal_buffer_ptrs_t *buffer_ptrs)
{
    if (buffer_ptrs == NULL) {
        log_error("Journal: buffer_ptrs is NULL");
        return -1;
    }
    if (buffer_ptrs->image_mutex == NULL) {
        log_error("Journal: image_mutex is NULL");
        return -1;
    }
    if (init_rt_mutex(&g_journal_mutex) != 0) {
        fprintf(stderr, "[JOURNAL] Error: failed to initialize mutex\n");
        return -1;
    }

    pthread_mutex_lock(&g_journal_mutex);
    memcpy(&g_buffer_ptrs, buffer_ptrs, sizeof(journal_buffer_ptrs_t));
    g_count = 0;
    g_next_sequence = 0;
    memset(g_entries, 0, sizeof(g_entries));
    g_initialized = true;
    pthread_mutex_unlock(&g_journal_mutex);

    log_info("[JOURNAL] mutex-fallback mode (atomics not lock-free on this target)");
    return 0;
}

void journal_cleanup(void)
{
    pthread_mutex_lock(&g_journal_mutex);
    g_initialized = false;
    g_count = 0;
    g_next_sequence = 0;
    memset(&g_buffer_ptrs, 0, sizeof(g_buffer_ptrs));
    pthread_mutex_unlock(&g_journal_mutex);
    pthread_mutex_destroy(&g_journal_mutex);
}

bool journal_is_initialized(void)
{
    bool result;
    pthread_mutex_lock(&g_journal_mutex);
    result = g_initialized;
    pthread_mutex_unlock(&g_journal_mutex);
    return result;
}

static int journal_add(uint8_t type, uint16_t index, uint8_t bit, uint64_t value)
{
    if (!g_initialized) {
        return -1;
    }
    pthread_mutex_lock(&g_journal_mutex);

    if (g_count >= JOURNAL_MAX_ENTRIES) {
        emergency_flush_locked();
    }
    journal_entry_t *e = &g_entries[g_count];
    e->sequence    = g_next_sequence++;
    e->buffer_type = type;
    e->bit_index   = bit;
    e->index       = index;
    e->value       = value;
    g_count++;

    pthread_mutex_unlock(&g_journal_mutex);
    return 0;
}

void journal_apply_and_clear(void)
{
    if (!g_initialized) {
        return;
    }
    pthread_mutex_lock(&g_journal_mutex);
    for (size_t i = 0; i < g_count; i++) {
        apply_entry(&g_entries[i]);
    }
    g_count = 0;
    g_next_sequence = 0;
    pthread_mutex_unlock(&g_journal_mutex);
}

/* Emergency flush when full. Caller holds g_journal_mutex. Releases it to take
 * the image mutex first (image -> journal order), re-takes journal, applies,
 * releases image, returns with g_journal_mutex held for the pending write. */
static void emergency_flush_locked(void)
{
    pthread_mutex_unlock(&g_journal_mutex);
    pthread_mutex_lock(g_buffer_ptrs.image_mutex);
    pthread_mutex_lock(&g_journal_mutex);
    for (size_t i = 0; i < g_count; i++) {
        apply_entry(&g_entries[i]);
    }
    g_count = 0;
    g_next_sequence = 0;
    pthread_mutex_unlock(g_buffer_ptrs.image_mutex);
}

size_t journal_pending_count(void)
{
    size_t count;
    pthread_mutex_lock(&g_journal_mutex);
    count = g_count;
    pthread_mutex_unlock(&g_journal_mutex);
    return count;
}

uint32_t journal_get_sequence(void)
{
    uint32_t seq;
    pthread_mutex_lock(&g_journal_mutex);
    seq = g_next_sequence;
    pthread_mutex_unlock(&g_journal_mutex);
    return seq;
}

#endif /* JOURNAL_LOCKFREE */

/*
 * =============================================================================
 * Public write functions (shared) -- validate, then enqueue via journal_add()
 * =============================================================================
 */

int journal_write_bool(journal_buffer_type_t type, uint16_t index,
                       uint8_t bit, bool value)
{
    if (type != JOURNAL_BOOL_INPUT &&
        type != JOURNAL_BOOL_OUTPUT &&
        type != JOURNAL_BOOL_MEMORY) {
        return -1;
    }
    if (bit > 7) {
        return -1;
    }
    return journal_add((uint8_t)type, index, bit, value ? 1u : 0u);
}

int journal_write_byte(journal_buffer_type_t type, uint16_t index,
                       uint8_t value)
{
    if (type != JOURNAL_BYTE_INPUT && type != JOURNAL_BYTE_OUTPUT) {
        return -1;
    }
    return journal_add((uint8_t)type, index, 0xFF, value);
}

int journal_write_int(journal_buffer_type_t type, uint16_t index,
                      uint16_t value)
{
    if (type != JOURNAL_INT_INPUT &&
        type != JOURNAL_INT_OUTPUT &&
        type != JOURNAL_INT_MEMORY) {
        return -1;
    }
    return journal_add((uint8_t)type, index, 0xFF, value);
}

int journal_write_dint(journal_buffer_type_t type, uint16_t index,
                       uint32_t value)
{
    if (type != JOURNAL_DINT_INPUT &&
        type != JOURNAL_DINT_OUTPUT &&
        type != JOURNAL_DINT_MEMORY) {
        return -1;
    }
    return journal_add((uint8_t)type, index, 0xFF, value);
}

int journal_write_lint(journal_buffer_type_t type, uint16_t index,
                       uint64_t value)
{
    if (type != JOURNAL_LINT_INPUT &&
        type != JOURNAL_LINT_OUTPUT &&
        type != JOURNAL_LINT_MEMORY) {
        return -1;
    }
    return journal_add((uint8_t)type, index, 0xFF, value);
}
