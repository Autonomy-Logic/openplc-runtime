// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file ethercat_master.h
 * @brief EtherCAT Master SOEM Wrapper Interface
 *
 * Provides a high-level interface for initializing and managing the
 * EtherCAT master using the SOEM library. Handles network initialization,
 * slave scanning, topology validation, state transitions, SDO configuration,
 * and slave recovery.
 *
 * The initialization sequence is split into discrete phases to support
 * the plugin state machine:
 *   1. open_and_scan   - Open interface, scan bus, validate topology
 *   2. write_sdos      - Write SDO parameters to slaves (in PRE-OP)
 *   3. configure       - Map process data, configure Distributed Clocks
 *   4. transition_to_op - Transition all slaves to OPERATIONAL
 */

#ifndef ETHERCAT_MASTER_H
#define ETHERCAT_MASTER_H

#include "ethercat_config.h"
#include "plugin_logger.h"
#include "soem/soem.h"

/**
 * @brief Open network interface, scan bus, and validate topology
 *
 * Performs initialization steps 1-3:
 *   1. Open network interface via SOEM
 *   2. Scan the bus for slaves
 *   3. Validate topology against JSON configuration
 *
 * @param inst   Per-master instance (contains config, SOEM context, IOmap)
 * @param logger Plugin logger instance
 * @return 0 on success, -1 on failure
 */
int ecat_master_open_and_scan(ecat_master_instance_t *inst, plugin_logger_t *logger);

/**
 * @brief Write SDO parameters to a slave
 *
 * Writes all configured SDO entries to the specified slave using ecx_SDOwrite.
 * Must be called while slaves are in PRE-OP state (after open_and_scan,
 * before configure).
 *
 * @param inst           Per-master instance
 * @param slave_pos      1-based slave position on the bus
 * @param sdos           Array of SDO configuration entries
 * @param sdo_count      Number of SDO entries
 * @param sdo_timeout_ms SDO operation timeout in milliseconds (0 = SOEM default)
 * @param logger         Plugin logger instance
 * @return 0 if all SDOs were written, -1 if any SDO write failed
 *         (sanity-check, missing CoE support, or any wkc<=0).  When the
 *         caller has slave->strict_sdo set, -1 should abort startup.
 */
int ecat_master_write_sdos(ecat_master_instance_t *inst, int slave_pos,
                           const ecat_sdo_config_t *sdos,
                           int sdo_count, int sdo_timeout_ms,
                           plugin_logger_t *logger);

/**
 * @brief Map process data and configure Distributed Clocks
 *
 * Performs initialization steps 4-5:
 *   4. Map process data (IOmap)
 *   5. Configure Distributed Clocks
 *
 * @param inst   Per-master instance
 * @param logger Plugin logger instance
 * @return 0 on success, -1 on failure
 */
int ecat_master_configure(ecat_master_instance_t *inst, plugin_logger_t *logger);

/**
 * @brief Transition all slaves to OPERATIONAL state
 *
 * Performs initialization steps 6-7:
 *   6. Wait for SAFE_OP state
 *   7. Request and poll for OPERATIONAL state
 *
 * Uses per-slave safeop_to_op_timeout_ms from the configuration (the
 * maximum across all slaves is applied to the broadcast statecheck).
 *
 * @param inst   Per-master instance (for config and SOEM context)
 * @param logger Plugin logger instance
 * @return 0 on success, -1 on failure
 */
int ecat_master_transition_to_op(ecat_master_instance_t *inst, plugin_logger_t *logger);

/**
 * @brief Validate bus topology against configuration
 *
 * Compares the slaves found on the bus with the expected configuration:
 * - Number of slaves must match
 * - For each slave: vendor_id and product_code must match
 *
 * @param inst   Per-master instance
 * @param logger Plugin logger instance
 * @return 0 on success, -1 on mismatch
 */
int ecat_master_validate_topology(ecat_master_instance_t *inst, plugin_logger_t *logger);

/**
 * @brief Close the EtherCAT master
 *
 * Transitions all slaves to INIT state and closes the network interface.
 *
 * @param inst   Per-master instance
 * @param logger Plugin logger instance
 */
void ecat_master_close(ecat_master_instance_t *inst, plugin_logger_t *logger);

/**
 * @brief Exchange process data with all slaves
 *
 * Sends outputs to slaves and receives inputs.
 *
 * @param inst       Per-master instance
 * @param timeout_us Receive timeout in microseconds (0 = use SOEM default)
 * @return Working counter value from receive, or -1 on error
 */
int ecat_master_exchange_processdata(ecat_master_instance_t *inst, int timeout_us);

/**
 * @brief Get the expected working counter for the bus
 *
 * Calculated as: outputsWKC * 2 + inputsWKC for group 0.
 *
 * @param inst Per-master instance
 * @return Expected WKC value
 */
int ecat_master_get_expected_wkc(ecat_master_instance_t *inst);

/**
 * @brief Get a pointer to a live SOEM slave descriptor
 *
 * @param inst     Per-master instance
 * @param position 1-based slave position on the bus
 * @return Pointer to ec_slavet, or NULL if position is invalid
 */
const ec_slavet *ecat_master_get_slave(ecat_master_instance_t *inst, int position);

/**
 * @brief Get the AL state of a specific slave
 *
 * Reads the current state from the SOEM slavelist (cached from last bus read).
 *
 * @param inst     Per-master instance
 * @param position 1-based slave position
 * @return EC_STATE_* value, or 0 if position is invalid
 */
uint16_t ecat_master_get_slave_state(ecat_master_instance_t *inst, int position);

/**
 * @brief Attempt to recover a slave that has left OPERATIONAL state
 *
 * Uses the SOEM recovery pattern:
 *   - SAFE_OP + ERROR: ACK error, then request OP
 *   - SAFE_OP: request OP directly
 *   - Lower states: ecx_reconfig_slave + ecx_recover_slave
 *
 * @param inst     Per-master instance
 * @param position 1-based slave position
 * @param logger   Plugin logger instance
 * @return 1 if recovered to OP, 0 if still recovering, -1 on error
 */
int ecat_master_recover_slave(ecat_master_instance_t *inst, int position, plugin_logger_t *logger);

/**
 * @brief Read back all slave states from the bus
 *
 * Calls ecx_readstate() to refresh the cached slave states.
 *
 * @param inst Per-master instance
 */
void ecat_master_read_states(ecat_master_instance_t *inst);

/**
 * @brief Get a pointer to the IOmap buffer base
 *
 * @param inst Per-master instance
 * @return Pointer to the IOmap buffer, or NULL if not initialized
 */
uint8_t *ecat_master_get_iomap(ecat_master_instance_t *inst);

/**
 * @brief Get the total IOmap size (inputs + outputs)
 *
 * @param inst Per-master instance
 * @return Total bytes used in the IOmap, or 0 if not initialized
 */
size_t ecat_master_get_iomap_size(ecat_master_instance_t *inst);

/**
 * @brief Get the number of slaves discovered on the bus
 *
 * @param inst Per-master instance
 * @return Slave count, or 0 if not initialized
 */
int ecat_master_get_slave_count(ecat_master_instance_t *inst);

#endif /* ETHERCAT_MASTER_H */
