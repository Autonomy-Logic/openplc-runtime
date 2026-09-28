// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file s7comm_config.c
 * @brief S7Comm Plugin Configuration Parser Implementation
 *
 * Parses JSON configuration files using cJSON library.
 */

#include "s7comm_config.h"
#include "cJSON.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Error codes */
#define S7COMM_CONFIG_OK              0
#define S7COMM_CONFIG_ERR_FILE       -1
#define S7COMM_CONFIG_ERR_PARSE      -2
#define S7COMM_CONFIG_ERR_MEMORY     -3
#define S7COMM_CONFIG_ERR_INVALID    -4
#define S7COMM_CONFIG_ERR_MISSING    -5

/* Buffer type string mappings */
static const struct {
    const char *name;
    s7comm_buffer_type_t type;
} buffer_type_map[] = {
    {"bool_input",   BUFFER_TYPE_BOOL_INPUT},
    {"bool_output",  BUFFER_TYPE_BOOL_OUTPUT},
    {"bool_memory",  BUFFER_TYPE_BOOL_MEMORY},
    {"byte_input",   BUFFER_TYPE_BYTE_INPUT},
    {"byte_output",  BUFFER_TYPE_BYTE_OUTPUT},
    {"int_input",    BUFFER_TYPE_INT_INPUT},
    {"int_output",   BUFFER_TYPE_INT_OUTPUT},
    {"int_memory",   BUFFER_TYPE_INT_MEMORY},
    {"dint_input",   BUFFER_TYPE_DINT_INPUT},
    {"dint_output",  BUFFER_TYPE_DINT_OUTPUT},
    {"dint_memory",  BUFFER_TYPE_DINT_MEMORY},
    {"lint_input",   BUFFER_TYPE_LINT_INPUT},
    {"lint_output",  BUFFER_TYPE_LINT_OUTPUT},
    {"lint_memory",  BUFFER_TYPE_LINT_MEMORY},
    {NULL, BUFFER_TYPE_NONE}
};

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
 * @brief Parse buffer type from string
 */
static s7comm_buffer_type_t parse_buffer_type(const char *type_str)
{
    if (type_str == NULL) {
        return BUFFER_TYPE_NONE;
    }

    for (int i = 0; buffer_type_map[i].name != NULL; i++) {
        if (strcmp(type_str, buffer_type_map[i].name) == 0) {
            return buffer_type_map[i].type;
        }
    }

    return BUFFER_TYPE_NONE;
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
 * @brief Parse buffer mapping from JSON object
 */
static void parse_buffer_mapping(const cJSON *obj, s7comm_buffer_mapping_t *mapping)
{
    if (obj == NULL || mapping == NULL) {
        return;
    }

    const char *type_str = get_string(obj, "type", NULL);
    mapping->type = parse_buffer_type(type_str);
    mapping->start_buffer = get_int(obj, "start_buffer", 0);
    mapping->bit_addressing = get_bool(obj, "bit_addressing", false);
}

/**
 * @brief Parse server section from JSON
 */
static void parse_server_section(const cJSON *server, s7comm_config_t *config)
{
    if (server == NULL) {
        return;
    }

    config->enabled = get_bool(server, "enabled", true);
    safe_strcpy(config->bind_address, get_string(server, "bind_address", "0.0.0.0"),
                S7COMM_MAX_STRING_LEN);
    config->port = (uint16_t)get_int(server, "port", S7COMM_DEFAULT_PORT);
    config->max_clients = get_int(server, "max_clients", S7COMM_DEFAULT_MAX_CLIENTS);
    config->work_interval_ms = get_int(server, "work_interval_ms", S7COMM_DEFAULT_WORK_INTERVAL);
    config->send_timeout_ms = get_int(server, "send_timeout_ms", S7COMM_DEFAULT_SEND_TIMEOUT);
    config->recv_timeout_ms = get_int(server, "recv_timeout_ms", S7COMM_DEFAULT_RECV_TIMEOUT);
    config->ping_timeout_ms = get_int(server, "ping_timeout_ms", S7COMM_DEFAULT_PING_TIMEOUT);
    config->pdu_size = get_int(server, "pdu_size", S7COMM_DEFAULT_PDU_SIZE);
}

/**
 * @brief Parse PLC identity section from JSON
 */
static void parse_identity_section(const cJSON *identity, s7comm_plc_identity_t *id)
{
    if (identity == NULL) {
        return;
    }

    safe_strcpy(id->name, get_string(identity, "name", "OpenPLC Runtime"),
                S7COMM_MAX_STRING_LEN);
    safe_strcpy(id->module_type, get_string(identity, "module_type", "CPU 315-2 PN/DP"),
                S7COMM_MAX_STRING_LEN);
    safe_strcpy(id->serial_number, get_string(identity, "serial_number", "S C-XXXXXXXXX"),
                S7COMM_MAX_STRING_LEN);
    safe_strcpy(id->copyright, get_string(identity, "copyright", "OpenPLC Project"),
                S7COMM_MAX_STRING_LEN);
    safe_strcpy(id->module_name, get_string(identity, "module_name", "OpenPLC"),
                S7COMM_MAX_STRING_LEN);
}

/**
 * @brief Parse a single data block from JSON
 */
static int parse_data_block(const cJSON *db_json, s7comm_data_block_t *db)
{
    if (db_json == NULL || db == NULL) {
        return S7COMM_CONFIG_ERR_INVALID;
    }

    db->db_number = get_int(db_json, "db_number", 0);
    if (db->db_number <= 0 || db->db_number > 65535) {
        return S7COMM_CONFIG_ERR_INVALID;
    }

    safe_strcpy(db->description, get_string(db_json, "description", ""),
                S7COMM_MAX_DESCRIPTION_LEN);
    db->size_bytes = get_int(db_json, "size_bytes", 0);
    if (db->size_bytes <= 0) {
        return S7COMM_CONFIG_ERR_INVALID;
    }

    const cJSON *mapping = cJSON_GetObjectItemCaseSensitive(db_json, "mapping");
    if (mapping != NULL) {
        parse_buffer_mapping(mapping, &db->mapping);
    }

    return S7COMM_CONFIG_OK;
}

/**
 * @brief Parse data_blocks array from JSON
 */
static int parse_data_blocks_section(const cJSON *data_blocks, s7comm_config_t *config)
{
    if (data_blocks == NULL || !cJSON_IsArray(data_blocks)) {
        config->num_data_blocks = 0;
        return S7COMM_CONFIG_OK;
    }

    int count = cJSON_GetArraySize(data_blocks);
    if (count > S7COMM_MAX_DATA_BLOCKS) {
        count = S7COMM_MAX_DATA_BLOCKS;
    }

    config->num_data_blocks = 0;
    const cJSON *db_json;
    cJSON_ArrayForEach(db_json, data_blocks) {
        if (config->num_data_blocks >= S7COMM_MAX_DATA_BLOCKS) {
            break;
        }

        int result = parse_data_block(db_json, &config->data_blocks[config->num_data_blocks]);
        if (result == S7COMM_CONFIG_OK) {
            config->num_data_blocks++;
        }
    }

    return S7COMM_CONFIG_OK;
}

/**
 * @brief Parse a system area (PE, PA, MK) from JSON
 */
static void parse_system_area(const cJSON *area_json, s7comm_system_area_t *area)
{
    if (area_json == NULL) {
        area->enabled = false;
        return;
    }

    area->enabled = get_bool(area_json, "enabled", false);
    area->size_bytes = get_int(area_json, "size_bytes", 128);

    const cJSON *mapping = cJSON_GetObjectItemCaseSensitive(area_json, "mapping");
    if (mapping != NULL) {
        parse_buffer_mapping(mapping, &area->mapping);
    }
}

/**
 * @brief Parse system_areas section from JSON
 */
static void parse_system_areas_section(const cJSON *system_areas, s7comm_config_t *config)
{
    if (system_areas == NULL) {
        return;
    }

    parse_system_area(cJSON_GetObjectItemCaseSensitive(system_areas, "pe_area"),
                      &config->pe_area);
    parse_system_area(cJSON_GetObjectItemCaseSensitive(system_areas, "pa_area"),
                      &config->pa_area);
    parse_system_area(cJSON_GetObjectItemCaseSensitive(system_areas, "mk_area"),
                      &config->mk_area);
}

/**
 * @brief Parse logging section from JSON
 */
static void parse_logging_section(const cJSON *logging, s7comm_logging_t *log_config)
{
    if (logging == NULL) {
        return;
    }

    log_config->log_connections = get_bool(logging, "log_connections", true);
    log_config->log_data_access = get_bool(logging, "log_data_access", false);
    log_config->log_errors = get_bool(logging, "log_errors", true);
}

void s7comm_config_init_defaults(s7comm_config_t *config)
{
    if (config == NULL) {
        return;
    }

    memset(config, 0, sizeof(s7comm_config_t));

    /* Server defaults */
    config->enabled = true;
    safe_strcpy(config->bind_address, "0.0.0.0", S7COMM_MAX_STRING_LEN);
    config->port = S7COMM_DEFAULT_PORT;
    config->max_clients = S7COMM_DEFAULT_MAX_CLIENTS;
    config->work_interval_ms = S7COMM_DEFAULT_WORK_INTERVAL;
    config->send_timeout_ms = S7COMM_DEFAULT_SEND_TIMEOUT;
    config->recv_timeout_ms = S7COMM_DEFAULT_RECV_TIMEOUT;
    config->ping_timeout_ms = S7COMM_DEFAULT_PING_TIMEOUT;
    config->pdu_size = S7COMM_DEFAULT_PDU_SIZE;

    /* Identity defaults */
    safe_strcpy(config->identity.name, "OpenPLC Runtime", S7COMM_MAX_STRING_LEN);
    safe_strcpy(config->identity.module_type, "CPU 315-2 PN/DP", S7COMM_MAX_STRING_LEN);
    safe_strcpy(config->identity.serial_number, "S C-XXXXXXXXX", S7COMM_MAX_STRING_LEN);
    safe_strcpy(config->identity.copyright, "OpenPLC Project", S7COMM_MAX_STRING_LEN);
    safe_strcpy(config->identity.module_name, "OpenPLC", S7COMM_MAX_STRING_LEN);

    /* Logging defaults */
    config->logging.log_connections = true;
    config->logging.log_data_access = false;
    config->logging.log_errors = true;
}

int s7comm_config_parse(const char *config_path, s7comm_config_t *config)
{
    if (config_path == NULL || config == NULL) {
        return S7COMM_CONFIG_ERR_INVALID;
    }

    /* Initialize with defaults */
    s7comm_config_init_defaults(config);

    /* Read file contents */
    char *json_str = read_file(config_path);
    if (json_str == NULL) {
        return S7COMM_CONFIG_ERR_FILE;
    }

    /* Parse JSON */
    cJSON *root = cJSON_Parse(json_str);
    free(json_str);

    if (root == NULL) {
        return S7COMM_CONFIG_ERR_PARSE;
    }

    /* Parse each section */
    parse_server_section(cJSON_GetObjectItemCaseSensitive(root, "server"), config);
    parse_identity_section(cJSON_GetObjectItemCaseSensitive(root, "plc_identity"), &config->identity);
    parse_data_blocks_section(cJSON_GetObjectItemCaseSensitive(root, "data_blocks"), config);
    parse_system_areas_section(cJSON_GetObjectItemCaseSensitive(root, "system_areas"), config);
    parse_logging_section(cJSON_GetObjectItemCaseSensitive(root, "logging"), &config->logging);

    cJSON_Delete(root);

    /* Validate the parsed configuration */
    return s7comm_config_validate(config);
}

int s7comm_config_validate(const s7comm_config_t *config)
{
    if (config == NULL) {
        return S7COMM_CONFIG_ERR_INVALID;
    }

    /* Validate port */
    if (config->port == 0) {
        return S7COMM_CONFIG_ERR_INVALID;
    }

    /* Validate timeouts */
    if (config->send_timeout_ms < 100 || config->recv_timeout_ms < 100) {
        return S7COMM_CONFIG_ERR_INVALID;
    }

    /* Validate PDU size (S7 spec: 240-960) */
    if (config->pdu_size < 240 || config->pdu_size > 960) {
        return S7COMM_CONFIG_ERR_INVALID;
    }

    /* Validate max_clients */
    if (config->max_clients < 1 || config->max_clients > 1024) {
        return S7COMM_CONFIG_ERR_INVALID;
    }

    /* Check for duplicate DB numbers */
    for (int i = 0; i < config->num_data_blocks; i++) {
        for (int j = i + 1; j < config->num_data_blocks; j++) {
            if (config->data_blocks[i].db_number == config->data_blocks[j].db_number) {
                return S7COMM_CONFIG_ERR_INVALID;
            }
        }
    }

    /* Validate data block mappings */
    for (int i = 0; i < config->num_data_blocks; i++) {
        const s7comm_data_block_t *db = &config->data_blocks[i];

        if (db->size_bytes <= 0 || db->size_bytes > 65535) {
            return S7COMM_CONFIG_ERR_INVALID;
        }

        if (db->mapping.type == BUFFER_TYPE_NONE) {
            return S7COMM_CONFIG_ERR_INVALID;
        }

        if (db->mapping.start_buffer < 0) {
            return S7COMM_CONFIG_ERR_INVALID;
        }
    }

    return S7COMM_CONFIG_OK;
}

const char *s7comm_buffer_type_name(s7comm_buffer_type_t type)
{
    for (int i = 0; buffer_type_map[i].name != NULL; i++) {
        if (buffer_type_map[i].type == type) {
            return buffer_type_map[i].name;
        }
    }
    return "none";
}

int s7comm_buffer_type_size(s7comm_buffer_type_t type)
{
    switch (type) {
        case BUFFER_TYPE_BOOL_INPUT:
        case BUFFER_TYPE_BOOL_OUTPUT:
        case BUFFER_TYPE_BOOL_MEMORY:
        case BUFFER_TYPE_BYTE_INPUT:
        case BUFFER_TYPE_BYTE_OUTPUT:
            return 1;

        case BUFFER_TYPE_INT_INPUT:
        case BUFFER_TYPE_INT_OUTPUT:
        case BUFFER_TYPE_INT_MEMORY:
            return 2;

        case BUFFER_TYPE_DINT_INPUT:
        case BUFFER_TYPE_DINT_OUTPUT:
        case BUFFER_TYPE_DINT_MEMORY:
            return 4;

        case BUFFER_TYPE_LINT_INPUT:
        case BUFFER_TYPE_LINT_OUTPUT:
        case BUFFER_TYPE_LINT_MEMORY:
            return 8;

        default:
            return 0;
    }
}
