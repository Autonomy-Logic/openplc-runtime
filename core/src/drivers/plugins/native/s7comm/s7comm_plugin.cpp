/**
 * @file s7comm_plugin.cpp
 * @brief S7Comm Plugin Implementation for OpenPLC Runtime v4
 *
 * This plugin implements a Siemens S7 communication server using the Snap7 library.
 * It allows S7-compatible HMIs and SCADA systems to read/write OpenPLC I/O buffers.
 *
 * Buffer Sync Strategy (using journal buffer system):
 * - S7 buffers: What Snap7 clients read/write (managed by Snap7 server)
 * - Journal writes: Race-condition-free writes to OpenPLC buffers
 *
 * Data flow (on-demand via Snap7 RWArea callback):
 * - S7 client READ: Callback acquires OpenPLC mutex, copies fresh data to S7 buffer
 * - S7 client WRITE: Callback uses journal writes (thread-safe, no mutex needed)
 *
 * This approach allows the S7 server thread to run independently without
 * requiring cycle_start/cycle_end hooks for data synchronization.
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <pthread.h>

/* Snap7 includes */
#include "snap7_libmain.h"
#include "s7_types.h"
#include "s7_server.h"  /* For OperationRead, OperationWrite, TS7Tag */

/* Plugin includes */
extern "C" {
#include "plugin_logger.h"
#include "plugin_types.h"
#include "s7comm_plugin.h"
#include "s7comm_config.h"
}

/*
 * =============================================================================
 * Constants
 * =============================================================================
 */
#define S7COMM_MAX_DB_SIZE  65536   /* Maximum size for a single DB buffer */

/*
 * =============================================================================
 * Data Block Runtime Structure
 * =============================================================================
 */
typedef struct {
    int db_number;                  /* S7 DB number */
    s7comm_buffer_type_t type;      /* Mapping type */
    int start_buffer;               /* Starting buffer index */
    int size_bytes;                 /* Size in bytes */
    bool bit_addressing;            /* Bit-level access enabled */
    uint8_t *s7_buffer;             /* S7 buffer (registered with Snap7) */
} s7comm_db_runtime_t;

/*
 * =============================================================================
 * System Area Runtime Structure
 * =============================================================================
 */
typedef struct {
    bool enabled;
    int size_bytes;
    s7comm_buffer_type_t type;
    int start_buffer;
    uint8_t *s7_buffer;             /* S7 buffer (registered with Snap7) */
} s7comm_area_runtime_t;

/*
 * =============================================================================
 * Plugin State
 * =============================================================================
 */
static plugin_logger_t g_logger;
static plugin_runtime_args_t g_runtime_args;
static s7comm_config_t g_config;
static bool g_initialized = false;
static bool g_running = false;
static bool g_config_loaded = false;

/* Snap7 server handle (S7Object is uintptr_t, use 0 for null) */
static S7Object g_server = 0;

/* No S7 buffer mutex needed - reads use OpenPLC mutex, writes use journal */

/* Runtime data blocks (dynamically allocated based on config) */
static s7comm_db_runtime_t g_db_runtime[S7COMM_MAX_DATA_BLOCKS];
static int g_num_db_runtime = 0;

/* System area runtime */
static s7comm_area_runtime_t g_pe_runtime;
static s7comm_area_runtime_t g_pa_runtime;
static s7comm_area_runtime_t g_mk_runtime;

/*
 * =============================================================================
 * Forward Declarations
 * =============================================================================
 */
static void s7comm_event_callback(void *usrPtr, PSrvEvent PEvent, int Size);
static int s7comm_rw_area_callback(void *usrPtr, int Sender, int Operation, PS7Tag PTag, void *pUsrData);
static int allocate_buffers(void);
static void free_buffers(void);
static int register_all_areas(void);
static void read_openplc_to_buffer(uint8_t *dest, int size, s7comm_buffer_type_t type, int start_buffer);
static void write_buffer_to_openplc_journal(uint8_t *src, int size, s7comm_buffer_type_t type, int start_buffer);
static void write_openplc_bool_bit(const uint8_t *src, s7comm_buffer_type_t type,
                                   int buffer_index, int bit_index);
static s7comm_db_runtime_t* find_db_runtime(int db_number);
static s7comm_area_runtime_t* find_area_runtime(int area);
static int get_type_size(s7comm_buffer_type_t type);
static bool is_bool_type(s7comm_buffer_type_t type);
static bool resolve_bit_address(int bit_address, int area_size_bytes, int mapping_start,
                                int *buffer_index, int *bit_index);

/*
 * =============================================================================
 * Endianness Conversion Helpers
 * S7 protocol uses big-endian (network byte order)
 * =============================================================================
 */
static inline uint16_t swap16(uint16_t val)
{
    return ((val & 0xFF00) >> 8) | ((val & 0x00FF) << 8);
}

static inline uint32_t swap32(uint32_t val)
{
    return ((val & 0xFF000000) >> 24) |
           ((val & 0x00FF0000) >> 8)  |
           ((val & 0x0000FF00) << 8)  |
           ((val & 0x000000FF) << 24);
}

static inline uint64_t swap64(uint64_t val)
{
    return ((val & 0xFF00000000000000ULL) >> 56) |
           ((val & 0x00FF000000000000ULL) >> 40) |
           ((val & 0x0000FF0000000000ULL) >> 24) |
           ((val & 0x000000FF00000000ULL) >> 8)  |
           ((val & 0x00000000FF000000ULL) << 8)  |
           ((val & 0x0000000000FF0000ULL) << 24) |
           ((val & 0x000000000000FF00ULL) << 40) |
           ((val & 0x00000000000000FFULL) << 56);
}

/*
 * =============================================================================
 * Memory Management
 * =============================================================================
 */

/**
 * @brief Allocate a system area buffer
 */
static int allocate_area(s7comm_area_runtime_t *area, const s7comm_system_area_t *config)
{
    memset(area, 0, sizeof(s7comm_area_runtime_t));

    if (!config->enabled || config->size_bytes <= 0) {
        area->enabled = false;
        return 0;
    }

    area->enabled = true;
    area->size_bytes = config->size_bytes;
    area->type = config->mapping.type;
    area->start_buffer = config->mapping.start_buffer;

    /* Allocate S7 buffer (what Snap7 clients see) */
    area->s7_buffer = (uint8_t *)calloc(1, config->size_bytes);
    if (area->s7_buffer == NULL) {
        return -1;
    }

    return 0;
}

/**
 * @brief Free a system area's buffer
 */
static void free_area(s7comm_area_runtime_t *area)
{
    if (area->s7_buffer != NULL) {
        free(area->s7_buffer);
        area->s7_buffer = NULL;
    }
    area->enabled = false;
}

/**
 * @brief Allocate all S7 buffers based on configuration
 */
static int allocate_buffers(void)
{
    g_num_db_runtime = 0;

    /* Allocate system areas */
    if (allocate_area(&g_pe_runtime, &g_config.pe_area) != 0) {
        plugin_logger_error(&g_logger, "Failed to allocate PE area buffer");
        return -1;
    }

    if (allocate_area(&g_pa_runtime, &g_config.pa_area) != 0) {
        plugin_logger_error(&g_logger, "Failed to allocate PA area buffer");
        return -1;
    }

    if (allocate_area(&g_mk_runtime, &g_config.mk_area) != 0) {
        plugin_logger_error(&g_logger, "Failed to allocate MK area buffer");
        return -1;
    }

    /* Allocate data blocks */
    for (int i = 0; i < g_config.num_data_blocks; i++) {
        const s7comm_data_block_t *db_cfg = &g_config.data_blocks[i];

        if (db_cfg->size_bytes <= 0 || db_cfg->size_bytes > S7COMM_MAX_DB_SIZE) {
            plugin_logger_warn(&g_logger, "DB%d: invalid size %d, skipping",
                              db_cfg->db_number, db_cfg->size_bytes);
            continue;
        }

        s7comm_db_runtime_t *db_rt = &g_db_runtime[g_num_db_runtime];
        db_rt->db_number = db_cfg->db_number;
        db_rt->type = db_cfg->mapping.type;
        db_rt->start_buffer = db_cfg->mapping.start_buffer;
        db_rt->size_bytes = db_cfg->size_bytes;
        db_rt->bit_addressing = db_cfg->mapping.bit_addressing;

        /* Allocate S7 buffer */
        db_rt->s7_buffer = (uint8_t *)calloc(1, db_cfg->size_bytes);
        if (db_rt->s7_buffer == NULL) {
            plugin_logger_error(&g_logger, "Failed to allocate DB%d S7 buffer", db_cfg->db_number);
            return -1;
        }

        g_num_db_runtime++;
        plugin_logger_debug(&g_logger, "Allocated DB%d: %d bytes, type=%s",
                           db_cfg->db_number, db_cfg->size_bytes,
                           s7comm_buffer_type_name(db_cfg->mapping.type));
    }

    return 0;
}

/**
 * @brief Free all allocated buffers
 */
static void free_buffers(void)
{
    /* Free system areas */
    free_area(&g_pe_runtime);
    free_area(&g_pa_runtime);
    free_area(&g_mk_runtime);

    /* Free data blocks */
    for (int i = 0; i < g_num_db_runtime; i++) {
        if (g_db_runtime[i].s7_buffer != NULL) {
            free(g_db_runtime[i].s7_buffer);
            g_db_runtime[i].s7_buffer = NULL;
        }
    }
    g_num_db_runtime = 0;
}

/**
 * @brief Register all S7 areas with the Snap7 server
 */
static int register_all_areas(void)
{
    int result;

    /* Register system areas (using S7 buffers) */
    if (g_pe_runtime.enabled && g_pe_runtime.s7_buffer != NULL) {
        result = Srv_RegisterArea(g_server, srvAreaPE, 0, g_pe_runtime.s7_buffer, g_pe_runtime.size_bytes);
        if (result != 0) {
            plugin_logger_warn(&g_logger, "Failed to register PE area: 0x%08X", result);
        } else {
            plugin_logger_debug(&g_logger, "Registered PE area: %d bytes", g_pe_runtime.size_bytes);
        }
    }

    if (g_pa_runtime.enabled && g_pa_runtime.s7_buffer != NULL) {
        result = Srv_RegisterArea(g_server, srvAreaPA, 0, g_pa_runtime.s7_buffer, g_pa_runtime.size_bytes);
        if (result != 0) {
            plugin_logger_warn(&g_logger, "Failed to register PA area: 0x%08X", result);
        } else {
            plugin_logger_debug(&g_logger, "Registered PA area: %d bytes", g_pa_runtime.size_bytes);
        }
    }

    if (g_mk_runtime.enabled && g_mk_runtime.s7_buffer != NULL) {
        result = Srv_RegisterArea(g_server, srvAreaMK, 0, g_mk_runtime.s7_buffer, g_mk_runtime.size_bytes);
        if (result != 0) {
            plugin_logger_warn(&g_logger, "Failed to register MK area: 0x%08X", result);
        } else {
            plugin_logger_debug(&g_logger, "Registered MK area: %d bytes", g_mk_runtime.size_bytes);
        }
    }

    /* Register data blocks (using S7 buffers) */
    for (int i = 0; i < g_num_db_runtime; i++) {
        s7comm_db_runtime_t *db = &g_db_runtime[i];
        result = Srv_RegisterArea(g_server, srvAreaDB, db->db_number, db->s7_buffer, db->size_bytes);
        if (result != 0) {
            plugin_logger_warn(&g_logger, "Failed to register DB%d: 0x%08X", db->db_number, result);
        } else {
            plugin_logger_debug(&g_logger, "Registered DB%d: %d bytes", db->db_number, db->size_bytes);
        }
    }

    return 0;
}

/*
 * =============================================================================
 * Plugin Lifecycle Functions
 * =============================================================================
 */

/**
 * @brief Initialize the S7Comm plugin
 */
extern "C" int init(void *args)
{
    if (!args) {
        plugin_logger_init(&g_logger, "S7COMM", NULL);
        plugin_logger_error(&g_logger, "init args is NULL");
        return -1;
    }

    /* Copy runtime args (critical - pointer is freed after init returns) */
    memcpy(&g_runtime_args, args, sizeof(plugin_runtime_args_t));

    /* Initialize logger with runtime_args for central logging */
    plugin_logger_init(&g_logger, "S7COMM", args);
    plugin_logger_info(&g_logger, "Initializing S7Comm plugin...");

    plugin_logger_info(&g_logger, "Buffer size: %d", g_runtime_args.buffer_size);

    g_initialized = true;
    return 0;
}

/**
 * @brief Start the S7 server
 */
extern "C" int start_loop(void)
{
    if (!g_initialized) {
        plugin_logger_error(&g_logger, "Cannot start - plugin not initialized");
        return -1;
    }

    /* Parse configuration file */
    const char *config_path = g_runtime_args.plugin_specific_config_file_path;
    if (config_path == NULL || config_path[0] == '\0') {
        plugin_logger_warn(&g_logger, "No config file specified, using defaults");
        s7comm_config_init_defaults(&g_config);
    } else {
        plugin_logger_info(&g_logger, "Loading config: %s", config_path);
        int result = s7comm_config_parse(config_path, &g_config);
        if (result != 0) {
            plugin_logger_error(&g_logger, "Failed to parse config file (error %d)", result);
            plugin_logger_warn(&g_logger, "Using default configuration");
            s7comm_config_init_defaults(&g_config);
        } else {
            plugin_logger_info(&g_logger, "Configuration loaded successfully");
            g_config_loaded = true;
        }
    }

    /* Check if server is enabled */
    if (!g_config.enabled) {
        plugin_logger_info(&g_logger, "S7Comm server is disabled in configuration");
        return 0;
    }

    if (g_running) {
        plugin_logger_warn(&g_logger, "Server already running");
        return 0;
    }

    /* Log configuration summary */
    plugin_logger_info(&g_logger, "Server config: port=%d, max_clients=%d, pdu_size=%d",
                       g_config.port, g_config.max_clients, g_config.pdu_size);
    plugin_logger_info(&g_logger, "PLC identity: %s (%s)", g_config.identity.name, g_config.identity.module_type);
    plugin_logger_info(&g_logger, "Data blocks configured: %d", g_config.num_data_blocks);

    /* Allocate S7 buffers */
    if (allocate_buffers() != 0) {
        plugin_logger_error(&g_logger, "Failed to allocate buffers");
        free_buffers();
        return -1;
    }

    /* Create Snap7 server */
    g_server = Srv_Create();
    if (g_server == 0) {
        plugin_logger_error(&g_logger, "Failed to create Snap7 server");
        free_buffers();
        return -1;
    }

    /* Configure server parameters from config */
    uint16_t port = g_config.port;
    int max_clients = g_config.max_clients;
    int work_interval = g_config.work_interval_ms;
    int send_timeout = g_config.send_timeout_ms;
    int recv_timeout = g_config.recv_timeout_ms;
    int ping_timeout = g_config.ping_timeout_ms;
    int pdu_size = g_config.pdu_size;

    Srv_SetParam(g_server, p_u16_LocalPort, &port);
    Srv_SetParam(g_server, p_i32_MaxClients, &max_clients);
    Srv_SetParam(g_server, p_i32_WorkInterval, &work_interval);
    Srv_SetParam(g_server, p_i32_SendTimeout, &send_timeout);
    Srv_SetParam(g_server, p_i32_RecvTimeout, &recv_timeout);
    Srv_SetParam(g_server, p_i32_PingTimeout, &ping_timeout);
    Srv_SetParam(g_server, p_i32_PDURequest, &pdu_size);

    /* Set event mask based on logging configuration */
    longword event_mask = 0;
    if (g_config.logging.log_connections) {
        event_mask |= evcServerStarted | evcServerStopped |
                      evcClientAdded | evcClientDisconnected | evcClientRejected;
    }
    if (g_config.logging.log_errors) {
        event_mask |= evcListenerCannotStart | evcClientException;
    }
    if (g_config.logging.log_data_access) {
        event_mask |= evcDataRead | evcDataWrite;
    }
    Srv_SetMask(g_server, mkEvent, event_mask);

    /* Set event callback for logging */
    Srv_SetEventsCallback(g_server, s7comm_event_callback, NULL);

    /* Set RWArea callback for on-demand data synchronization */
    Srv_SetRWAreaCallback(g_server, s7comm_rw_area_callback, NULL);

    /* Register all S7 areas with the server */
    register_all_areas();

    plugin_logger_info(&g_logger, "S7Comm plugin setup complete (journal-buffered mode)");

    /* Log registered areas summary */
    if (g_pe_runtime.enabled) {
        plugin_logger_info(&g_logger, "PE area: %d bytes -> %s[%d]",
                          g_pe_runtime.size_bytes,
                          s7comm_buffer_type_name(g_pe_runtime.type),
                          g_pe_runtime.start_buffer);
    }
    if (g_pa_runtime.enabled) {
        plugin_logger_info(&g_logger, "PA area: %d bytes -> %s[%d]",
                          g_pa_runtime.size_bytes,
                          s7comm_buffer_type_name(g_pa_runtime.type),
                          g_pa_runtime.start_buffer);
    }
    if (g_mk_runtime.enabled) {
        plugin_logger_info(&g_logger, "MK area: %d bytes -> %s[%d]",
                          g_mk_runtime.size_bytes,
                          s7comm_buffer_type_name(g_mk_runtime.type),
                          g_mk_runtime.start_buffer);
    }
    for (int i = 0; i < g_num_db_runtime; i++) {
        plugin_logger_info(&g_logger, "DB%d: %d bytes -> %s[%d]",
                          g_db_runtime[i].db_number,
                          g_db_runtime[i].size_bytes,
                          s7comm_buffer_type_name(g_db_runtime[i].type),
                          g_db_runtime[i].start_buffer);
    }

    plugin_logger_info(&g_logger, "Starting S7 server on %s:%d...",
                       g_config.bind_address, g_config.port);

    /* Start the server */
    int result;
    if (strcmp(g_config.bind_address, "0.0.0.0") == 0) {
        result = Srv_Start(g_server);
    } else {
        result = Srv_StartTo(g_server, g_config.bind_address);
    }

    if (result != 0) {
        plugin_logger_error(&g_logger, "Failed to start S7 server: 0x%08X", result);
        if (g_config.port < 1024) {
            plugin_logger_error(&g_logger, "Note: Port %d requires root privileges on Linux", g_config.port);
        }
        return -1;
    }

    g_running = true;
    plugin_logger_info(&g_logger, "S7 server started successfully");
    return 0;
}

/**
 * @brief Stop the S7 server
 */
extern "C" void stop_loop(void)
{
    if (!g_running) {
        plugin_logger_debug(&g_logger, "Server already stopped");
        return;
    }

    plugin_logger_info(&g_logger, "Stopping S7 server...");

    Srv_Stop(g_server);
    g_running = false;

    plugin_logger_info(&g_logger, "S7 server stopped");
}

/**
 * @brief Cleanup plugin resources
 */
extern "C" void cleanup(void)
{
    plugin_logger_info(&g_logger, "Cleaning up S7Comm plugin...");

    if (g_running) {
        stop_loop();
    }

    if (g_server != 0) {
        Srv_Destroy(g_server);
        g_server = 0;
    }

    free_buffers();

    g_initialized = false;
    g_config_loaded = false;
    plugin_logger_info(&g_logger, "S7Comm plugin cleanup complete");
}

/**
 * @brief Called at the start of each PLC scan cycle
 *
 * With the journal-based approach, data synchronization happens on-demand
 * via the RWArea callback. No action needed here.
 */
extern "C" void cycle_start(void)
{
    /* Data sync is handled on-demand via RWArea callback */
}

/**
 * @brief Called at the end of each PLC scan cycle
 *
 * With the journal-based approach, data synchronization happens on-demand
 * via the RWArea callback. No action needed here.
 */
extern "C" void cycle_end(void)
{
    /* Data sync is handled on-demand via RWArea callback */
}

/*
 * =============================================================================
 * Snap7 Callbacks
 * =============================================================================
 */

/**
 * @brief Snap7 event callback for logging connections and errors
 */
static void s7comm_event_callback(void *usrPtr, PSrvEvent PEvent, int Size)
{
    (void)usrPtr;
    (void)Size;

    switch (PEvent->EvtCode) {
        case evcServerStarted:
            plugin_logger_info(&g_logger, "S7 server started");
            break;
        case evcServerStopped:
            plugin_logger_info(&g_logger, "S7 server stopped");
            break;
        case evcClientAdded:
            if (g_config.logging.log_connections) {
                plugin_logger_info(&g_logger, "Client connected (ID: %d)", PEvent->EvtSender);
            }
            break;
        case evcClientDisconnected:
            if (g_config.logging.log_connections) {
                plugin_logger_info(&g_logger, "Client disconnected (ID: %d)", PEvent->EvtSender);
            }
            break;
        case evcClientRejected:
            plugin_logger_warn(&g_logger, "Client rejected (ID: %d)", PEvent->EvtSender);
            break;
        case evcListenerCannotStart:
            plugin_logger_error(&g_logger, "Listener cannot start - port may be in use or requires root");
            break;
        case evcClientException:
            if (g_config.logging.log_errors) {
                plugin_logger_warn(&g_logger, "Client exception (ID: %d)", PEvent->EvtSender);
            }
            break;
        case evcDataRead:
            if (g_config.logging.log_data_access) {
                plugin_logger_debug(&g_logger, "Data read by client %d", PEvent->EvtSender);
            }
            break;
        case evcDataWrite:
            if (g_config.logging.log_data_access) {
                plugin_logger_debug(&g_logger, "Data write by client %d", PEvent->EvtSender);
            }
            break;
        default:
            /* Ignore other events */
            break;
    }
}

/*
 * =============================================================================
 * On-Demand Data Synchronization (via RWArea Callback)
 *
 * S7 client READs: Acquire OpenPLC mutex, copy to S7 buffer, release mutex
 * S7 client WRITEs: Use journal writes (thread-safe, no mutex needed)
 * =============================================================================
 */

/**
 * @brief Map s7comm buffer type to journal buffer type
 *
 * Journal buffer types (from plugin_types.h):
 *   0=BOOL_INPUT, 1=BOOL_OUTPUT, 2=BOOL_MEMORY
 *   3=BYTE_INPUT, 4=BYTE_OUTPUT
 *   5=INT_INPUT, 6=INT_OUTPUT, 7=INT_MEMORY
 *   8=DINT_INPUT, 9=DINT_OUTPUT, 10=DINT_MEMORY
 *   11=LINT_INPUT, 12=LINT_OUTPUT, 13=LINT_MEMORY
 */
static int map_to_journal_type(s7comm_buffer_type_t type)
{
    switch (type) {
        case BUFFER_TYPE_BOOL_INPUT:  return 0;
        case BUFFER_TYPE_BOOL_OUTPUT: return 1;
        case BUFFER_TYPE_BOOL_MEMORY: return 2;
        case BUFFER_TYPE_INT_INPUT:   return 5;
        case BUFFER_TYPE_INT_OUTPUT:  return 6;
        case BUFFER_TYPE_INT_MEMORY:  return 7;
        case BUFFER_TYPE_DINT_INPUT:  return 8;
        case BUFFER_TYPE_DINT_OUTPUT: return 9;
        case BUFFER_TYPE_DINT_MEMORY: return 10;
        case BUFFER_TYPE_LINT_INPUT:  return 11;
        case BUFFER_TYPE_LINT_OUTPUT: return 12;
        case BUFFER_TYPE_LINT_MEMORY: return 13;
        default:                      return -1;
    }
}

/**
 * @brief Find DB runtime structure by DB number
 */
static s7comm_db_runtime_t* find_db_runtime(int db_number)
{
    for (int i = 0; i < g_num_db_runtime; i++) {
        if (g_db_runtime[i].db_number == db_number) {
            return &g_db_runtime[i];
        }
    }
    return NULL;
}

/**
 * @brief Find system area runtime structure by S7 protocol area code
 *
 * Note: The callback receives S7 protocol area codes (S7AreaPE=0x81, S7AreaPA=0x82, etc.)
 * not server area codes (srvAreaPE=0, srvAreaPA=1, etc.)
 */
static s7comm_area_runtime_t* find_area_runtime(int area)
{
    switch (area) {
        case S7AreaPE:
            return g_pe_runtime.enabled ? &g_pe_runtime : NULL;
        case S7AreaPA:
            return g_pa_runtime.enabled ? &g_pa_runtime : NULL;
        case S7AreaMK:
            return g_mk_runtime.enabled ? &g_mk_runtime : NULL;
        default:
            return NULL;
    }
}

/**
 * @brief Get size of a single element for a buffer type
 */
static int get_type_size(s7comm_buffer_type_t type)
{
    switch (type) {
        case BUFFER_TYPE_BOOL_INPUT:
        case BUFFER_TYPE_BOOL_OUTPUT:
        case BUFFER_TYPE_BOOL_MEMORY:
            return 1;  /* 1 byte per bool group */

        case BUFFER_TYPE_INT_INPUT:
        case BUFFER_TYPE_INT_OUTPUT:
        case BUFFER_TYPE_INT_MEMORY:
            return 2;  /* 2 bytes per INT */

        case BUFFER_TYPE_DINT_INPUT:
        case BUFFER_TYPE_DINT_OUTPUT:
        case BUFFER_TYPE_DINT_MEMORY:
            return 4;  /* 4 bytes per DINT */

        case BUFFER_TYPE_LINT_INPUT:
        case BUFFER_TYPE_LINT_OUTPUT:
        case BUFFER_TYPE_LINT_MEMORY:
            return 8;  /* 8 bytes per LINT */

        default:
            return 1;
    }
}

/**
 * @brief Check whether a mapping uses an OpenPLC BOOL image table
 */
static bool is_bool_type(s7comm_buffer_type_t type)
{
    return type == BUFFER_TYPE_BOOL_INPUT ||
           type == BUFFER_TYPE_BOOL_OUTPUT ||
           type == BUFFER_TYPE_BOOL_MEMORY;
}

/**
 * @brief Resolve an S7 bit offset to an OpenPLC BOOL byte/bit position
 *
 * Snap7 passes TS7Tag::Start in bits when WordLen is S7WLBit. For example,
 * DBX0.1 arrives as bit_address=1, while DBX1.0 arrives as bit_address=8.
 */
static bool resolve_bit_address(int bit_address, int area_size_bytes, int mapping_start,
                                int *buffer_index, int *bit_index)
{
    if (buffer_index == NULL || bit_index == NULL || bit_address < 0 ||
        bit_address >= area_size_bytes * 8) {
        return false;
    }

    int resolved_buffer_index = mapping_start + (bit_address / 8);
    if (resolved_buffer_index < 0 || resolved_buffer_index >= g_runtime_args.buffer_size) {
        return false;
    }

    *buffer_index = resolved_buffer_index;
    *bit_index = bit_address % 8;
    return true;
}

/*
 * =============================================================================
 * Read Functions: OpenPLC -> S7 Buffer (for S7 client READs)
 * These functions copy data from OpenPLC image tables to the S7 buffer.
 * Called with OpenPLC mutex held.
 * =============================================================================
 */

/**
 * @brief Read OpenPLC bool buffer to destination (mutex must be held)
 */
static void read_openplc_bool_to_buffer(uint8_t *dest, int size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_BOOL *(*buffer)[8] = NULL;

    switch (type) {
        case BUFFER_TYPE_BOOL_INPUT:
            buffer = g_runtime_args.bool_input;
            break;
        case BUFFER_TYPE_BOOL_OUTPUT:
            buffer = g_runtime_args.bool_output;
            break;
        case BUFFER_TYPE_BOOL_MEMORY:
            buffer = g_runtime_args.bool_memory;
            break;
        default:
            return;
    }

    if (buffer == NULL) {
        return;
    }

    int max_bytes = g_runtime_args.buffer_size - start_buffer;
    if (max_bytes > size) max_bytes = size;

    for (int byte_idx = 0; byte_idx < max_bytes; byte_idx++) {
        uint8_t byte_val = 0;
        int plc_idx = start_buffer + byte_idx;
        for (int bit_idx = 0; bit_idx < 8; bit_idx++) {
            IEC_BOOL *ptr = buffer[plc_idx][bit_idx];
            if (ptr != NULL && *ptr) {
                byte_val |= (1 << bit_idx);
            }
        }
        dest[byte_idx] = byte_val;
    }
}

/**
 * @brief Read OpenPLC int buffer to destination with endian conversion (mutex must be held)
 */
static void read_openplc_int_to_buffer(uint8_t *dest, int size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_UINT **buffer = NULL;

    switch (type) {
        case BUFFER_TYPE_INT_INPUT:
            buffer = g_runtime_args.int_input;
            break;
        case BUFFER_TYPE_INT_OUTPUT:
            buffer = g_runtime_args.int_output;
            break;
        case BUFFER_TYPE_INT_MEMORY:
            buffer = g_runtime_args.int_memory;
            break;
        default:
            return;
    }

    uint16_t *s7_words = (uint16_t *)dest;
    int num_words = size / 2;
    int max_words = g_runtime_args.buffer_size - start_buffer;
    if (max_words > num_words) max_words = num_words;

    for (int i = 0; i < max_words; i++) {
        IEC_UINT *ptr = buffer[start_buffer + i];
        if (ptr != NULL) {
            s7_words[i] = swap16(*ptr);
        }
    }
}

/**
 * @brief Read OpenPLC dint buffer to destination with endian conversion (mutex must be held)
 */
static void read_openplc_dint_to_buffer(uint8_t *dest, int size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_UDINT **buffer = NULL;

    switch (type) {
        case BUFFER_TYPE_DINT_INPUT:
            buffer = g_runtime_args.dint_input;
            break;
        case BUFFER_TYPE_DINT_OUTPUT:
            buffer = g_runtime_args.dint_output;
            break;
        case BUFFER_TYPE_DINT_MEMORY:
            buffer = g_runtime_args.dint_memory;
            break;
        default:
            return;
    }

    uint32_t *s7_dwords = (uint32_t *)dest;
    int num_dwords = size / 4;
    int max_dwords = g_runtime_args.buffer_size - start_buffer;
    if (max_dwords > num_dwords) max_dwords = num_dwords;

    for (int i = 0; i < max_dwords; i++) {
        IEC_UDINT *ptr = buffer[start_buffer + i];
        if (ptr != NULL) {
            s7_dwords[i] = swap32(*ptr);
        }
    }
}

/**
 * @brief Read OpenPLC lint buffer to destination with endian conversion (mutex must be held)
 */
static void read_openplc_lint_to_buffer(uint8_t *dest, int size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_ULINT **buffer = NULL;

    switch (type) {
        case BUFFER_TYPE_LINT_INPUT:
            buffer = g_runtime_args.lint_input;
            break;
        case BUFFER_TYPE_LINT_OUTPUT:
            buffer = g_runtime_args.lint_output;
            break;
        case BUFFER_TYPE_LINT_MEMORY:
            buffer = g_runtime_args.lint_memory;
            break;
        default:
            return;
    }

    uint64_t *s7_lwords = (uint64_t *)dest;
    int num_lwords = size / 8;
    int max_lwords = g_runtime_args.buffer_size - start_buffer;
    if (max_lwords > num_lwords) max_lwords = num_lwords;

    for (int i = 0; i < max_lwords; i++) {
        IEC_ULINT *ptr = buffer[start_buffer + i];
        if (ptr != NULL) {
            s7_lwords[i] = swap64(*ptr);
        }
    }
}

/**
 * @brief Dispatch read from OpenPLC to buffer based on buffer type
 */
static void read_openplc_to_buffer(uint8_t *dest, int size, s7comm_buffer_type_t type, int start_buffer)
{
    switch (type) {
        case BUFFER_TYPE_BOOL_INPUT:
        case BUFFER_TYPE_BOOL_OUTPUT:
        case BUFFER_TYPE_BOOL_MEMORY:
            read_openplc_bool_to_buffer(dest, size, type, start_buffer);
            break;

        case BUFFER_TYPE_INT_INPUT:
        case BUFFER_TYPE_INT_OUTPUT:
        case BUFFER_TYPE_INT_MEMORY:
            read_openplc_int_to_buffer(dest, size, type, start_buffer);
            break;

        case BUFFER_TYPE_DINT_INPUT:
        case BUFFER_TYPE_DINT_OUTPUT:
        case BUFFER_TYPE_DINT_MEMORY:
            read_openplc_dint_to_buffer(dest, size, type, start_buffer);
            break;

        case BUFFER_TYPE_LINT_INPUT:
        case BUFFER_TYPE_LINT_OUTPUT:
        case BUFFER_TYPE_LINT_MEMORY:
            read_openplc_lint_to_buffer(dest, size, type, start_buffer);
            break;

        default:
            break;
    }
}

/*
 * =============================================================================
 * Write Functions: S7 Buffer -> OpenPLC via Journal (for S7 client WRITEs)
 * These functions write data from S7 buffer to OpenPLC via journal.
 * No mutex needed - journal writes are thread-safe.
 * =============================================================================
 */

/**
 * @brief Write bool buffer to OpenPLC via journal
 */
static void write_bool_to_openplc_journal(uint8_t *src, int size, s7comm_buffer_type_t type, int start_buffer)
{
    int journal_type = map_to_journal_type(type);
    if (journal_type < 0) return;

    int max_bytes = g_runtime_args.buffer_size - start_buffer;
    if (max_bytes > size) max_bytes = size;

    for (int byte_idx = 0; byte_idx < max_bytes; byte_idx++) {
        uint8_t byte_val = src[byte_idx];
        int plc_idx = start_buffer + byte_idx;
        for (int bit_idx = 0; bit_idx < 8; bit_idx++) {
            int bit_val = (byte_val >> bit_idx) & 0x01;
            g_runtime_args.journal_write_bool(journal_type, plc_idx, bit_idx, bit_val);
        }
    }
}

/**
 * @brief Write one S7 bit payload to one OpenPLC BOOL journal entry
 */
static void write_openplc_bool_bit(const uint8_t *src, s7comm_buffer_type_t type,
                                   int buffer_index, int bit_index)
{
    int journal_type = map_to_journal_type(type);
    if (journal_type < 0) return;

    g_runtime_args.journal_write_bool(journal_type, buffer_index, bit_index,
                                      (src[0] & 0x01) != 0);
}

/**
 * @brief Write int buffer to OpenPLC via journal with endian conversion
 */
static void write_int_to_openplc_journal(uint8_t *src, int size, s7comm_buffer_type_t type, int start_buffer)
{
    int journal_type = map_to_journal_type(type);
    if (journal_type < 0) return;

    uint16_t *s7_words = (uint16_t *)src;
    int num_words = size / 2;
    int max_words = g_runtime_args.buffer_size - start_buffer;
    if (max_words > num_words) max_words = num_words;

    for (int i = 0; i < max_words; i++) {
        uint16_t value = swap16(s7_words[i]);
        g_runtime_args.journal_write_int(journal_type, start_buffer + i, value);
    }
}

/**
 * @brief Write dint buffer to OpenPLC via journal with endian conversion
 */
static void write_dint_to_openplc_journal(uint8_t *src, int size, s7comm_buffer_type_t type, int start_buffer)
{
    int journal_type = map_to_journal_type(type);
    if (journal_type < 0) return;

    uint32_t *s7_dwords = (uint32_t *)src;
    int num_dwords = size / 4;
    int max_dwords = g_runtime_args.buffer_size - start_buffer;
    if (max_dwords > num_dwords) max_dwords = num_dwords;

    for (int i = 0; i < max_dwords; i++) {
        uint32_t value = swap32(s7_dwords[i]);
        g_runtime_args.journal_write_dint(journal_type, start_buffer + i, value);
    }
}

/**
 * @brief Write lint buffer to OpenPLC via journal with endian conversion
 */
static void write_lint_to_openplc_journal(uint8_t *src, int size, s7comm_buffer_type_t type, int start_buffer)
{
    int journal_type = map_to_journal_type(type);
    if (journal_type < 0) return;

    uint64_t *s7_lwords = (uint64_t *)src;
    int num_lwords = size / 8;
    int max_lwords = g_runtime_args.buffer_size - start_buffer;
    if (max_lwords > num_lwords) max_lwords = num_lwords;

    for (int i = 0; i < max_lwords; i++) {
        uint64_t value = swap64(s7_lwords[i]);
        g_runtime_args.journal_write_lint(journal_type, start_buffer + i, value);
    }
}

/**
 * @brief Dispatch write from buffer to OpenPLC journal based on buffer type
 */
static void write_buffer_to_openplc_journal(uint8_t *src, int size, s7comm_buffer_type_t type, int start_buffer)
{
    switch (type) {
        case BUFFER_TYPE_BOOL_INPUT:
        case BUFFER_TYPE_BOOL_OUTPUT:
        case BUFFER_TYPE_BOOL_MEMORY:
            write_bool_to_openplc_journal(src, size, type, start_buffer);
            break;

        case BUFFER_TYPE_INT_INPUT:
        case BUFFER_TYPE_INT_OUTPUT:
        case BUFFER_TYPE_INT_MEMORY:
            write_int_to_openplc_journal(src, size, type, start_buffer);
            break;

        case BUFFER_TYPE_DINT_INPUT:
        case BUFFER_TYPE_DINT_OUTPUT:
        case BUFFER_TYPE_DINT_MEMORY:
            write_dint_to_openplc_journal(src, size, type, start_buffer);
            break;

        case BUFFER_TYPE_LINT_INPUT:
        case BUFFER_TYPE_LINT_OUTPUT:
        case BUFFER_TYPE_LINT_MEMORY:
            write_lint_to_openplc_journal(src, size, type, start_buffer);
            break;

        default:
            break;
    }
}

/*
 * =============================================================================
 * Snap7 RWArea Callback - On-Demand Data Synchronization
 * =============================================================================
 */

/**
 * @brief Snap7 RWArea callback for on-demand data synchronization
 *
 * Called by Snap7 when an S7 client reads or writes data.
 * - On READ: Acquire OpenPLC mutex, copy fresh data to S7 buffer, release mutex
 * - On WRITE: Use journal writes (thread-safe, no mutex needed)
 *
 * @param usrPtr User pointer (unused)
 * @param Sender Client identifier
 * @param Operation OperationRead or OperationWrite
 * @param PTag S7 tag with Area, DBNumber, Start, Size
 * @param pUsrData Pointer to data buffer
 * @return 0 to accept operation, non-zero to reject
 */
static int s7comm_rw_area_callback(void *usrPtr, int Sender, int Operation, PS7Tag PTag, void *pUsrData)
{
    (void)usrPtr;
    (void)Sender;

    if (pUsrData == NULL || PTag == NULL) {
        return -1;
    }

    s7comm_buffer_type_t type;
    int start_buffer;
    int mapping_start;
    int area_size_bytes;
    int bit_index = 0;
    int size = PTag->Size;
    bool bit_access = PTag->WordLen == S7WLBit;

    /* Determine mapping based on S7 protocol area code */
    if (PTag->Area == S7AreaDB) {
        /* Data block - look up configuration */
        s7comm_db_runtime_t *db = find_db_runtime(PTag->DBNumber);
        if (db == NULL) {
            /* DB not configured - return zeros for read, ignore write */
            return 0;
        }
        type = db->type;
        mapping_start = db->start_buffer;
        area_size_bytes = db->size_bytes;
    } else {
        /* System area (PE, PA, MK) */
        s7comm_area_runtime_t *area = find_area_runtime(PTag->Area);
        if (area == NULL) {
            /* Area not configured - return zeros for read, ignore write */
            return 0;
        }
        type = area->type;
        mapping_start = area->start_buffer;
        area_size_bytes = area->size_bytes;
    }

    if (bit_access) {
        if (!is_bool_type(type) || size != 1 ||
            !resolve_bit_address(PTag->Start, area_size_bytes, mapping_start,
                                 &start_buffer, &bit_index)) {
            return -1;
        }
    } else {
        start_buffer = mapping_start + (PTag->Start / get_type_size(type));
    }

    if (Operation == OperationRead) {
        /*
         * S7 client is READing - provide fresh data from OpenPLC.
         * image_lock() takes the image mutex and drains the journal so the
         * copy sees freshly committed values; the snap7 TCP send happens
         * after this callback returns, outside the lock. For S7WLBit this
         * copies the containing byte; Snap7 masks the requested bit after
         * the callback returns.
         */
        g_runtime_args.image_lock();
        read_openplc_to_buffer((uint8_t *)pUsrData, size, type, start_buffer);
        g_runtime_args.image_unlock();
    } else if (Operation == OperationWrite) {
        /*
         * S7 client is WRITing - journal the changes
         * Journal writes are thread-safe, no mutex needed
         */
        if (bit_access) {
            write_openplc_bool_bit((const uint8_t *)pUsrData, type, start_buffer, bit_index);
        } else {
            write_buffer_to_openplc_journal((uint8_t *)pUsrData, size, type, start_buffer);
        }
    }

    return 0;  /* Accept operation */
}
