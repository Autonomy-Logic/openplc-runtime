/**
 * @file ethercat_master.c
 * @brief EtherCAT Master SOEM Wrapper Implementation
 *
 * Wraps the SOEM library to provide high-level EtherCAT master operations:
 * network initialization, slave scanning, topology validation against
 * the JSON configuration, and state machine management.
 *
 * Uses the ecx_* context-based API from SOEM 2.x.
 */

#include "ethercat_master.h"
#include "soem/soem.h"

#include <string.h>
#include <stdio.h>

/*
 * =============================================================================
 * SOEM Context and IO Map
 * =============================================================================
 */

/** Maximum IO map size in bytes */
#define ECAT_IOMAP_SIZE 4096

/** Number of retries when polling for OPERATIONAL state */
#define ECAT_OP_POLL_RETRIES 10

/** SOEM context - manages all bus state */
static ecx_contextt g_ecx_context;

/** IO map buffer for process data exchange */
static uint8 g_iomap[ECAT_IOMAP_SIZE];

/** Track whether SOEM was initialized (ec_init called) */
static int g_soem_initialized = 0;

/*
 * =============================================================================
 * Topology Validation
 * =============================================================================
 */

int ecat_master_validate_topology(const ecat_config_t *config, plugin_logger_t *logger)
{
    int found_count = g_ecx_context.slavecount;

    if (found_count != config->slave_count) {
        plugin_logger_error(logger,
            "Topology mismatch: expected %d slaves, found %d on the bus",
            config->slave_count, found_count);
        return -1;
    }

    for (int i = 0; i < config->slave_count; i++) {
        const ecat_slave_t *expected = &config->slaves[i];
        int pos = expected->position;

        if (pos < 1 || pos > found_count) {
            plugin_logger_error(logger,
                "Slave %d: position %d is out of range (1-%d)",
                i, pos, found_count);
            return -1;
        }

        ec_slavet *found = &g_ecx_context.slavelist[pos];

        if (found->eep_man != expected->vendor_id) {
            plugin_logger_error(logger,
                "Slave %d (%s) at position %d: vendor_id mismatch - "
                "expected 0x%08X, found 0x%08X",
                i, expected->name, pos,
                expected->vendor_id, found->eep_man);
            return -1;
        }

        if (found->eep_id != expected->product_code) {
            plugin_logger_error(logger,
                "Slave %d (%s) at position %d: product_code mismatch - "
                "expected 0x%08X, found 0x%08X",
                i, expected->name, pos,
                expected->product_code, found->eep_id);
            return -1;
        }

        plugin_logger_debug(logger,
            "Slave %d (%s) at position %d: topology OK "
            "(vendor=0x%08X, product=0x%08X)",
            i, expected->name, pos,
            found->eep_man, found->eep_id);
    }

    plugin_logger_info(logger, "Topology validation passed: %d slaves match configuration",
                       config->slave_count);
    return 0;
}

/*
 * =============================================================================
 * Master Initialization
 * =============================================================================
 */

int ecat_master_init(const ecat_config_t *config, plugin_logger_t *logger)
{
    /* Zero-initialize the SOEM context before use */
    memset(&g_ecx_context, 0, sizeof(g_ecx_context));
    memset(g_iomap, 0, sizeof(g_iomap));

    /* Step 1: Initialize SOEM on the configured network interface */
    plugin_logger_info(logger, "Opening network interface: %s", config->master.interface);

    if (!ecx_init(&g_ecx_context, config->master.interface)) {
#if defined(__CYGWIN__) || defined(_WIN32)
        plugin_logger_error(logger,
            "Failed to initialize EtherCAT interface '%s'. "
            "Verify that Npcap (https://npcap.com) is installed and "
            "the interface name matches a network adapter (use "
            "'ipconfig' or Npcap's WlanHelper to list adapters).",
            config->master.interface);
#else
        plugin_logger_error(logger,
            "Failed to initialize EtherCAT interface '%s'. "
            "Check that the interface exists and the process has "
            "CAP_NET_RAW capability (or is running as root).",
            config->master.interface);
#endif
        return -1;
    }

    g_soem_initialized = 1;
    plugin_logger_info(logger, "Network interface opened successfully");

    /* Step 2: Scan the bus and enumerate slaves */
    plugin_logger_info(logger, "Scanning EtherCAT bus...");

    if (ecx_config_init(&g_ecx_context) <= 0) {
        plugin_logger_error(logger,
            "No EtherCAT slaves found on interface '%s'. "
            "Check cable connections and slave power.",
            config->master.interface);
        ecx_close(&g_ecx_context);
        g_soem_initialized = 0;
        return -1;
    }

    plugin_logger_info(logger, "Found %d slave(s) on the bus", g_ecx_context.slavecount);

    /* Log discovered slaves */
    for (int i = 1; i <= g_ecx_context.slavecount; i++) {
        ec_slavet *slave = &g_ecx_context.slavelist[i];
        plugin_logger_info(logger,
            "  [%d] %s - vendor=0x%08X, product=0x%08X, rev=0x%08X",
            i, slave->name, slave->eep_man, slave->eep_id, slave->eep_rev);
    }

    /* Step 3: Validate topology against JSON configuration */
    if (ecat_master_validate_topology(config, logger) != 0) {
        plugin_logger_error(logger,
            "Topology validation failed - aborting master initialization");
        ecx_close(&g_ecx_context);
        g_soem_initialized = 0;
        return -1;
    }

    /* Step 4: Map process data (IO map) */
    plugin_logger_info(logger, "Mapping process data...");

    ecx_config_map_group(&g_ecx_context, &g_iomap, 0);

    ec_groupt *grp = &g_ecx_context.grouplist[0];
    plugin_logger_info(logger, "IO map: %d output bytes, %d input bytes, %d segments",
                       grp->Obytes, grp->Ibytes, grp->nsegments);

    /* Step 5: Configure Distributed Clocks */
    plugin_logger_info(logger, "Configuring Distributed Clocks...");
    ecx_configdc(&g_ecx_context);

    /* Step 6: Wait for SAFE_OP after config */
    plugin_logger_info(logger, "Waiting for SAFE_OP state...");

    ecx_statecheck(&g_ecx_context, 0, EC_STATE_SAFE_OP, EC_TIMEOUTSTATE * 4);

    /* Read back actual states */
    ecx_readstate(&g_ecx_context);
    if (g_ecx_context.slavelist[0].state != EC_STATE_SAFE_OP) {
        plugin_logger_error(logger,
            "Not all slaves reached SAFE_OP state (current state: 0x%04X)",
            g_ecx_context.slavelist[0].state);

        /* Log individual slave states for debugging */
        for (int i = 1; i <= g_ecx_context.slavecount; i++) {
            ec_slavet *slave = &g_ecx_context.slavelist[i];
            if (slave->state != EC_STATE_SAFE_OP) {
                plugin_logger_error(logger,
                    "  Slave %d (%s): state=0x%04X, ALstatuscode=0x%04X",
                    i, slave->name, slave->state, slave->ALstatuscode);
            }
        }

        ecx_close(&g_ecx_context);
        g_soem_initialized = 0;
        return -1;
    }

    plugin_logger_info(logger, "All slaves in SAFE_OP state");

    /* Step 7: Send initial process data and request OPERATIONAL */
    plugin_logger_info(logger, "Requesting OPERATIONAL state...");

    /* Send one round of process data to make slave outputs happy */
    ecx_send_processdata(&g_ecx_context);
    ecx_receive_processdata(&g_ecx_context, EC_TIMEOUTRET);

    /* Request OP state */
    g_ecx_context.slavelist[0].state = EC_STATE_OPERATIONAL;
    ecx_writestate(&g_ecx_context, 0);

    /* Poll for OP state with process data exchange between checks */
    int op_reached = 0;
    for (int retry = 0; retry < ECAT_OP_POLL_RETRIES; retry++) {
        ecx_send_processdata(&g_ecx_context);
        ecx_receive_processdata(&g_ecx_context, EC_TIMEOUTRET);
        ecx_statecheck(&g_ecx_context, 0, EC_STATE_OPERATIONAL,
                        EC_TIMEOUTSTATE / ECAT_OP_POLL_RETRIES);

        if (g_ecx_context.slavelist[0].state == EC_STATE_OPERATIONAL) {
            op_reached = 1;
            break;
        }
    }

    if (!op_reached) {
        plugin_logger_error(logger,
            "Not all slaves reached OPERATIONAL state after %d retries",
            ECAT_OP_POLL_RETRIES);

        /* Log individual slave states for debugging */
        ecx_readstate(&g_ecx_context);
        for (int i = 1; i <= g_ecx_context.slavecount; i++) {
            ec_slavet *slave = &g_ecx_context.slavelist[i];
            if (slave->state != EC_STATE_OPERATIONAL) {
                plugin_logger_error(logger,
                    "  Slave %d (%s): state=0x%04X, ALstatuscode=0x%04X",
                    i, slave->name, slave->state, slave->ALstatuscode);
            }
        }

        ecx_close(&g_ecx_context);
        g_soem_initialized = 0;
        return -1;
    }

    plugin_logger_info(logger, "EtherCAT master operational with %d slave(s)",
                       g_ecx_context.slavecount);

    return 0;
}

/*
 * =============================================================================
 * Master Close
 * =============================================================================
 */

void ecat_master_close(plugin_logger_t *logger)
{
    if (!g_soem_initialized) {
        plugin_logger_debug(logger, "SOEM not initialized, nothing to close");
        return;
    }

    /* Transition all slaves to INIT state */
    plugin_logger_info(logger, "Transitioning slaves to INIT state...");
    g_ecx_context.slavelist[0].state = EC_STATE_INIT;
    ecx_writestate(&g_ecx_context, 0);

    /* Close the network interface */
    ecx_close(&g_ecx_context);
    g_soem_initialized = 0;

    /* Clear IO map */
    memset(g_iomap, 0, sizeof(g_iomap));

    plugin_logger_info(logger, "EtherCAT master closed");
}

/*
 * =============================================================================
 * Process Data and State Access
 * =============================================================================
 */

int ecat_master_exchange_processdata(int timeout_us)
{
    ecx_send_processdata(&g_ecx_context);
    int wkc = ecx_receive_processdata(&g_ecx_context,
                                       (timeout_us > 0) ? timeout_us : EC_TIMEOUTRET);
    return wkc;
}

int ecat_master_get_expected_wkc(void)
{
    ec_groupt *grp = &g_ecx_context.grouplist[0];
    return (grp->outputsWKC * 2) + grp->inputsWKC;
}

const ec_slavet *ecat_master_get_slave(int position)
{
    if (position < 1 || position > g_ecx_context.slavecount)
        return NULL;
    return &g_ecx_context.slavelist[position];
}

int ecat_master_is_operational(void)
{
    if (!g_soem_initialized)
        return 0;
    return (g_ecx_context.slavelist[0].state == EC_STATE_OPERATIONAL) ? 1 : 0;
}
