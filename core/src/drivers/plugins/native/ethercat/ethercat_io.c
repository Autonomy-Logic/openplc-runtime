/**
 * @file ethercat_io.c
 * @brief EtherCAT I/O Module — IEC location parsing, channel mapping, and process data exchange
 *
 * Implements the bridge between the SOEM IOmap buffer and the OpenPLC
 * runtime I/O buffers (bool_input/output, byte_input/output, etc.).
 *
 * Flow:
 *  1. At startup, ecat_io_build_channel_map() walks every configured channel,
 *     resolves its IOmap pointer via PDO walking, parses its IEC location,
 *     and stores a compact mapping entry.
 *  2. Each PLC scan cycle:
 *     - cycle_start  → ecat_io_read_inputs()  copies IOmap → PLC inputs
 *     - cycle_end    → ecat_io_write_outputs() copies PLC outputs → IOmap
 */

#include "ethercat_io.h"
#include "ethercat_master.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <ctype.h>

/*
 * =============================================================================
 * Inline helpers — bit-level IOmap access
 * =============================================================================
 */

static inline uint8_t iomap_read_bit(const uint8_t *ptr, int bit)
{
    return (*ptr >> bit) & 0x01;
}

static inline void iomap_write_bit(uint8_t *ptr, int bit, uint8_t val)
{
    if (val)
        *ptr |= (uint8_t)(1 << bit);
    else
        *ptr &= (uint8_t)~(1 << bit);
}

/*
 * =============================================================================
 * IEC Location Parser
 * =============================================================================
 */

int ecat_io_parse_iec_location(const char *loc_str, iec_location_t *loc)
{
    if (!loc_str || !loc)
        return -1;

    const char *p = loc_str;

    /* Expect leading '%' */
    if (*p != '%')
        return -1;
    p++;

    /* Direction: I or Q */
    switch (toupper((unsigned char)*p)) {
    case 'I': loc->direction = IEC_DIR_INPUT;  break;
    case 'Q': loc->direction = IEC_DIR_OUTPUT; break;
    default:  return -1;
    }
    p++;

    /* Size qualifier: X, B, W, D, L */
    switch (toupper((unsigned char)*p)) {
    case 'X': loc->size = IEC_SIZE_BIT;   break;
    case 'B': loc->size = IEC_SIZE_BYTE;  break;
    case 'W': loc->size = IEC_SIZE_WORD;  break;
    case 'D': loc->size = IEC_SIZE_DWORD; break;
    case 'L': loc->size = IEC_SIZE_LWORD; break;
    default:  return -1;
    }
    p++;

    /* Byte index (decimal) */
    if (!isdigit((unsigned char)*p))
        return -1;

    char *endptr = NULL;
    long byte_val = strtol(p, &endptr, 10);
    if (endptr == p || byte_val < 0)
        return -1;
    loc->byte_index = (int)byte_val;
    p = endptr;

    /* Optional bit index — only valid for X (bit) size */
    loc->bit_index = -1;
    if (*p == '.') {
        p++;
        if (!isdigit((unsigned char)*p))
            return -1;
        long bit_val = strtol(p, &endptr, 10);
        if (endptr == p || bit_val < 0 || bit_val > 7)
            return -1;
        if (loc->size != IEC_SIZE_BIT)
            return -1;  /* bit index only meaningful for X size */
        loc->bit_index = (int)bit_val;
        p = endptr;
    } else if (loc->size == IEC_SIZE_BIT) {
        /* X size without explicit bit → default to bit 0 */
        loc->bit_index = 0;
    }

    /* Must be at end of string */
    if (*p != '\0')
        return -1;

    return 0;
}

/*
 * =============================================================================
 * IOmap Offset Calculation (static)
 * =============================================================================
 */

/**
 * @brief Walk PDO entries to find the IOmap pointer and bit offset for a channel
 *
 * For a given channel (identified by pdo_index + pdo_entry_index + pdo_entry_subindex),
 * accumulates bit lengths through the PDO list until the target entry is found.
 *
 * @param soem_slave  Live SOEM slave descriptor (provides outputs/inputs base pointer)
 * @param cfg_slave   Configured slave (provides PDO lists from JSON)
 * @param channel     Channel to locate
 * @param is_output   true if channel is an output (RxPDO), false for input (TxPDO)
 * @param out_ptr     [out] pointer into IOmap buffer
 * @param out_bit     [out] bit offset within *out_ptr (0-7)
 * @return 0 on success, -1 if channel's PDO entry was not found
 */
static int calculate_iomap_offset(const ec_slavet *soem_slave,
                                  const ecat_slave_t *cfg_slave,
                                  const ecat_channel_t *channel,
                                  bool is_output,
                                  uint8_t **out_ptr,
                                  int *out_bit)
{
    /* Select PDO direction:
     *   Output channels → RxPDOs (master writes to slave) → soem_slave->outputs
     *   Input channels  → TxPDOs (slave sends to master)  → soem_slave->inputs
     */
    const ecat_pdo_t *pdos;
    int pdo_count;
    uint8_t *base_ptr;
    int start_bit;

    if (is_output) {
        pdos      = cfg_slave->rx_pdos;
        pdo_count = cfg_slave->rx_pdo_count;
        base_ptr  = soem_slave->outputs;
        start_bit = soem_slave->Ostartbit;
    } else {
        pdos      = cfg_slave->tx_pdos;
        pdo_count = cfg_slave->tx_pdo_count;
        base_ptr  = soem_slave->inputs;
        start_bit = soem_slave->Istartbit;
    }

    if (!base_ptr)
        return -1;

    int accumulated_bits = start_bit;

    for (int p = 0; p < pdo_count; p++) {
        const ecat_pdo_t *pdo = &pdos[p];
        for (int e = 0; e < pdo->entry_count; e++) {
            const ecat_pdo_entry_t *entry = &pdo->entries[e];

            /* Check if this is the target entry */
            if (strcmp(pdo->index, channel->pdo_index) == 0 &&
                strcmp(entry->index, channel->pdo_entry_index) == 0 &&
                entry->subindex == channel->pdo_entry_subindex) {
                *out_ptr = base_ptr + (accumulated_bits / 8);
                *out_bit = accumulated_bits % 8;
                return 0;
            }

            accumulated_bits += entry->bit_length;
        }
    }

    return -1;  /* entry not found */
}

/*
 * =============================================================================
 * Channel Map Builder
 * =============================================================================
 */

int ecat_io_build_channel_map(const ecat_config_t *config,
                              ecat_channel_map_t *map,
                              plugin_runtime_args_t *args,
                              plugin_logger_t *logger)
{
    memset(map, 0, sizeof(*map));

    int errors = 0;
    int mapped = 0;

    for (int s = 0; s < config->slave_count; s++) {
        const ecat_slave_t *cfg_slave = &config->slaves[s];
        int pos = cfg_slave->position;

        /* Get live SOEM slave descriptor */
        const ec_slavet *soem_slave = ecat_master_get_slave(pos);
        if (!soem_slave) {
            plugin_logger_warn(logger,
                "Slave '%s' position %d: not found in SOEM context, skipping channels",
                cfg_slave->name, pos);
            errors++;
            continue;
        }

        for (int c = 0; c < cfg_slave->channel_count; c++) {
            const ecat_channel_t *ch = &cfg_slave->channels[c];

            /* Skip channels without IEC location */
            if (ch->iec_location[0] == '\0')
                continue;

            /* Parse IEC location */
            iec_location_t iec_loc;
            if (ecat_io_parse_iec_location(ch->iec_location, &iec_loc) != 0) {
                plugin_logger_warn(logger,
                    "Slave '%s' channel '%s': invalid IEC location '%s', skipping",
                    cfg_slave->name, ch->name, ch->iec_location);
                errors++;
                continue;
            }

            /* Bounds check against PLC buffer size */
            if (iec_loc.byte_index >= args->buffer_size) {
                plugin_logger_warn(logger,
                    "Slave '%s' channel '%s': IEC location '%s' byte index %d "
                    "exceeds buffer size %d, skipping",
                    cfg_slave->name, ch->name, ch->iec_location,
                    iec_loc.byte_index, args->buffer_size);
                errors++;
                continue;
            }

            /* Determine channel direction from type string */
            bool is_output = (strstr(ch->type, "output") != NULL);

            /* Calculate IOmap offset by walking PDO entries */
            uint8_t *iomap_ptr = NULL;
            int iomap_bit = 0;
            if (calculate_iomap_offset(soem_slave, cfg_slave, ch, is_output,
                                       &iomap_ptr, &iomap_bit) != 0) {
                plugin_logger_warn(logger,
                    "Slave '%s' channel '%s': PDO entry not found "
                    "(pdo=%s entry=%s sub=%d), skipping",
                    cfg_slave->name, ch->name,
                    ch->pdo_index, ch->pdo_entry_index, ch->pdo_entry_subindex);
                errors++;
                continue;
            }

            /* Build the map entry */
            ecat_channel_map_entry_t entry;
            entry.iomap_ptr        = iomap_ptr;
            entry.iomap_bit_offset = iomap_bit;
            entry.bit_length       = ch->bit_length;
            entry.size             = iec_loc.size;
            entry.byte_index       = iec_loc.byte_index;
            entry.bit_index        = iec_loc.bit_index;

            /* Add to the appropriate direction array */
            if (iec_loc.direction == IEC_DIR_INPUT) {
                if (map->input_count < ECAT_MAX_MAP_ENTRIES) {
                    map->inputs[map->input_count++] = entry;
                    mapped++;
                } else {
                    plugin_logger_warn(logger, "Input channel map full (%d entries)",
                                       ECAT_MAX_MAP_ENTRIES);
                    errors++;
                }
            } else {
                if (map->output_count < ECAT_MAX_MAP_ENTRIES) {
                    map->outputs[map->output_count++] = entry;
                    mapped++;
                } else {
                    plugin_logger_warn(logger, "Output channel map full (%d entries)",
                                       ECAT_MAX_MAP_ENTRIES);
                    errors++;
                }
            }

            plugin_logger_debug(logger,
                "  Mapped: slave '%s' ch '%s' (%s) → %s byte=%d bit=%d",
                cfg_slave->name, ch->name, ch->iec_location,
                (iec_loc.direction == IEC_DIR_INPUT) ? "INPUT" : "OUTPUT",
                iec_loc.byte_index, iec_loc.bit_index);
        }
    }

    plugin_logger_info(logger,
        "Channel map built: %d inputs, %d outputs (%d errors)",
        map->input_count, map->output_count, errors);

    return (mapped > 0) ? 0 : -1;
}

/*
 * =============================================================================
 * Per-Cycle I/O Functions
 * =============================================================================
 */

/**
 * @note Thread safety: buffer_mutex must be held by the caller.
 * This is guaranteed by plc_cycle_thread() in plc_state_manager.c
 * which holds buffer_mutex across the entire scan cycle.
 */
void ecat_io_read_inputs(const ecat_channel_map_t *map,
                         plugin_runtime_args_t *args)
{
    for (int i = 0; i < map->input_count; i++) {
        const ecat_channel_map_entry_t *e = &map->inputs[i];

        switch (e->size) {
        case IEC_SIZE_BIT:
            if (args->bool_input &&
                args->bool_input[e->byte_index] &&
                args->bool_input[e->byte_index][e->bit_index]) {
                *args->bool_input[e->byte_index][e->bit_index] =
                    iomap_read_bit(e->iomap_ptr, e->iomap_bit_offset);
            }
            break;

        case IEC_SIZE_BYTE:
            if (args->byte_input && args->byte_input[e->byte_index]) {
                *args->byte_input[e->byte_index] = *e->iomap_ptr;
            }
            break;

        case IEC_SIZE_WORD:
            if (args->int_input && args->int_input[e->byte_index]) {
                memcpy(args->int_input[e->byte_index], e->iomap_ptr, 2);
            }
            break;

        case IEC_SIZE_DWORD:
            if (args->dint_input && args->dint_input[e->byte_index]) {
                memcpy(args->dint_input[e->byte_index], e->iomap_ptr, 4);
            }
            break;

        case IEC_SIZE_LWORD:
            if (args->lint_input && args->lint_input[e->byte_index]) {
                memcpy(args->lint_input[e->byte_index], e->iomap_ptr, 8);
            }
            break;
        }
    }
}

/**
 * @note Thread safety: buffer_mutex must be held by the caller.
 * This is guaranteed by plc_cycle_thread() in plc_state_manager.c
 * which holds buffer_mutex across the entire scan cycle.
 */
void ecat_io_write_outputs(const ecat_channel_map_t *map,
                           plugin_runtime_args_t *args)
{
    for (int i = 0; i < map->output_count; i++) {
        const ecat_channel_map_entry_t *e = &map->outputs[i];

        switch (e->size) {
        case IEC_SIZE_BIT:
            if (args->bool_output &&
                args->bool_output[e->byte_index] &&
                args->bool_output[e->byte_index][e->bit_index]) {
                iomap_write_bit(e->iomap_ptr, e->iomap_bit_offset,
                                *args->bool_output[e->byte_index][e->bit_index]);
            }
            break;

        case IEC_SIZE_BYTE:
            if (args->byte_output && args->byte_output[e->byte_index]) {
                *e->iomap_ptr = *args->byte_output[e->byte_index];
            }
            break;

        case IEC_SIZE_WORD:
            if (args->int_output && args->int_output[e->byte_index]) {
                memcpy(e->iomap_ptr, args->int_output[e->byte_index], 2);
            }
            break;

        case IEC_SIZE_DWORD:
            if (args->dint_output && args->dint_output[e->byte_index]) {
                memcpy(e->iomap_ptr, args->dint_output[e->byte_index], 4);
            }
            break;

        case IEC_SIZE_LWORD:
            if (args->lint_output && args->lint_output[e->byte_index]) {
                memcpy(e->iomap_ptr, args->lint_output[e->byte_index], 8);
            }
            break;
        }
    }
}
