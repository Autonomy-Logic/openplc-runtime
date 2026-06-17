/**
 * @file journal_buffer.h
 * @brief Journal Buffer System for Race-Condition-Free Plugin Writes
 *
 * This module provides a journaled write mechanism for plugins to write to
 * OpenPLC image tables without race conditions. All plugin writes go through
 * the journal buffer, which is applied atomically at the start of each PLC
 * scan cycle.
 *
 * Key design principles:
 * - Writes go to journal: All plugin writes are recorded with a sequence number
 * - Reads are direct: Plugins read directly from image tables
 * - Atomic application: Journal is applied at cycle_start before plugin hooks
 * - Last writer wins: Entries applied in sequence order
 *
 * Usage:
 *   // In plugin code, instead of:
 *   //   *bool_output[5][3] = true;
 *   // Use:
 *   journal_write_bool(JOURNAL_BOOL_OUTPUT, 5, 3, true);
 *
 * @note Thread-safe. Uses internal mutex for journal operations.
 */

#ifndef JOURNAL_BUFFER_H
#define JOURNAL_BUFFER_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <pthread.h>
#include "../lib/iec_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Journal capacity per cycle (per bank in the lock-free implementation)
 *
 * Sized so a full scan's worth of plugin writes fits comfortably. Writes
 * accumulate between drains -- a plugin running faster than the fastest task
 * contributes several writes per cell per drain -- so this is deliberately
 * generous. Overflow within a cycle is handled by the implementation: the
 * lock-free path drops the excess write(s) and logs it; the mutex fallback
 * triggers an emergency flush. Keep this large enough that overflow only
 * happens under genuine misconfiguration/overload.
 */
#define JOURNAL_MAX_ENTRIES 4096

/**
 * @brief Buffer type enumeration for journal entries
 *
 * Matches the OpenPLC image table types.
 */
typedef enum {
    JOURNAL_BOOL_INPUT = 0,
    JOURNAL_BOOL_OUTPUT,
    JOURNAL_BOOL_MEMORY,
    JOURNAL_BYTE_INPUT,
    JOURNAL_BYTE_OUTPUT,
    JOURNAL_INT_INPUT,
    JOURNAL_INT_OUTPUT,
    JOURNAL_INT_MEMORY,
    JOURNAL_DINT_INPUT,
    JOURNAL_DINT_OUTPUT,
    JOURNAL_DINT_MEMORY,
    JOURNAL_LINT_INPUT,
    JOURNAL_LINT_OUTPUT,
    JOURNAL_LINT_MEMORY,
    JOURNAL_TYPE_COUNT
} journal_buffer_type_t;

/**
 * @brief Journal entry structure
 *
 * Each entry represents a single write operation to be applied.
 * Entries are applied in sequence order during journal_apply_and_clear().
 */
typedef struct {
    uint32_t sequence;          /**< Auto-increment, determines apply order */
    uint8_t  buffer_type;       /**< journal_buffer_type_t enum */
    uint8_t  bit_index;         /**< For bool types: 0-7, for others: 0xFF */
    uint16_t index;             /**< Buffer array index */
    uint64_t value;             /**< Value to write (sized for largest type) */
} journal_entry_t;

/**
 * @brief Buffer pointers structure for journal initialization
 *
 * Contains pointers to all image table arrays and the image table mutex.
 */
typedef struct {
    /* Boolean buffers (pointer to 2D array) */
    IEC_BOOL *(*bool_input)[8];
    IEC_BOOL *(*bool_output)[8];
    IEC_BOOL *(*bool_memory)[8];

    /* Byte buffers */
    IEC_BYTE **byte_input;
    IEC_BYTE **byte_output;

    /* Integer buffers (16-bit) */
    IEC_UINT **int_input;
    IEC_UINT **int_output;
    IEC_UINT **int_memory;

    /* Double integer buffers (32-bit) */
    IEC_UDINT **dint_input;
    IEC_UDINT **dint_output;
    IEC_UDINT **dint_memory;

    /* Long integer buffers (64-bit) */
    IEC_ULINT **lint_input;
    IEC_ULINT **lint_output;
    IEC_ULINT **lint_memory;

    /* Buffer size (number of elements in each array) */
    int buffer_size;

    /* Image table mutex (for emergency flush and apply operations) */
    pthread_mutex_t *image_mutex;
} journal_buffer_ptrs_t;

/**
 * @brief Initialize the journal buffer system
 *
 * Must be called during runtime initialization, after image tables are set up.
 *
 * @param buffer_ptrs Pointer to structure containing image table pointers
 * @return 0 on success, -1 on failure
 */
int journal_init(const journal_buffer_ptrs_t *buffer_ptrs);

/**
 * @brief Cleanup journal buffer resources
 *
 * Should be called during runtime shutdown.
 */
void journal_cleanup(void);

/**
 * @brief Check if journal buffer is initialized
 *
 * @return true if initialized and ready for use
 */
bool journal_is_initialized(void);

/**
 * @brief Write a boolean value to the journal
 *
 * @param type Buffer type (JOURNAL_BOOL_INPUT, JOURNAL_BOOL_OUTPUT, JOURNAL_BOOL_MEMORY)
 * @param index Buffer array index (0 to buffer_size-1)
 * @param bit Bit index within the byte (0-7)
 * @param value Boolean value to write
 * @return 0 on success, -1 on failure
 */
int journal_write_bool(journal_buffer_type_t type, uint16_t index,
                       uint8_t bit, bool value);

/**
 * @brief Write a byte value to the journal
 *
 * @param type Buffer type (JOURNAL_BYTE_INPUT, JOURNAL_BYTE_OUTPUT)
 * @param index Buffer array index (0 to buffer_size-1)
 * @param value Byte value to write
 * @return 0 on success, -1 on failure
 */
int journal_write_byte(journal_buffer_type_t type, uint16_t index,
                       uint8_t value);

/**
 * @brief Write a 16-bit integer value to the journal
 *
 * @param type Buffer type (JOURNAL_INT_INPUT, JOURNAL_INT_OUTPUT, JOURNAL_INT_MEMORY)
 * @param index Buffer array index (0 to buffer_size-1)
 * @param value 16-bit value to write
 * @return 0 on success, -1 on failure
 */
int journal_write_int(journal_buffer_type_t type, uint16_t index,
                      uint16_t value);

/**
 * @brief Write a 32-bit integer value to the journal
 *
 * @param type Buffer type (JOURNAL_DINT_INPUT, JOURNAL_DINT_OUTPUT, JOURNAL_DINT_MEMORY)
 * @param index Buffer array index (0 to buffer_size-1)
 * @param value 32-bit value to write
 * @return 0 on success, -1 on failure
 */
int journal_write_dint(journal_buffer_type_t type, uint16_t index,
                       uint32_t value);

/**
 * @brief Write a 64-bit integer value to the journal
 *
 * @param type Buffer type (JOURNAL_LINT_INPUT, JOURNAL_LINT_OUTPUT, JOURNAL_LINT_MEMORY)
 * @param index Buffer array index (0 to buffer_size-1)
 * @param value 64-bit value to write
 * @return 0 on success, -1 on failure
 */
int journal_write_lint(journal_buffer_type_t type, uint16_t index,
                       uint64_t value);

/**
 * @brief Pin an image slot to a forced value (force locks out external writes)
 *
 * Seeds the slot with @p value and marks it forced. Until journal_force_clear,
 * every journal write to that slot (the program's own copy_out AND any plugin)
 * is dropped at apply time, so the forced value owns the slot for the whole
 * cycle. This is the located/image leg of variable forcing; globals and
 * program-internal leaves are forced in the IECVar instead.
 *
 * @param type  Buffer type (same enum as journal_write_*)
 * @param index Buffer array index
 * @param bit   Bit index (0-7) for BOOL types; ignored otherwise
 * @param value Forced value (sized for the largest type)
 *
 * @note MUST be called only from the dispatcher's debug-write drain, under the
 *       image lock — the same serialization domain as journal_apply_and_clear.
 */
void journal_force_set(journal_buffer_type_t type, uint16_t index,
                       uint8_t bit, uint64_t value);

/**
 * @brief Release a forced image slot (writes flow through again)
 *
 * @param type  Buffer type
 * @param index Buffer array index
 * @param bit   Bit index (0-7) for BOOL types; ignored otherwise
 *
 * @note Same calling constraint as journal_force_set.
 */
void journal_force_clear(journal_buffer_type_t type, uint16_t index,
                         uint8_t bit);

/**
 * @brief Apply all pending journal entries to image tables and clear the journal
 *
 * This function should be called at the start of each PLC scan cycle,
 * BEFORE plugin cycle_start hooks are called.
 *
 * @note The image table mutex (image_mutex) MUST be held by the caller.
 *       This function acquires the journal mutex internally.
 */
void journal_apply_and_clear(void);

/**
 * @brief Get the number of pending journal entries
 *
 * Useful for diagnostics and monitoring.
 *
 * @return Number of entries waiting to be applied
 */
size_t journal_pending_count(void);

/**
 * @brief Get the current sequence number
 *
 * Useful for diagnostics. The sequence number increments with each write
 * and resets to 0 when the journal is cleared.
 *
 * @return Current sequence number
 */
uint32_t journal_get_sequence(void);

#ifdef __cplusplus
}
#endif

#endif /* JOURNAL_BUFFER_H */
