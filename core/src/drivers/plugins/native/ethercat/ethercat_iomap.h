// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file ethercat_iomap.h
 * @brief Binds EtherCAT process data entries to PLC located variables.
 *
 * The mapping file (ethercat_iomapping.json, written by the Editor) names each entry by
 * (master, slave position, entry index, subindex) and gives its IEC location. EtherDOG's
 * "layout" reply says where that entry sits in the exchanged image. Joining the two yields
 * a transfer list the client thread runs every cycle.
 */

#ifndef ETHERCAT_IOMAP_H
#define ETHERCAT_IOMAP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cJSON.h"
#include "plugin_types.h"

#define ECAT_IOMAP_MAX_MASTERS 4
#define ECAT_IOMAP_MAX_ENTRIES 2048
#define ECAT_IOMAP_NAME_LEN 64
/** Highest byte index a location may use; the runtime's journal indexes are 16-bit. */
#define ECAT_IOMAP_MAX_BYTE_INDEX 65535

typedef enum { IEC_SIZE_BIT, IEC_SIZE_BYTE, IEC_SIZE_WORD, IEC_SIZE_DWORD, IEC_SIZE_LWORD } iec_size_t;
typedef enum { IEC_DIR_INPUT, IEC_DIR_OUTPUT } iec_dir_t;

/** A parsed IEC 61131-3 location such as "%IX0.3" or "%QW12". */
typedef struct {
    iec_dir_t direction;
    iec_size_t size;
    int byte_index;
    int bit_index; /* 0-7 for X, -1 otherwise */
} iec_location_t;

/**
 * @brief Parse "%[IQ][XBWDL]<byte>[.<bit>]"; the bit part is only valid for X.
 * @return 0 on success, -1 on a malformed location.
 */
int ecat_io_parse_iec_location(const char *loc_str, iec_location_t *loc);

/** One mapping file entry. */
typedef struct {
    int slave;
    uint16_t index;
    uint8_t subindex;
    char iec_location[16];
    iec_location_t loc;
} ecat_iomap_entry_t;

typedef struct {
    char name[ECAT_IOMAP_NAME_LEN];
    ecat_iomap_entry_t entries[ECAT_IOMAP_MAX_ENTRIES];
    int entry_count;
} ecat_iomap_master_t;

typedef struct {
    ecat_iomap_master_t masters[ECAT_IOMAP_MAX_MASTERS];
    int master_count;
} ecat_iomap_t;

/** One resolved per-cycle copy between the frame payload and a PLC variable. */
typedef struct {
    uint32_t bit_offset;
    uint8_t bit_length;
    iec_size_t size;
    void *plc_ptr;     /* outputs: variable read under image_lock */
    int journal_index; /* inputs: byte index into the %I image */
    int journal_bit;
} ecat_xfer_t;

typedef struct {
    bool active;
    uint32_t output_bytes;
    uint32_t input_bytes;
    ecat_xfer_t inputs[ECAT_IOMAP_MAX_ENTRIES];
    int input_count;
    ecat_xfer_t outputs[ECAT_IOMAP_MAX_ENTRIES];
    int output_count;
} ecat_bound_master_t;

typedef struct {
    ecat_bound_master_t masters[ECAT_IOMAP_MAX_MASTERS];
} ecat_bound_map_t;

/** Load and validate the mapping file. Returns 0, or -1 with @p err naming the problem. */
int ecat_iomap_load(const char *path, ecat_iomap_t *map, char *err, size_t err_size);

/**
 * @brief Join the mapping with EtherDOG's layout reply and resolve PLC pointers.
 *
 * Every mapped entry must exist in the layout with a matching direction and width; any
 * mismatch fails the whole bind with @p err naming the entry. Entries whose PLC variable is
 * not declared in the program are skipped, as the in-process plugin did.
 *
 * @return 0 on success, -1 on failure.
 */
int ecat_iomap_bind(const ecat_iomap_t *map, const cJSON *layout, plugin_runtime_args_t *args,
                    ecat_bound_map_t *out, char *err, size_t err_size);

/** Publish one input frame into the %I image through the journal (lock-free). */
void ecat_iomap_publish_inputs(const ecat_bound_master_t *m, const uint8_t *payload,
                               size_t len, plugin_runtime_args_t *args);

/** Fill @p payload (zeroed first) from the %Q image. Call between image_lock/unlock. */
void ecat_iomap_collect_outputs(const ecat_bound_master_t *m, uint8_t *payload, size_t len);

#endif /* ETHERCAT_IOMAP_H */
