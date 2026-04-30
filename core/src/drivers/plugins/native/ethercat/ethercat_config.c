/**
 * @file ethercat_config.c
 * @brief EtherCAT Plugin Configuration Parser Implementation
 *
 * Parses JSON configuration files using the cJSON library.
 * The JSON format follows the contract defined by the OpenPLC Editor:
 *   [{ "name": "ethercat_master", "protocol": "ETHERCAT", "config": { ... } }]
 */

#include "ethercat_config.h"
#include "cJSON.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/** Linux IFNAMSIZ is 16; valid iface names are 1..15 chars. */
#define ECAT_LINUX_IFNAME_MAX 16

bool ecat_is_valid_iface_name(const char *iface)
{
    if (iface == NULL)
        return false;
    size_t len = strlen(iface);
    if (len == 0 || len >= ECAT_LINUX_IFNAME_MAX)
        return false;
    if (!isalpha((unsigned char)iface[0]))
        return false;
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)iface[i];
        if (!isalnum(c) && c != '_' && c != '-')
            return false;
    }
    return true;
}

/*
 * =============================================================================
 * Helper Functions
 * =============================================================================
 */

/**
 * @brief Read entire file into a string
 */
static char *read_file(const char *path)
{
    FILE *fp = fopen(path, "rb");
    if (fp == NULL) {
        return NULL;
    }

    fseek(fp, 0, SEEK_END);
    long size = ftell(fp);
    fseek(fp, 0, SEEK_SET);

    if (size <= 0 || size > 1024 * 1024) { /* Max 1MB config file */
        fclose(fp);
        return NULL;
    }

    char *buffer = (char *)malloc(size + 1);
    if (buffer == NULL) {
        fclose(fp);
        return NULL;
    }

    size_t read_size = fread(buffer, 1, size, fp);
    fclose(fp);

    if ((long)read_size != size) {
        free(buffer);
        return NULL;
    }

    buffer[size] = '\0';
    return buffer;
}

/**
 * @brief Safely copy string with length limit
 */
static void safe_strcpy(char *dest, const char *src, size_t max_len)
{
    if (src == NULL) {
        dest[0] = '\0';
        return;
    }
    strncpy(dest, src, max_len - 1);
    dest[max_len - 1] = '\0';
}

/**
 * @brief Get string value from JSON object
 */
static const char *get_string(const cJSON *obj, const char *key, const char *default_val)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (cJSON_IsString(item) && item->valuestring != NULL) {
        return item->valuestring;
    }
    return default_val;
}

/**
 * @brief Get integer value from JSON object
 */
static int get_int(const cJSON *obj, const char *key, int default_val)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (cJSON_IsNumber(item)) {
        return item->valueint;
    }
    return default_val;
}

/**
 * @brief Get a numeric value from a JSON field that may be a number or a string.
 *
 * Handles:
 *   - JSON numbers  (cJSON_IsNumber)
 *   - Decimal strings ("100", "-50")
 *   - Hex strings    ("0xFF", "0x1A")
 *   - Float strings  ("3.14", "-1.5e2")
 *
 * @return The parsed value, or default_val on missing/empty/unparseable input.
 */
static double get_numeric_value(const cJSON *obj, const char *key, double default_val)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (item == NULL)
        return default_val;

    if (cJSON_IsNumber(item))
        return item->valuedouble;

    if (cJSON_IsString(item) && item->valuestring != NULL && item->valuestring[0] != '\0') {
        const char *str = item->valuestring;
        char *endptr = NULL;

        /* Check for hex prefix */
        if (str[0] == '0' && (str[1] == 'x' || str[1] == 'X')) {
            long long hex_val = strtoll(str, &endptr, 16);
            if (endptr != str && *endptr == '\0')
                return (double)hex_val;
            return default_val;
        }

        /* Try parsing as double (covers integers, floats, negative, scientific) */
        double dval = strtod(str, &endptr);
        if (endptr != str && *endptr == '\0')
            return dval;
    }

    return default_val;
}

/**
 * @brief Get boolean value from JSON object
 */
static bool get_bool(const cJSON *obj, const char *key, bool default_val)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (cJSON_IsBool(item)) {
        return cJSON_IsTrue(item);
    }
    return default_val;
}

/**
 * @brief Convert hex string (e.g. "0x00000002") to uint32_t
 */
static uint32_t hex_to_uint32(const char *hex_str)
{
    if (hex_str == NULL) {
        return 0;
    }
    return (uint32_t)strtoul(hex_str, NULL, 16);
}

/**
 * @brief Case-insensitive string comparison
 */
static int strcasecmp_local(const char *a, const char *b)
{
    while (*a && *b) {
        int diff = tolower((unsigned char)*a) - tolower((unsigned char)*b);
        if (diff != 0)
            return diff;
        a++;
        b++;
    }
    return tolower((unsigned char)*a) - tolower((unsigned char)*b);
}

ecat_data_type_t ecat_parse_data_type(const char *str)
{
    if (str == NULL || str[0] == '\0')
        return ECAT_DTYPE_UNKNOWN;

    /* Boolean */
    if (strcasecmp_local(str, "BOOL") == 0)
        return ECAT_DTYPE_BOOL;

    /* 8-bit integer */
    if (strcasecmp_local(str, "INT8") == 0 || strcasecmp_local(str, "SINT") == 0)
        return ECAT_DTYPE_INT8;
    if (strcasecmp_local(str, "UINT8") == 0 || strcasecmp_local(str, "USINT") == 0 ||
        strcasecmp_local(str, "BYTE") == 0)
        return ECAT_DTYPE_UINT8;

    /* 16-bit integer */
    if (strcasecmp_local(str, "INT16") == 0 || strcasecmp_local(str, "INT") == 0)
        return ECAT_DTYPE_INT16;
    if (strcasecmp_local(str, "UINT16") == 0 || strcasecmp_local(str, "UINT") == 0 ||
        strcasecmp_local(str, "WORD") == 0)
        return ECAT_DTYPE_UINT16;

    /* 32-bit integer */
    if (strcasecmp_local(str, "INT32") == 0 || strcasecmp_local(str, "DINT") == 0)
        return ECAT_DTYPE_INT32;
    if (strcasecmp_local(str, "UINT32") == 0 || strcasecmp_local(str, "UDINT") == 0 ||
        strcasecmp_local(str, "DWORD") == 0)
        return ECAT_DTYPE_UINT32;

    /* 64-bit integer */
    if (strcasecmp_local(str, "INT64") == 0 || strcasecmp_local(str, "LINT") == 0)
        return ECAT_DTYPE_INT64;
    if (strcasecmp_local(str, "UINT64") == 0 || strcasecmp_local(str, "ULINT") == 0 ||
        strcasecmp_local(str, "LWORD") == 0)
        return ECAT_DTYPE_UINT64;

    /* 32-bit float (REAL) */
    if (strcasecmp_local(str, "REAL") == 0 || strcasecmp_local(str, "REAL32") == 0 ||
        strcasecmp_local(str, "FLOAT") == 0)
        return ECAT_DTYPE_REAL32;

    /* 64-bit float (LREAL) */
    if (strcasecmp_local(str, "LREAL") == 0 || strcasecmp_local(str, "REAL64") == 0 ||
        strcasecmp_local(str, "DOUBLE") == 0)
        return ECAT_DTYPE_REAL64;

    /* Padding */
    if (strcasecmp_local(str, "PAD") == 0)
        return ECAT_DTYPE_PAD;

    return ECAT_DTYPE_UNKNOWN;
}

/*
 * =============================================================================
 * Section Parsers
 * =============================================================================
 */

/**
 * @brief Parse master configuration from JSON
 */
static void parse_master_section(const cJSON *master, ecat_master_config_t *config)
{
    if (master == NULL) {
        return;
    }

    safe_strcpy(config->interface, get_string(master, "interface", "eth0"), sizeof(config->interface));
    config->cycle_time_us = get_int(master, "cycle_time_us", 1000);
    config->receive_timeout_us = get_int(master, "receive_timeout_us", 2000);
    config->watchdog_timeout_cycles = get_int(master, "watchdog_timeout_cycles", 3);
    safe_strcpy(config->log_level, get_string(master, "log_level", "info"), sizeof(config->log_level));
    safe_strcpy(config->task_name, get_string(master, "task_name", ""), sizeof(config->task_name));
    config->task_cycle_time_us = get_int(master, "task_cycle_time_us", 0);
}

/**
 * @brief Parse diagnostics configuration from JSON
 */
static void parse_diagnostics_section(const cJSON *diag, ecat_diagnostics_config_t *config)
{
    if (diag == NULL) {
        return;
    }

    config->log_connections = get_bool(diag, "log_connections", true);
    config->log_data_access = get_bool(diag, "log_data_access", false);
    config->log_errors = get_bool(diag, "log_errors", true);
    config->max_log_entries = get_int(diag, "max_log_entries", 10000);
    config->status_update_interval_ms = get_int(diag, "status_update_interval_ms", 500);
}

/**
 * @brief Parse a single PDO entry from JSON
 */
static int parse_pdo_entry(const cJSON *entry_json, ecat_pdo_entry_t *entry)
{
    if (entry_json == NULL || entry == NULL) {
        return ECAT_CONFIG_ERR_INVALID;
    }

    safe_strcpy(entry->index, get_string(entry_json, "index", "0x0000"), sizeof(entry->index));
    entry->subindex = (uint8_t)get_int(entry_json, "subindex", 0);
    entry->bit_length = (uint8_t)get_int(entry_json, "bit_length", 0);
    safe_strcpy(entry->name, get_string(entry_json, "name", ""), sizeof(entry->name));
    safe_strcpy(entry->data_type, get_string(entry_json, "data_type", ""), sizeof(entry->data_type));
    entry->parsed_type = ecat_parse_data_type(entry->data_type);

    return ECAT_CONFIG_OK;
}

/**
 * @brief Parse a single PDO from JSON
 */
static int parse_pdo(const cJSON *pdo_json, ecat_pdo_t *pdo)
{
    if (pdo_json == NULL || pdo == NULL) {
        return ECAT_CONFIG_ERR_INVALID;
    }

    safe_strcpy(pdo->index, get_string(pdo_json, "index", "0x0000"), sizeof(pdo->index));
    safe_strcpy(pdo->name, get_string(pdo_json, "name", ""), sizeof(pdo->name));

    pdo->entry_count = 0;
    const cJSON *entries = cJSON_GetObjectItemCaseSensitive(pdo_json, "entries");
    if (entries != NULL && cJSON_IsArray(entries)) {
        const cJSON *entry_json;
        cJSON_ArrayForEach(entry_json, entries) {
            if (pdo->entry_count >= ECAT_MAX_PDO_ENTRIES) {
                break;
            }
            if (parse_pdo_entry(entry_json, &pdo->entries[pdo->entry_count]) == ECAT_CONFIG_OK) {
                pdo->entry_count++;
            }
        }
    }

    return ECAT_CONFIG_OK;
}

/**
 * @brief Parse an array of PDOs (rx_pdos or tx_pdos) from JSON
 */
static int parse_pdo_array(const cJSON *pdo_array, ecat_pdo_t *pdos, int *pdo_count)
{
    *pdo_count = 0;

    if (pdo_array == NULL || !cJSON_IsArray(pdo_array)) {
        return ECAT_CONFIG_OK;
    }

    const cJSON *pdo_json;
    cJSON_ArrayForEach(pdo_json, pdo_array) {
        if (*pdo_count >= ECAT_MAX_PDOS) {
            break;
        }
        if (parse_pdo(pdo_json, &pdos[*pdo_count]) == ECAT_CONFIG_OK) {
            (*pdo_count)++;
        }
    }

    return ECAT_CONFIG_OK;
}

/**
 * @brief Parse a single SDO configuration from JSON
 */
static int parse_sdo(const cJSON *sdo_json, ecat_sdo_config_t *sdo)
{
    if (sdo_json == NULL || sdo == NULL) {
        return ECAT_CONFIG_ERR_INVALID;
    }

    safe_strcpy(sdo->index, get_string(sdo_json, "index", "0x0000"), sizeof(sdo->index));
    sdo->subindex = (uint8_t)get_int(sdo_json, "subindex", 0);
    sdo->value = get_numeric_value(sdo_json, "value", 0.0);
    safe_strcpy(sdo->data_type, get_string(sdo_json, "data_type", ""), sizeof(sdo->data_type));
    sdo->parsed_type = ecat_parse_data_type(sdo->data_type);
    safe_strcpy(sdo->name, get_string(sdo_json, "name", ""), sizeof(sdo->name));

    return ECAT_CONFIG_OK;
}

/**
 * @brief Parse a single channel from JSON
 */
static int parse_channel(const cJSON *ch_json, ecat_channel_t *channel)
{
    if (ch_json == NULL || channel == NULL) {
        return ECAT_CONFIG_ERR_INVALID;
    }

    channel->index = get_int(ch_json, "index", 0);
    safe_strcpy(channel->name, get_string(ch_json, "name", ""), sizeof(channel->name));
    safe_strcpy(channel->type, get_string(ch_json, "type", ""), sizeof(channel->type));
    channel->bit_length = (uint8_t)get_int(ch_json, "bit_length", 0);
    safe_strcpy(channel->iec_location, get_string(ch_json, "iec_location", ""), sizeof(channel->iec_location));
    safe_strcpy(channel->pdo_index, get_string(ch_json, "pdo_index", ""), sizeof(channel->pdo_index));
    safe_strcpy(channel->pdo_entry_index, get_string(ch_json, "pdo_entry_index", ""), sizeof(channel->pdo_entry_index));
    channel->pdo_entry_subindex = (uint8_t)get_int(ch_json, "pdo_entry_subindex", 0);

    return ECAT_CONFIG_OK;
}

/**
 * @brief Parse a single slave configuration from JSON
 */
static int parse_slave(const cJSON *slave_json, ecat_slave_t *slave)
{
    if (slave_json == NULL || slave == NULL) {
        return ECAT_CONFIG_ERR_INVALID;
    }

    memset(slave, 0, sizeof(ecat_slave_t));

    slave->position = get_int(slave_json, "position", 0);
    safe_strcpy(slave->name, get_string(slave_json, "name", ""), sizeof(slave->name));
    safe_strcpy(slave->type, get_string(slave_json, "type", "coupler"), sizeof(slave->type));

    /* Convert hex string vendor_id, product_code, revision to uint32_t */
    slave->vendor_id = hex_to_uint32(get_string(slave_json, "vendor_id", "0x0"));
    slave->product_code = hex_to_uint32(get_string(slave_json, "product_code", "0x0"));
    slave->revision = hex_to_uint32(get_string(slave_json, "revision", "0x0"));

    /* Parse channels */
    slave->channel_count = 0;
    const cJSON *channels = cJSON_GetObjectItemCaseSensitive(slave_json, "channels");
    if (channels != NULL && cJSON_IsArray(channels)) {
        const cJSON *ch_json;
        cJSON_ArrayForEach(ch_json, channels) {
            if (slave->channel_count >= ECAT_MAX_CHANNELS) {
                break;
            }
            if (parse_channel(ch_json, &slave->channels[slave->channel_count]) == ECAT_CONFIG_OK) {
                slave->channel_count++;
            }
        }
    }

    /* Parse SDO configurations */
    slave->sdo_count = 0;
    const cJSON *sdos = cJSON_GetObjectItemCaseSensitive(slave_json, "sdo_configurations");
    if (sdos != NULL && cJSON_IsArray(sdos)) {
        const cJSON *sdo_json;
        cJSON_ArrayForEach(sdo_json, sdos) {
            if (slave->sdo_count >= ECAT_MAX_SDOS) {
                break;
            }
            if (parse_sdo(sdo_json, &slave->sdo_configs[slave->sdo_count]) == ECAT_CONFIG_OK) {
                slave->sdo_count++;
            }
        }
    }

    /* Parse RxPDOs and TxPDOs */
    parse_pdo_array(cJSON_GetObjectItemCaseSensitive(slave_json, "rx_pdos"),
                    slave->rx_pdos, &slave->rx_pdo_count);
    parse_pdo_array(cJSON_GetObjectItemCaseSensitive(slave_json, "tx_pdos"),
                    slave->tx_pdos, &slave->tx_pdo_count);

    /* Parse per-slave configuration (defaults applied if "config" is absent) */
    slave->startup_checks.check_vendor_id = true;
    slave->startup_checks.check_product_code = true;
    slave->addressing.ethercat_address = 0;
    slave->timeouts.sdo_timeout_ms = 1000;
    slave->timeouts.init_to_preop_timeout_ms = 3000;
    slave->timeouts.safeop_to_op_timeout_ms = 10000;
    slave->watchdog.sm_watchdog_enabled = true;
    slave->watchdog.sm_watchdog_ms = 100;
    slave->watchdog.pdi_watchdog_enabled = false;
    slave->watchdog.pdi_watchdog_ms = 100;
    slave->dc.enabled = false;
    slave->dc.sync_unit_cycle_us = 0;
    slave->dc.sync0_enabled = false;
    slave->dc.sync0_cycle_us = 0;
    slave->dc.sync0_shift_us = 0;
    slave->dc.sync1_enabled = false;
    slave->dc.sync1_cycle_us = 0;
    slave->dc.sync1_shift_us = 0;

    const cJSON *cfg = cJSON_GetObjectItemCaseSensitive(slave_json, "config");
    if (cfg != NULL && cJSON_IsObject(cfg)) {
        /* Startup checks */
        const cJSON *sc = cJSON_GetObjectItemCaseSensitive(cfg, "startup_checks");
        if (sc != NULL && cJSON_IsObject(sc)) {
            slave->startup_checks.check_vendor_id = get_bool(sc, "check_vendor_id", true);
            slave->startup_checks.check_product_code = get_bool(sc, "check_product_code", true);
        }

        /* Addressing */
        const cJSON *addr = cJSON_GetObjectItemCaseSensitive(cfg, "addressing");
        if (addr != NULL && cJSON_IsObject(addr)) {
            slave->addressing.ethercat_address = (uint16_t)get_int(addr, "ethercat_address", 0);
        }

        /* Timeouts (negative values fall back to defaults) */
        const cJSON *to = cJSON_GetObjectItemCaseSensitive(cfg, "timeouts");
        if (to != NULL && cJSON_IsObject(to)) {
            int val;
            val = get_int(to, "sdo_timeout_ms", 1000);
            if (val > 0) slave->timeouts.sdo_timeout_ms = val;
            val = get_int(to, "init_to_preop_timeout_ms", 3000);
            if (val > 0) slave->timeouts.init_to_preop_timeout_ms = val;
            val = get_int(to, "safeop_to_op_timeout_ms", 10000);
            if (val > 0) slave->timeouts.safeop_to_op_timeout_ms = val;
        }

        /* Watchdog (negative ms values fall back to defaults) */
        const cJSON *wd = cJSON_GetObjectItemCaseSensitive(cfg, "watchdog");
        if (wd != NULL && cJSON_IsObject(wd)) {
            int val;
            slave->watchdog.sm_watchdog_enabled = get_bool(wd, "sm_watchdog_enabled", true);
            val = get_int(wd, "sm_watchdog_ms", 100);
            if (val > 0) slave->watchdog.sm_watchdog_ms = val;
            slave->watchdog.pdi_watchdog_enabled = get_bool(wd, "pdi_watchdog_enabled", false);
            val = get_int(wd, "pdi_watchdog_ms", 100);
            if (val > 0) slave->watchdog.pdi_watchdog_ms = val;
        }

        /* Distributed Clocks */
        const cJSON *dc = cJSON_GetObjectItemCaseSensitive(cfg, "distributed_clocks");
        if (dc != NULL && cJSON_IsObject(dc)) {
            slave->dc.enabled = get_bool(dc, "enabled", false);
            slave->dc.sync_unit_cycle_us = get_int(dc, "sync_unit_cycle_us", 0);
            slave->dc.sync0_enabled = get_bool(dc, "sync0_enabled", false);
            slave->dc.sync0_cycle_us = get_int(dc, "sync0_cycle_us", 0);
            slave->dc.sync0_shift_us = get_int(dc, "sync0_shift_us", 0);
            slave->dc.sync1_enabled = get_bool(dc, "sync1_enabled", false);
            slave->dc.sync1_cycle_us = get_int(dc, "sync1_cycle_us", 0);
            slave->dc.sync1_shift_us = get_int(dc, "sync1_shift_us", 0);
        }
    }

    return ECAT_CONFIG_OK;
}

/**
 * @brief Parse the slaves array from JSON
 */
static int parse_slaves_section(const cJSON *slaves, ecat_config_t *config)
{
    config->slave_count = 0;

    if (slaves == NULL || !cJSON_IsArray(slaves)) {
        return ECAT_CONFIG_OK;
    }

    const cJSON *slave_json;
    cJSON_ArrayForEach(slave_json, slaves) {
        if (config->slave_count >= ECAT_MAX_SLAVES) {
            break;
        }
        if (parse_slave(slave_json, &config->slaves[config->slave_count]) == ECAT_CONFIG_OK) {
            config->slave_count++;
        }
    }

    return ECAT_CONFIG_OK;
}

/*
 * =============================================================================
 * Public API
 * =============================================================================
 */

void ecat_config_init_defaults(ecat_config_t *config)
{
    if (config == NULL) {
        return;
    }

    memset(config, 0, sizeof(ecat_config_t));

    /* Master defaults */
    safe_strcpy(config->master.interface, "eth0", sizeof(config->master.interface));
    config->master.cycle_time_us = 1000;
    config->master.receive_timeout_us = 2000;
    config->master.watchdog_timeout_cycles = 3;
    safe_strcpy(config->master.log_level, "info", sizeof(config->master.log_level));
    config->master.task_name[0] = '\0';
    config->master.task_cycle_time_us = 0;

    /* Diagnostics defaults */
    config->diagnostics.log_connections = true;
    config->diagnostics.log_data_access = false;
    config->diagnostics.log_errors = true;
    config->diagnostics.max_log_entries = 10000;
    config->diagnostics.status_update_interval_ms = 500;
}

int ecat_config_parse(const char *config_path, ecat_config_t *config)
{
    if (config_path == NULL || config == NULL) {
        return ECAT_CONFIG_ERR_INVALID;
    }

    /* Initialize with defaults */
    ecat_config_init_defaults(config);

    /* Read file contents */
    char *json_str = read_file(config_path);
    if (json_str == NULL) {
        return ECAT_CONFIG_ERR_FILE;
    }

    /* Parse JSON */
    cJSON *root = cJSON_Parse(json_str);
    free(json_str);

    if (root == NULL) {
        return ECAT_CONFIG_ERR_PARSE;
    }

    /*
     * The JSON has array root format: [{ name, protocol, config }]
     * Extract the "config" object from the first element.
     */
    const cJSON *config_obj = NULL;

    if (cJSON_IsArray(root)) {
        const cJSON *first_entry = cJSON_GetArrayItem(root, 0);
        if (first_entry != NULL) {
            config_obj = cJSON_GetObjectItemCaseSensitive(first_entry, "config");
        }
    } else if (cJSON_IsObject(root)) {
        /* Also support a bare config object for flexibility */
        config_obj = cJSON_GetObjectItemCaseSensitive(root, "config");
        if (config_obj == NULL) {
            /* The root itself might be the config */
            config_obj = root;
        }
    }

    if (config_obj == NULL) {
        cJSON_Delete(root);
        return ECAT_CONFIG_ERR_PARSE;
    }

    /* Parse each section */
    parse_master_section(cJSON_GetObjectItemCaseSensitive(config_obj, "master"), &config->master);
    parse_slaves_section(cJSON_GetObjectItemCaseSensitive(config_obj, "slaves"), config);
    parse_diagnostics_section(cJSON_GetObjectItemCaseSensitive(config_obj, "diagnostics"), &config->diagnostics);

    cJSON_Delete(root);

    /* Validate the parsed configuration */
    return ecat_config_validate(config);
}

int ecat_config_parse_all(const char *config_path,
                          ecat_master_instance_t *instances,
                          int max_masters,
                          int *out_count)
{
    if (config_path == NULL || instances == NULL || out_count == NULL || max_masters < 1) {
        return ECAT_CONFIG_ERR_INVALID;
    }

    *out_count = 0;

    /* Read file contents */
    char *json_str = read_file(config_path);
    if (json_str == NULL) {
        return ECAT_CONFIG_ERR_FILE;
    }

    /* Parse JSON */
    cJSON *root = cJSON_Parse(json_str);
    free(json_str);

    if (root == NULL) {
        return ECAT_CONFIG_ERR_PARSE;
    }

    if (!cJSON_IsArray(root)) {
        /* Fall back to single-entry parse for bare config objects */
        ecat_config_init_defaults(&instances[0].config);
        const cJSON *config_obj = cJSON_GetObjectItemCaseSensitive(root, "config");
        if (config_obj == NULL) {
            config_obj = root;
        }
        const char *name = get_string(root, "name", "master");
        safe_strcpy(instances[0].name, name, sizeof(instances[0].name));
        parse_master_section(cJSON_GetObjectItemCaseSensitive(config_obj, "master"),
                             &instances[0].config.master);
        parse_slaves_section(cJSON_GetObjectItemCaseSensitive(config_obj, "slaves"),
                             &instances[0].config);
        parse_diagnostics_section(cJSON_GetObjectItemCaseSensitive(config_obj, "diagnostics"),
                                  &instances[0].config.diagnostics);
        cJSON_Delete(root);
        int result = ecat_config_validate(&instances[0].config);
        if (result == ECAT_CONFIG_OK) {
            *out_count = 1;
        }
        return result;
    }

    /* Iterate all entries in the array */
    int count = 0;
    int array_size = cJSON_GetArraySize(root);

    for (int i = 0; i < array_size && count < max_masters; i++) {
        const cJSON *entry = cJSON_GetArrayItem(root, i);
        if (entry == NULL) continue;

        /* Check protocol is ETHERCAT (case-insensitive) */
        const char *protocol = get_string(entry, "protocol", "");
        if (strcasecmp_local(protocol, "ETHERCAT") != 0) continue;

        const cJSON *config_obj = cJSON_GetObjectItemCaseSensitive(entry, "config");
        if (config_obj == NULL) continue;

        /* Initialize this instance's config with defaults */
        ecat_config_init_defaults(&instances[count].config);

        /* Extract master name */
        const char *name = get_string(entry, "name", "master");
        safe_strcpy(instances[count].name, name, sizeof(instances[count].name));

        /* Parse configuration sections */
        parse_master_section(cJSON_GetObjectItemCaseSensitive(config_obj, "master"),
                             &instances[count].config.master);
        parse_slaves_section(cJSON_GetObjectItemCaseSensitive(config_obj, "slaves"),
                             &instances[count].config);
        parse_diagnostics_section(cJSON_GetObjectItemCaseSensitive(config_obj, "diagnostics"),
                                  &instances[count].config.diagnostics);

        /* Validate this master's config */
        int result = ecat_config_validate(&instances[count].config);
        if (result == ECAT_CONFIG_OK) {
            count++;
        } else {
            fprintf(stderr, "ETHERCAT CONFIG: skipping entry[%d] '%s' "
                    "(validation failed, error=%d)\n", i, name, result);
        }
    }

    cJSON_Delete(root);
    *out_count = count;

    return (count > 0) ? ECAT_CONFIG_OK : ECAT_CONFIG_ERR_MISSING;
}

int ecat_config_validate(const ecat_config_t *config)
{
    if (config == NULL) {
        return ECAT_CONFIG_ERR_INVALID;
    }

    /* Validate master interface is not empty */
    if (config->master.interface[0] == '\0') {
        return ECAT_CONFIG_ERR_INVALID;
    }

    /* Validate cycle time */
    if (config->master.cycle_time_us < 1) {
        return ECAT_CONFIG_ERR_INVALID;
    }

    /* Validate receive timeout */
    if (config->master.receive_timeout_us < 1) {
        return ECAT_CONFIG_ERR_INVALID;
    }

    /* Validate slave positions are positive and unique */
    for (int i = 0; i < config->slave_count; i++) {
        const ecat_slave_t *slave = &config->slaves[i];

        if (slave->position < 1) {
            return ECAT_CONFIG_ERR_INVALID;
        }

        if (slave->vendor_id == 0) {
            return ECAT_CONFIG_ERR_INVALID;
        }

        if (slave->product_code == 0) {
            return ECAT_CONFIG_ERR_INVALID;
        }

        /* Check for duplicate positions */
        for (int j = i + 1; j < config->slave_count; j++) {
            if (slave->position == config->slaves[j].position) {
                return ECAT_CONFIG_ERR_INVALID;
            }
        }

        /* Validate channels have IEC location */
        for (int c = 0; c < slave->channel_count; c++) {
            const ecat_channel_t *ch = &slave->channels[c];
            if (ch->iec_location[0] != '\0' && ch->iec_location[0] != '%') {
                return ECAT_CONFIG_ERR_INVALID;
            }
        }
    }

    return ECAT_CONFIG_OK;
}

/*
 * =============================================================================
 * State Machine and Data Type Helpers
 * =============================================================================
 */

const char *ecat_state_to_string(ecat_plugin_state_t state)
{
    switch (state) {
    case ECAT_STATE_IDLE:          return "IDLE";
    case ECAT_STATE_SCANNING:      return "SCANNING";
    case ECAT_STATE_CONFIGURING:   return "CONFIGURING";
    case ECAT_STATE_TRANSITIONING: return "TRANSITIONING";
    case ECAT_STATE_OPERATIONAL:   return "OPERATIONAL";
    case ECAT_STATE_RECOVERING:    return "RECOVERING";
    case ECAT_STATE_ERROR:         return "ERROR";
    case ECAT_STATE_STOPPED:       return "STOPPED";
    }
    return "UNKNOWN";
}

int ecat_data_type_size(ecat_data_type_t dt)
{
    switch (dt) {
    case ECAT_DTYPE_BOOL:    return 1;
    case ECAT_DTYPE_INT8:    return 1;
    case ECAT_DTYPE_UINT8:   return 1;
    case ECAT_DTYPE_INT16:   return 2;
    case ECAT_DTYPE_UINT16:  return 2;
    case ECAT_DTYPE_INT32:   return 4;
    case ECAT_DTYPE_UINT32:  return 4;
    case ECAT_DTYPE_INT64:   return 8;
    case ECAT_DTYPE_UINT64:  return 8;
    case ECAT_DTYPE_REAL32:  return 4;
    case ECAT_DTYPE_REAL64:  return 8;
    case ECAT_DTYPE_UNKNOWN: return 0;
    case ECAT_DTYPE_PAD:     return 0;
    }
    return 0;
}
