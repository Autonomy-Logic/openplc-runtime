// SPDX-License-Identifier: LGPL-3.0-or-later
// Copyright (c) 2026 Autonomy®

/**
 * @file s7comm_config.h
 * @brief S7Comm Plugin Configuration Structures and Parser
 *
 * Defines data structures for JSON configuration and functions
 * for parsing, validating, and managing configuration.
 */

#ifndef S7COMM_CONFIG_H
#define S7COMM_CONFIG_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Configuration limits */
#define S7COMM_MAX_DATA_BLOCKS     64
#define S7COMM_MAX_STRING_LEN      64
#define S7COMM_MAX_DESCRIPTION_LEN 128

/* Default values */
#define S7COMM_DEFAULT_PORT           102
#define S7COMM_DEFAULT_MAX_CLIENTS    32
#define S7COMM_DEFAULT_WORK_INTERVAL  100
#define S7COMM_DEFAULT_SEND_TIMEOUT   3000
#define S7COMM_DEFAULT_RECV_TIMEOUT   3000
#define S7COMM_DEFAULT_PING_TIMEOUT   10000
#define S7COMM_DEFAULT_PDU_SIZE       480

/**
 * @brief Buffer type enumeration for mapping S7 areas to OpenPLC buffers
 */
typedef enum {
    BUFFER_TYPE_NONE = 0,
    BUFFER_TYPE_BOOL_INPUT,
    BUFFER_TYPE_BOOL_OUTPUT,
    BUFFER_TYPE_BOOL_MEMORY,
    BUFFER_TYPE_BYTE_INPUT,
    BUFFER_TYPE_BYTE_OUTPUT,
    BUFFER_TYPE_INT_INPUT,
    BUFFER_TYPE_INT_OUTPUT,
    BUFFER_TYPE_INT_MEMORY,
    BUFFER_TYPE_DINT_INPUT,
    BUFFER_TYPE_DINT_OUTPUT,
    BUFFER_TYPE_DINT_MEMORY,
    BUFFER_TYPE_LINT_INPUT,
    BUFFER_TYPE_LINT_OUTPUT,
    BUFFER_TYPE_LINT_MEMORY
} s7comm_buffer_type_t;

/**
 * @brief Buffer mapping configuration
 */
typedef struct {
    s7comm_buffer_type_t type;      /* OpenPLC buffer type */
    int start_buffer;               /* Starting buffer index */
    bool bit_addressing;            /* Enable bit-level access */
} s7comm_buffer_mapping_t;

/**
 * @brief Data block configuration
 */
typedef struct {
    int db_number;                                  /* S7 DB number (1-65535) */
    char description[S7COMM_MAX_DESCRIPTION_LEN];   /* Human-readable description */
    int size_bytes;                                 /* DB size in bytes */
    s7comm_buffer_mapping_t mapping;                /* Buffer mapping */
} s7comm_data_block_t;

/**
 * @brief System area configuration (PE, PA, MK)
 */
typedef struct {
    bool enabled;                       /* Enable this area */
    int size_bytes;                     /* Area size in bytes */
    s7comm_buffer_mapping_t mapping;    /* Buffer mapping */
} s7comm_system_area_t;

/**
 * @brief PLC identity configuration for SZL responses
 */
typedef struct {
    char name[S7COMM_MAX_STRING_LEN];           /* PLC name */
    char module_type[S7COMM_MAX_STRING_LEN];    /* CPU type string */
    char serial_number[S7COMM_MAX_STRING_LEN];  /* Serial number */
    char copyright[S7COMM_MAX_STRING_LEN];      /* Copyright string */
    char module_name[S7COMM_MAX_STRING_LEN];    /* Module name */
} s7comm_plc_identity_t;

/**
 * @brief Logging configuration
 */
typedef struct {
    bool log_connections;   /* Log client connect/disconnect */
    bool log_data_access;   /* Log read/write operations */
    bool log_errors;        /* Log errors and warnings */
} s7comm_logging_t;

/**
 * @brief Complete S7Comm configuration
 */
typedef struct {
    /* Server settings */
    bool enabled;                                   /* Enable/disable the S7 server */
    char bind_address[S7COMM_MAX_STRING_LEN];       /* Network interface to bind */
    uint16_t port;                                  /* S7Comm TCP port */
    int max_clients;                                /* Maximum simultaneous connections */
    int work_interval_ms;                           /* Worker thread polling interval */
    int send_timeout_ms;                            /* Socket send timeout */
    int recv_timeout_ms;                            /* Socket receive timeout */
    int ping_timeout_ms;                            /* Keep-alive timeout */
    int pdu_size;                                   /* Maximum PDU size */

    /* PLC identity */
    s7comm_plc_identity_t identity;

    /* Data blocks */
    int num_data_blocks;
    s7comm_data_block_t data_blocks[S7COMM_MAX_DATA_BLOCKS];

    /* System areas */
    s7comm_system_area_t pe_area;   /* Process inputs (I area) */
    s7comm_system_area_t pa_area;   /* Process outputs (Q area) */
    s7comm_system_area_t mk_area;   /* Markers (M area) */

    /* Logging */
    s7comm_logging_t logging;
} s7comm_config_t;

/**
 * @brief Parse configuration from JSON file
 *
 * @param config_path Path to the JSON configuration file
 * @param config Pointer to configuration structure to populate
 * @return 0 on success, negative error code on failure
 */
int s7comm_config_parse(const char *config_path, s7comm_config_t *config);

/**
 * @brief Validate configuration values
 *
 * @param config Pointer to configuration structure to validate
 * @return 0 if valid, negative error code indicating the issue
 */
int s7comm_config_validate(const s7comm_config_t *config);

/**
 * @brief Initialize configuration with default values
 *
 * @param config Pointer to configuration structure to initialize
 */
void s7comm_config_init_defaults(s7comm_config_t *config);

/**
 * @brief Get human-readable name for a buffer type
 *
 * @param type Buffer type enumeration value
 * @return String name of the buffer type
 */
const char *s7comm_buffer_type_name(s7comm_buffer_type_t type);

/**
 * @brief Get element size in bytes for a buffer type
 *
 * @param type Buffer type enumeration value
 * @return Size in bytes (1, 2, 4, or 8), or 0 for invalid type
 */
int s7comm_buffer_type_size(s7comm_buffer_type_t type);

#ifdef __cplusplus
}
#endif

#endif /* S7COMM_CONFIG_H */
