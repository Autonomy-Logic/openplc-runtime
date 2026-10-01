// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file ethercat_io.h
 * @brief EtherCAT I/O Module — IEC location parsing, channel mapping, and process data exchange
 *
 * Bridges the SOEM IOmap and the OpenPLC runtime I/O buffers.
 * Provides:
 *  - IEC 61131-3 location string parser (%IX0.0, %QW3, etc.)
 *  - Channel map builder that links each configured channel to its
 *    IOmap byte/bit and PLC buffer position
 *  - Per-cycle read/write helpers called from cycle_start/cycle_end
 */

#ifndef ETHERCAT_IO_H
#define ETHERCAT_IO_H

#include <stdint.h>
#include "ethercat_config.h"
#include "plugin_types.h"
#include "plugin_logger.h"
#include "soem/soem.h"

/*
 * I/O type definitions (iec_size_t, iec_dir_t, iec_location_t,
 * ecat_channel_map_entry_t, ecat_channel_map_t, ecat_transfer_entry_t,
 * ecat_transfer_list_t, ECAT_MAX_MAP_ENTRIES) are defined in
 * ethercat_config.h so that ecat_master_instance_t can embed them.
 */

/**
 * @brief Parse an IEC 61131-3 location string into its components
 *
 * Accepted format: %[IQ][XBWDL]<byte>[.<bit>]
 *   - Direction: I (input) or Q (output)
 *   - Size: X (bit), B (byte), W (word), D (dword), L (lword)
 *   - Byte: decimal byte address
 *   - Bit: optional, only valid for X size, 0-7
 *
 * @param loc_str  NUL-terminated IEC location string
 * @param loc      Output parsed location
 * @return 0 on success, -1 on parse error
 */
int ecat_io_parse_iec_location(const char *loc_str, iec_location_t *loc);

/**
 * @brief Build the channel map from configuration + live SOEM state
 *
 * Iterates every slave/channel in @p config, resolves the IOmap pointer
 * via SOEM slave data, parses the IEC location, and stores the mapping
 * for use in the per-cycle read/write functions.
 *
 * @param config  Parsed EtherCAT configuration
 * @param map     Output channel map (zeroed before population)
 * @param inst    Master instance (provides SOEM context and IOmap)
 * @param args    Runtime args (for buffer_size bounds check)
 * @param logger  Logger instance
 * @return 0 on success, -1 if any channel failed to map (partial maps
 *         are rejected to surface JSON/ESI mismatches at startup)
 */
int ecat_io_build_channel_map(const ecat_config_t *config,
                              ecat_channel_map_t *map,
                              ecat_master_instance_t *inst,
                              plugin_runtime_args_t *args,
                              plugin_logger_t *logger);

/**
 * @brief Build a transfer list from a channel map and runtime args
 *
 * Resolves each channel map entry into a direct {plc_ptr, iomap_offset,
 * byte_count} triple.  Entries whose PLC pointer is NULL (unmapped IEC
 * address) are silently skipped.
 *
 * Must be called after ecat_io_build_channel_map() and after glueVars()
 * has populated the image table pointers.
 *
 * @param map    Channel map built by ecat_io_build_channel_map()
 * @param xfer   Output transfer list (zeroed before population)
 * @param args   Runtime args with PLC buffer pointers
 * @param logger Logger instance
 * @return 0 on success, -1 if zero channels resolved (image table not
 *         populated yet).  Channels without a PLC variable bound are
 *         skipped with a warn but do not fail the call.
 */
int ecat_io_build_transfer_list(const ecat_channel_map_t *map,
                                ecat_transfer_list_t *xfer,
                                plugin_runtime_args_t *args,
                                plugin_logger_t *logger);

/**
 * @brief Fast per-cycle: publish IOmap inputs into the PLC %I image
 *
 * Input values are written through the lock-free journal (args->journal_write_*)
 * rather than poked directly into the image, so they apply atomically at the
 * next scan boundary and never race the IEC task threads. No image lock is
 * held here.
 *
 * @param xfer       Transfer list built by ecat_io_build_transfer_list()
 * @param iomap_base Base pointer of the IOmap buffer
 * @param args       Runtime args providing the journal_write_* entry points
 */
void ecat_io_read_inputs_fast(const ecat_transfer_list_t *xfer,
                              const uint8_t *iomap_base,
                              plugin_runtime_args_t *args);

/**
 * @brief Fast per-cycle: copy PLC variables into IOmap outputs
 *
 * @param xfer       Transfer list built by ecat_io_build_transfer_list()
 * @param iomap_base Base pointer of the IOmap buffer
 */
void ecat_io_write_outputs_fast(const ecat_transfer_list_t *xfer,
                                uint8_t *iomap_base);

#endif /* ETHERCAT_IO_H */
