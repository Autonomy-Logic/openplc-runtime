/* Out-of-range forces, COUNTED rather than logged.
 *
 * Saying it out loud is right -- a silent drop is the defect this change set
 * out to remove -- but not from here. Both force paths run under image_lock,
 * on plc_cycle_thread, which has called set_realtime_priority(). log_warn
 * takes log_mutex, a plain mutex with no priority inheritance (unlike the
 * image mutex, which is built through init_recursive_pi_mutex precisely
 * because it needs it) and then does a blocking socket write. So a SCHED_FIFO
 * dispatcher could block behind a low-priority logging thread WHILE HOLDING
 * the image lock, stalling every plugin thread waiting on it.
 *
 * It is unrate-limited too: one line per offending entry, up to
 * DBGW_MAX_ENTRIES per drain, and an OPC-UA client repeatedly writing one bad
 * address reproduces it every tick.
 *
 * So the count is incremented here and reported from off the real-time path
 * by journal_take_force_drops(), which the dispatcher can read between
 * cycles. The information survives; the stall does not. */
static unsigned g_force_oob_drops = 0;

unsigned journal_take_force_drops(void)
{
    const unsigned n  = g_force_oob_drops;
    g_force_oob_drops = 0;
    return n;
}

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
#include "image_tables.h"
#include "utils/log.h"
#include "utils/utils.h"
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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
 *
 * SIZED FROM THE IMAGE, not from a constant of its own (RTOP-284). This was a
 * fixed 1024 per journal type -- a third hardcoded 1024, alongside the image's
 * and the Modbus slave plugin's -- and the three guards below bounded against
 * it and returned quietly. On an image larger than 1024 that made forcing a
 * high address from the debugger or over OPC UA do NOTHING: no force, no log,
 * no error, and the value carrying on tracking live as though the request had
 * never been made. The image can be any size now, so this follows it: one row
 * per journal type, each as long as the image.
 * --------------------------------------------------------------------------- */
/* JOURNAL TYPES AND IMAGE TABLE IDS ARE NOT THE SAME ORDER, despite both
 * having fourteen members and journal_buffer.h saying this enum "matches the
 * OpenPLC image table types". It matches the CONCEPTS, not the indices:
 * journal puts each width's memory table beside its input and output
 * (..._INPUT, ..._OUTPUT, ..._MEMORY) while image_tables.h groups all the
 * memory tables at the end. JOURNAL_INT_MEMORY is 7; IMAGE_TABLE_INT_MEMORY
 * is 10.
 *
 * So a cast between them is silent corruption: writes land in another table's
 * bounds and the wrong area is refused or admitted. The mapping is written
 * out, once, here. */
static const image_table_id_t kJournalToImageTable[JOURNAL_TYPE_COUNT] = {
    [JOURNAL_BOOL_INPUT]  = IMAGE_TABLE_BOOL_INPUT,
    [JOURNAL_BOOL_OUTPUT] = IMAGE_TABLE_BOOL_OUTPUT,
    [JOURNAL_BOOL_MEMORY] = IMAGE_TABLE_BOOL_MEMORY,
    [JOURNAL_BYTE_INPUT]  = IMAGE_TABLE_BYTE_INPUT,
    [JOURNAL_BYTE_OUTPUT] = IMAGE_TABLE_BYTE_OUTPUT,
    [JOURNAL_INT_INPUT]   = IMAGE_TABLE_INT_INPUT,
    [JOURNAL_INT_OUTPUT]  = IMAGE_TABLE_INT_OUTPUT,
    [JOURNAL_INT_MEMORY]  = IMAGE_TABLE_INT_MEMORY,
    [JOURNAL_DINT_INPUT]  = IMAGE_TABLE_DINT_INPUT,
    [JOURNAL_DINT_OUTPUT] = IMAGE_TABLE_DINT_OUTPUT,
    [JOURNAL_DINT_MEMORY] = IMAGE_TABLE_DINT_MEMORY,
    [JOURNAL_LINT_INPUT]  = IMAGE_TABLE_LINT_INPUT,
    [JOURNAL_LINT_OUTPUT] = IMAGE_TABLE_LINT_OUTPUT,
    [JOURNAL_LINT_MEMORY] = IMAGE_TABLE_LINT_MEMORY,
};

/** The longest table, which is how long a forced-slot row has to be: rows are
 *  one length for all fourteen types, so the longest is the only one that can
 *  record a forced slot anywhere any table reaches. Under-allocating here is
 *  what silently stopped a high address being forced at all. */
static uint32_t journal_longest_table(void)
{
    uint32_t longest = 0;
    for (int t = 0; t < JOURNAL_TYPE_COUNT; ++t)
    {
        const uint32_t n = image_table_capacity(kJournalToImageTable[t]);
        if (n > longest)
            longest = n;
    }
    return longest;
}

/** How far this journal type's table actually reaches. */
static uint32_t journal_type_capacity(uint8_t type)
{
    if (type >= JOURNAL_TYPE_COUNT)
        return 0;
    return image_table_capacity(kJournalToImageTable[type]);
}

static uint8_t *g_forced[JOURNAL_TYPE_COUNT];
/* uint32_t, not uint16_t: the image is allowed up to 65536 elements, which does
 * not fit a uint16_t and would wrap to zero -- turning "the largest legal
 * image" into "forcing is disabled everywhere". The indices compared against it
 * are uint16_t and promote cleanly. */
static uint32_t g_force_size = 0; /* rows are this long; 0 = not allocated */
static int g_force_count     = 0;

/* Allocate the forced-slot bitmap to match the image. All or nothing: a
 * partially allocated bitmap would leave some journal types unforceable with
 * no way to tell which, which is the silent failure this change removes. */
static void force_map_free(void);

static int force_map_alloc(uint32_t elements)
{
    /* Release first. Assigning over g_forced[t] unconditionally leaked all
     * fourteen rows on any journal_init not preceded by a journal_cleanup,
     * which is a shape the state machine does not currently produce but does
     * not forbid either. */
    force_map_free();

    for (int t = 0; t < JOURNAL_TYPE_COUNT; t++)
    {
        g_forced[t] = (uint8_t *)calloc(elements ? elements : 1, sizeof(uint8_t));
        if (g_forced[t] == NULL)
        {
            for (int u = 0; u < JOURNAL_TYPE_COUNT; u++)
            {
                free(g_forced[u]);
                g_forced[u] = NULL;
            }
            g_force_size = 0;
            return -1;
        }
    }
    g_force_size  = elements;
    g_force_count = 0;
    return 0;
}

static void force_map_free(void)
{
    /* THE GUARDS GO DOWN FIRST, and the order is the whole point.
     *
     * is_slot_forced() checks g_force_count and g_force_size and only then
     * indexes g_forced[type][idx]. Freeing the rows before clearing those two
     * leaves a window in which a reader passes both checks and dereferences a
     * row that is already NULL.
     *
     * The window is reachable rather than theoretical: image_lock is handed to
     * every plugin as args->image_lock and calls journal_apply_and_clear(), so
     * is_slot_forced runs on plugin-owned threads -- EtherCAT's bus thread and
     * the s7comm server callback among them -- and this function takes no
     * lock. Clearing first means a reader that sees either guard down never
     * indexes a row at all.
     *
     * g_force_count first of all, because it is the fast-path check and the
     * only one a reader with nothing forced ever reaches. */
    g_force_count = 0;
    g_force_size  = 0;

    for (int t = 0; t < JOURNAL_TYPE_COUNT; t++)
    {
        free(g_forced[t]);
        g_forced[t] = NULL;
    }
}

static inline int type_is_bool(uint8_t t)
{
    return t == JOURNAL_BOOL_INPUT || t == JOURNAL_BOOL_OUTPUT || t == JOURNAL_BOOL_MEMORY;
}

static inline int is_slot_forced(uint8_t type, uint16_t idx, uint8_t bit)
{
    if (g_force_count == 0)
        return 0; /* fast path: nothing forced */
    /* Two bounds, and both matter: the row has to exist (g_force_size is how
     * long every row was allocated) and the slot has to be one this table
     * actually has. With the tables at different lengths the second is the
     * real one -- a row is as long as the LARGEST table so every type has
     * somewhere to record, and the per-table check is what stops a forced slot
     * being honoured in an area that does not reach that far. */
    if (type >= JOURNAL_TYPE_COUNT || idx >= g_force_size ||
        (uint32_t)idx >= journal_type_capacity(type))
        return 0;
    if (type_is_bool(type))
    {
        if (bit >= 8)
            return 0;
        return (g_forced[type][idx] >> bit) & 1;
    }
    return g_forced[type][idx] != 0;
}

/* Write a value straight into the image slot — NO forced-slot check. Shared
 * by apply_entry (after its drop check) and journal_force_set (the seed). */
static void apply_write_raw(const journal_entry_t *entry)
{
    uint16_t idx = entry->index;

    /* Bounds check against THIS ENTRY'S OWN TABLE.
     *
     * It used to compare against g_buffer_ptrs.buffer_size, one figure for
     * fourteen tables. That number is now the SMALLEST of them, so a write to
     * any longer table above the smallest table's length would be dropped --
     * silently, which is the failure mode this whole area keeps producing.
     *
     * Still compared as uint32_t rather than through a (uint16_t) cast: the
     * image may reach 65536, which that cast turns into 0 and drops every
     * write at exactly the largest legal image. */
    if ((uint32_t)idx >= journal_type_capacity(entry->buffer_type))
    {
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
    if ((entry->buffer_type == JOURNAL_BOOL_INPUT || entry->buffer_type == JOURNAL_BOOL_OUTPUT ||
         entry->buffer_type == JOURNAL_BOOL_MEMORY) &&
        entry->bit_index >= 8)
    {
        return;
    }

    switch ((journal_buffer_type_t)entry->buffer_type)
    {
    case JOURNAL_BOOL_INPUT:
    {
        IEC_BOOL *ptr = g_buffer_ptrs.bool_input[idx][entry->bit_index];
        if (ptr != NULL)
        {
            *ptr = (IEC_BOOL)(entry->value & 1);
        }
        break;
    }
    case JOURNAL_BOOL_OUTPUT:
    {
        IEC_BOOL *ptr = g_buffer_ptrs.bool_output[idx][entry->bit_index];
        if (ptr != NULL)
        {
            *ptr = (IEC_BOOL)(entry->value & 1);
        }
        break;
    }
    case JOURNAL_BOOL_MEMORY:
    {
        IEC_BOOL *ptr = g_buffer_ptrs.bool_memory[idx][entry->bit_index];
        if (ptr != NULL)
        {
            *ptr = (IEC_BOOL)(entry->value & 1);
        }
        break;
    }
    case JOURNAL_BYTE_INPUT:
    {
        IEC_BYTE *ptr = g_buffer_ptrs.byte_input[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_BYTE)(entry->value & 0xFF);
        }
        break;
    }
    case JOURNAL_BYTE_OUTPUT:
    {
        IEC_BYTE *ptr = g_buffer_ptrs.byte_output[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_BYTE)(entry->value & 0xFF);
        }
        break;
    }
    case JOURNAL_INT_INPUT:
    {
        IEC_UINT *ptr = g_buffer_ptrs.int_input[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_UINT)(entry->value & 0xFFFF);
        }
        break;
    }
    case JOURNAL_INT_OUTPUT:
    {
        IEC_UINT *ptr = g_buffer_ptrs.int_output[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_UINT)(entry->value & 0xFFFF);
        }
        break;
    }
    case JOURNAL_INT_MEMORY:
    {
        IEC_UINT *ptr = g_buffer_ptrs.int_memory[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_UINT)(entry->value & 0xFFFF);
        }
        break;
    }
    case JOURNAL_DINT_INPUT:
    {
        IEC_UDINT *ptr = g_buffer_ptrs.dint_input[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_UDINT)(entry->value & 0xFFFFFFFF);
        }
        break;
    }
    case JOURNAL_DINT_OUTPUT:
    {
        IEC_UDINT *ptr = g_buffer_ptrs.dint_output[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_UDINT)(entry->value & 0xFFFFFFFF);
        }
        break;
    }
    case JOURNAL_DINT_MEMORY:
    {
        IEC_UDINT *ptr = g_buffer_ptrs.dint_memory[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_UDINT)(entry->value & 0xFFFFFFFF);
        }
        break;
    }
    case JOURNAL_LINT_INPUT:
    {
        IEC_ULINT *ptr = g_buffer_ptrs.lint_input[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_ULINT)entry->value;
        }
        break;
    }
    case JOURNAL_LINT_OUTPUT:
    {
        IEC_ULINT *ptr = g_buffer_ptrs.lint_output[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_ULINT)entry->value;
        }
        break;
    }
    case JOURNAL_LINT_MEMORY:
    {
        IEC_ULINT *ptr = g_buffer_ptrs.lint_memory[idx];
        if (ptr != NULL)
        {
            *ptr = (IEC_ULINT)entry->value;
        }
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
    if (is_slot_forced(entry->buffer_type, entry->index, entry->bit_index))
    {
        return;
    }
    apply_write_raw(entry);
}

/* Pin an image slot to `value` and mark it forced. Seeds the slot immediately
 * (bypassing the drop), then every later journal write to it is dropped until
 * journal_force_clear. Called only from the dispatcher's debug-write drain,
 * under image_lock — the same serialization domain as apply_entry. */
void journal_force_set(journal_buffer_type_t type, uint16_t index, uint8_t bit, uint64_t value)
{
    /* BOTH BOUNDS: the row AND the table.
     *
     * g_force_size is how long every row was allocated -- the LONGEST table,
     * so each type has somewhere to record. It is not how far this type's
     * table reaches. While every table had the same length the two were one
     * number and could not disagree; they can now.
     *
     * With bool_output at 1 element and int_output at 100, g_force_size is
     * 100, so forcing bool_output index 5 passed this check, flipped the bit
     * and incremented g_force_count -- permanently disabling the fast path in
     * is_slot_forced -- while apply_write_raw and is_slot_forced both refused
     * it on the per-table bound. A force that did nothing at all, and said
     * nothing, which is the failure this guard exists to report. */
    if ((uint8_t)type >= JOURNAL_TYPE_COUNT || index >= g_force_size ||
        (uint32_t)index >= journal_type_capacity((uint8_t)type))
    {
        /* Counted, not logged: see g_force_oob_drops. When the map was never
         * allocated g_force_size is 0 and EVERY force lands here. */
        g_force_oob_drops++;
        return;
    }
    if (type_is_bool((uint8_t)type) && bit >= 8)
    {
        return;
    }
    uint8_t mask = type_is_bool((uint8_t)type) ? (uint8_t)(1u << bit) : (uint8_t)0x01;
    if (!(g_forced[type][index] & mask))
    {
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
    /* BOTH BOUNDS: the row AND the table.
     *
     * g_force_size is how long every row was allocated -- the LONGEST table,
     * so each type has somewhere to record. It is not how far this type's
     * table reaches. While every table had the same length the two were one
     * number and could not disagree; they can now.
     *
     * With bool_output at 1 element and int_output at 100, g_force_size is
     * 100, so forcing bool_output index 5 passed this check, flipped the bit
     * and incremented g_force_count -- permanently disabling the fast path in
     * is_slot_forced -- while apply_write_raw and is_slot_forced both refused
     * it on the per-table bound. A force that did nothing at all, and said
     * nothing, which is the failure this guard exists to report. */
    if ((uint8_t)type >= JOURNAL_TYPE_COUNT || index >= g_force_size ||
        (uint32_t)index >= journal_type_capacity((uint8_t)type))
    {
        /* Counted, not logged: see g_force_oob_drops. When the map was never
         * allocated g_force_size is 0 and EVERY force lands here. */
        g_force_oob_drops++;
        return;
    }
    if (type_is_bool((uint8_t)type) && bit >= 8)
    {
        return;
    }
    uint8_t mask = type_is_bool((uint8_t)type) ? (uint8_t)(1u << bit) : (uint8_t)0x01;
    if (g_forced[type][index] & mask)
    {
        g_forced[type][index] &= (uint8_t)~mask;
        if (g_force_count > 0)
        {
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

#define JOURNAL_NBANKS 2
#define JOURNAL_BANK_SHIFT 31u
#define JOURNAL_COUNT_MASK 0x7FFFFFFFu
/* Bounded wait for an in-flight producer's publish at flip time. Each spin is
 * one acquire load; this caps the consumer's wait so a dead/stalled producer
 * can never hang the scan. ~one yield every 64 spins helps on single-core. */
#define JOURNAL_PUBLISH_SPIN_MAX 200000u

typedef struct
{
    journal_entry_t entries[JOURNAL_MAX_ENTRIES];
    atomic_uchar published[JOURNAL_MAX_ENTRIES]; /* 0 = empty, 1 = ready */
} journal_bank_t;

static journal_bank_t g_banks[JOURNAL_NBANKS];
static atomic_uint g_control; /* [bank:1][count:31] */
static atomic_bool g_initialized = false;

int journal_init(const journal_buffer_ptrs_t *buffer_ptrs)
{
    if (buffer_ptrs == NULL)
    {
        log_error("Journal: buffer_ptrs is NULL");
        return -1;
    }
    if (buffer_ptrs->image_mutex == NULL)
    {
        log_error("Journal: image_mutex is NULL");
        return -1;
    }

    memcpy(&g_buffer_ptrs, buffer_ptrs, sizeof(journal_buffer_ptrs_t));

    /* The forced-slot bitmap follows the image, so forcing works across the
     * whole of it rather than the first 1024 slots. Rows are as long as the
     * LONGEST table, because they are one length for all fourteen types and
     * anything shorter cannot record a forced slot in the tables above it. */
    if (force_map_alloc(journal_longest_table()) != 0)
    {
        log_error("Journal: could not allocate the forced-slot map for %u slots",
                  journal_longest_table());
        return -1;
    }

    for (int b = 0; b < JOURNAL_NBANKS; b++)
    {
        memset(g_banks[b].entries, 0, sizeof(g_banks[b].entries));
        for (size_t i = 0; i < JOURNAL_MAX_ENTRIES; i++)
        {
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
    force_map_free();
    memset(&g_buffer_ptrs, 0, sizeof(g_buffer_ptrs));
}

bool journal_is_initialized(void)
{
    return atomic_load_explicit(&g_initialized, memory_order_acquire);
}

static int journal_add(uint8_t type, uint16_t index, uint8_t bit, uint64_t value)
{
    if (!atomic_load_explicit(&g_initialized, memory_order_acquire))
    {
        return -1;
    }

    /* Claim a unique (bank, slot). relaxed: publication ordering is carried by
     * the per-slot release/acquire below, not by the counter itself. */
    uint32_t ctrl = atomic_fetch_add_explicit(&g_control, 1u, memory_order_relaxed);
    uint32_t bank = ctrl >> JOURNAL_BANK_SHIFT;
    uint32_t slot = ctrl & JOURNAL_COUNT_MASK;

    if (slot >= JOURNAL_MAX_ENTRIES)
    {
        /* Overflow: the active bank is full for this cycle. Drop. The consumer
         * detects and reports the drop count from the raw control count. */
        return -1;
    }

    journal_entry_t *e = &g_banks[bank].entries[slot];
    e->sequence        = slot;
    e->buffer_type     = type;
    e->bit_index       = bit;
    e->index           = index;
    e->value           = value;

    /* Publish: release pairs with the consumer's acquire so the full entry is
     * visible before the flag is observed set. */
    atomic_store_explicit(&g_banks[bank].published[slot], 1u, memory_order_release);
    return 0;
}

void journal_apply_and_clear(void)
{
    if (!atomic_load_explicit(&g_initialized, memory_order_acquire))
    {
        return;
    }

    /* Fast path: nothing pending -> nothing to apply, so skip the bank flip.
     * The image already reflects every committed write. A producer that adds an
     * entry after this load is simply applied on the next drain (one cycle
     * later) -- the same ordering guarantee a flush-on-lock read offers. This
     * keeps a read-heavy plugin (locking every cycle to read %Q via image_lock)
     * from flipping the journal needlessly and racing producers mid-publish. */
    if ((atomic_load_explicit(&g_control, memory_order_relaxed) & JOURNAL_COUNT_MASK) == 0)
    {
        return;
    }

    uint32_t cur     = atomic_load_explicit(&g_control, memory_order_relaxed);
    uint32_t active  = cur >> JOURNAL_BANK_SHIFT;
    uint32_t newbank = active ^ 1u;

    /* Flip + reset count in one RMW. Linearizes producers into either the
     * retired bank (counted in `old`) or the fresh bank (count from 0). */
    uint32_t old =
        atomic_exchange_explicit(&g_control, newbank << JOURNAL_BANK_SHIFT, memory_order_acq_rel);
    uint32_t retired = old >> JOURNAL_BANK_SHIFT; /* == active */
    uint32_t raw     = old & JOURNAL_COUNT_MASK;
    uint32_t count   = raw;

    if (count > JOURNAL_MAX_ENTRIES)
    {
        log_warn("[JOURNAL] overflow: %u write(s) dropped this cycle "
                 "(capacity=%d) -- increase JOURNAL_MAX_ENTRIES or reduce the "
                 "plugin write rate",
                 raw - JOURNAL_MAX_ENTRIES, JOURNAL_MAX_ENTRIES);
        count = JOURNAL_MAX_ENTRIES;
    }

    journal_bank_t *bank = &g_banks[retired];
    for (uint32_t i = 0; i < count; i++)
    {
        uint32_t spins = 0;
        while (atomic_load_explicit(&bank->published[i], memory_order_acquire) == 0)
        {
            if (++spins >= JOURNAL_PUBLISH_SPIN_MAX)
            {
                break;
            }
            if ((spins & 0x3Fu) == 0)
            {
                sched_yield();
            }
        }
        if (atomic_load_explicit(&bank->published[i], memory_order_acquire) != 0)
        {
            apply_entry(&bank->entries[i]);
        }
        else
        {
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
static size_t g_count           = 0;
static uint32_t g_next_sequence = 0;
static pthread_mutex_t g_journal_mutex;
static bool g_initialized = false;

static void emergency_flush_locked(void);

int journal_init(const journal_buffer_ptrs_t *buffer_ptrs)
{
    if (buffer_ptrs == NULL)
    {
        log_error("Journal: buffer_ptrs is NULL");
        return -1;
    }
    if (buffer_ptrs->image_mutex == NULL)
    {
        log_error("Journal: image_mutex is NULL");
        return -1;
    }
    if (init_rt_mutex(&g_journal_mutex) != 0)
    {
        fprintf(stderr, "[JOURNAL] Error: failed to initialize mutex\n");
        return -1;
    }

    /* Allocated BEFORE the lock is taken, deliberately. Inside it, the early
     * return on failure would skip the unlock at the end of this function and
     * leave g_journal_mutex held forever -- every later journal_add,
     * journal_apply_and_clear and journal_is_initialized would block, taking
     * the scan thread with them, and journal_cleanup could not recover it. The
     * map depends on nothing this lock protects. */
    if (force_map_alloc(journal_longest_table()) != 0)
    {
        log_error("Journal: could not allocate the forced-slot map for %u slots",
                  journal_longest_table());
        return -1;
    }

    pthread_mutex_lock(&g_journal_mutex);
    memcpy(&g_buffer_ptrs, buffer_ptrs, sizeof(journal_buffer_ptrs_t));
    g_count         = 0;
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
    g_initialized   = false;
    g_count         = 0;
    g_next_sequence = 0;
    force_map_free();
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
    if (!g_initialized)
    {
        return -1;
    }
    pthread_mutex_lock(&g_journal_mutex);

    if (g_count >= JOURNAL_MAX_ENTRIES)
    {
        emergency_flush_locked();
    }
    journal_entry_t *e = &g_entries[g_count];
    e->sequence        = g_next_sequence++;
    e->buffer_type     = type;
    e->bit_index       = bit;
    e->index           = index;
    e->value           = value;
    g_count++;

    pthread_mutex_unlock(&g_journal_mutex);
    return 0;
}

void journal_apply_and_clear(void)
{
    if (!g_initialized)
    {
        return;
    }
    pthread_mutex_lock(&g_journal_mutex);
    for (size_t i = 0; i < g_count; i++)
    {
        apply_entry(&g_entries[i]);
    }
    g_count         = 0;
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
    for (size_t i = 0; i < g_count; i++)
    {
        apply_entry(&g_entries[i]);
    }
    g_count         = 0;
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

int journal_write_bool(journal_buffer_type_t type, uint16_t index, uint8_t bit, bool value)
{
    if (type != JOURNAL_BOOL_INPUT && type != JOURNAL_BOOL_OUTPUT && type != JOURNAL_BOOL_MEMORY)
    {
        return -1;
    }
    if (bit > 7)
    {
        return -1;
    }
    return journal_add((uint8_t)type, index, bit, value ? 1u : 0u);
}

int journal_write_byte(journal_buffer_type_t type, uint16_t index, uint8_t value)
{
    if (type != JOURNAL_BYTE_INPUT && type != JOURNAL_BYTE_OUTPUT)
    {
        return -1;
    }
    return journal_add((uint8_t)type, index, 0xFF, value);
}

int journal_write_int(journal_buffer_type_t type, uint16_t index, uint16_t value)
{
    if (type != JOURNAL_INT_INPUT && type != JOURNAL_INT_OUTPUT && type != JOURNAL_INT_MEMORY)
    {
        return -1;
    }
    return journal_add((uint8_t)type, index, 0xFF, value);
}

int journal_write_dint(journal_buffer_type_t type, uint16_t index, uint32_t value)
{
    if (type != JOURNAL_DINT_INPUT && type != JOURNAL_DINT_OUTPUT && type != JOURNAL_DINT_MEMORY)
    {
        return -1;
    }
    return journal_add((uint8_t)type, index, 0xFF, value);
}

int journal_write_lint(journal_buffer_type_t type, uint16_t index, uint64_t value)
{
    if (type != JOURNAL_LINT_INPUT && type != JOURNAL_LINT_OUTPUT && type != JOURNAL_LINT_MEMORY)
    {
        return -1;
    }
    return journal_add((uint8_t)type, index, 0xFF, value);
}
