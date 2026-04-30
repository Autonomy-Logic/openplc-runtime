/**
 * @file ethercat_master.c
 * @brief EtherCAT Master SOEM Wrapper Implementation
 *
 * Wraps the SOEM library to provide high-level EtherCAT master operations:
 * network initialization, slave scanning, topology validation against
 * the JSON configuration, SDO writes, state machine management, and
 * slave recovery.
 *
 * Uses the ecx_* context-based API from SOEM 2.x.
 */

#include "ethercat_master.h"
#include "ethercat_proc.h"
#include "soem/soem.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* Low-latency NIC tuning and socket options (Linux only) */
#if !defined(__CYGWIN__) && !defined(_WIN32)
#include <sys/socket.h>
#define ECAT_BUSY_POLL_US 50

/*
 * =============================================================================
 * NIC Settings Save / Restore (with crash-recovery persistence)
 * =============================================================================
 *
 * Before applying low-latency ethtool tuning we capture the original NIC
 * settings and persist them to a file on disk.  On a normal shutdown the
 * settings are restored and the file is removed.  If the process is killed
 * (SIGKILL, OOM, crash), the file survives and the next startup detects it,
 * restoring the NIC before applying fresh tuning.
 *
 * Persistence file: /run/runtime/ecat_nic_saved.conf  (inside tmpfs, so
 * a full system reboot clears it -- at which point the kernel already
 * reloads driver defaults).
 *
 * File format (simple key=value, one per line):
 *   iface=eth0
 *   rx_usecs=100
 *   tx_usecs=50
 *   gro=on
 *   gso=on
 *   tso=on
 */

#include <sys/stat.h>
#include <unistd.h>
#include <errno.h>

/** Maximum interface name length (IFNAMSIZ) */
#define NIC_IFNAME_MAX 16

/** Directory and file used to persist original NIC settings across crashes */
#define NIC_SAVE_DIR  "/run/runtime"
/* Per-interface save files are built by nic_save_path() as:
 *   NIC_SAVE_DIR "/ecat_nic_saved_<iface>.conf"  */

typedef struct {
    char iface[NIC_IFNAME_MAX];  /* interface these settings belong to     */
    int  saved;                  /* non-zero if values were captured       */

    /* ethtool -C (coalescing) */
    int  rx_usecs;
    int  tx_usecs;
    int  coalescing_saved;       /* non-zero if coalescing values valid    */

    /* ethtool -K (offloads) */
    int  gro;                    /* 1 = on, 0 = off */
    int  gso;
    int  tso;
    int  offloads_saved;         /* non-zero if offload values valid       */
} nic_saved_settings_t;

#define ECAT_MAX_NIC_SAVED ECAT_MAX_MASTERS
static nic_saved_settings_t g_nic_saved[ECAT_MAX_NIC_SAVED];
static int g_nic_saved_count = 0;
static pthread_mutex_t g_nic_saved_mutex = PTHREAD_MUTEX_INITIALIZER;

/**
 * @brief Find saved NIC settings by interface name.
 *
 * Caller must hold g_nic_saved_mutex.
 *
 * @return Pointer to the matching entry, or NULL if not found.
 */
static nic_saved_settings_t *find_nic_saved(const char *iface)
{
    for (int i = 0; i < g_nic_saved_count; i++) {
        if (strcmp(g_nic_saved[i].iface, iface) == 0)
            return &g_nic_saved[i];
    }
    return NULL;
}

/**
 * @brief Find or allocate a NIC saved settings slot for the given interface.
 *
 * Returns the existing slot if one already exists for this interface,
 * otherwise reserves a new slot. Either way the returned slot is
 * zero-initialized and has the interface name pre-filled.
 *
 * Caller must hold g_nic_saved_mutex.
 *
 * @return Pointer to the slot, or NULL if the array is full.
 */
static nic_saved_settings_t *get_nic_slot(const char *iface)
{
    nic_saved_settings_t *slot = find_nic_saved(iface);
    if (!slot) {
        if (g_nic_saved_count >= ECAT_MAX_NIC_SAVED)
            return NULL;
        slot = &g_nic_saved[g_nic_saved_count++];
    }

    memset(slot, 0, sizeof(*slot));
    strncpy(slot->iface, iface, NIC_IFNAME_MAX - 1);
    slot->iface[NIC_IFNAME_MAX - 1] = '\0';
    return slot;
}

/* ------------------------------------------------------------------ */
/*  Helpers: ethtool output parsing                                    */
/* ------------------------------------------------------------------ */

/**
 * @brief Parse a single integer value from an ethtool output line.
 *
 * Looks for a line containing exactly "<key>:" (not "<key>-something:")
 * and reads the integer that follows the colon.  This avoids matching
 * "rx-usecs-irq" when searching for "rx-usecs".
 *
 * @return 0 on success, -1 if not found
 */
static int parse_ethtool_int(const char *output, const char *key, int *value)
{
    size_t key_len = strlen(key);
    const char *p = output;
    while ((p = strstr(p, key)) != NULL) {
        char next = p[key_len];
        /* Accept only if the key is followed by ':' or whitespace,
         * not by '-' or another alnum (e.g. "rx-usecs-irq") */
        if (next == ':' || next == ' ' || next == '\t')
            break;
        p += key_len;
    }
    if (!p)
        return -1;
    p += key_len;
    while (*p == ':' || *p == ' ' || *p == '\t')
        p++;
    if (*p == '\0' || (*p != '-' && !isdigit((unsigned char)*p)))
        return -1;
    *value = atoi(p);
    return 0;
}

/**
 * @brief Parse an on/off boolean from an ethtool -k output line.
 *
 * Lines look like: "generic-receive-offload: on"
 *
 * @return 0 on success, -1 if not found
 */
static int parse_ethtool_bool(const char *output, const char *key, int *value)
{
    const char *p = strstr(output, key);
    if (!p)
        return -1;
    p += strlen(key);
    while (*p == ':' || *p == ' ' || *p == '\t')
        p++;
    if (strncmp(p, "on", 2) == 0)
        *value = 1;
    else
        *value = 0;
    return 0;
}

/* ------------------------------------------------------------------ */
/*  Persistence: write / read / remove the save file                   */
/* ------------------------------------------------------------------ */

/**
 * @brief Write the in-memory saved settings to the persistence file.
 *
 * Creates NIC_SAVE_DIR if it does not exist.  The file is written
 * atomically: we write to a temporary path and rename, so a crash
 * mid-write never leaves a half-written file.
 */
/**
 * @brief Build the per-interface persistence file path.
 *
 * @param iface  Interface name (e.g. "eth0")
 * @param buf    Output buffer
 * @param size   Size of output buffer
 */
static void nic_save_path(const char *iface, char *buf, size_t size)
{
    snprintf(buf, size, NIC_SAVE_DIR "/ecat_nic_saved_%s.conf", iface);
}

static void persist_nic_settings(const nic_saved_settings_t *ns, plugin_logger_t *logger)
{
    /* Ensure the directory exists (may already exist for the log socket) */
    if (mkdir(NIC_SAVE_DIR, 0755) != 0 && errno != EEXIST) {
        plugin_logger_warn(logger,
            "Cannot create %s: %s - NIC settings will not survive a crash",
            NIC_SAVE_DIR, strerror(errno));
        return;
    }

    char save_file[128];
    nic_save_path(ns->iface, save_file, sizeof(save_file));

    char tmp_path[128];
    snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", save_file);

    FILE *fp = fopen(tmp_path, "w");
    if (!fp) {
        plugin_logger_warn(logger,
            "Cannot write %s: %s - NIC settings will not survive a crash",
            tmp_path, strerror(errno));
        return;
    }

    fprintf(fp, "iface=%s\n", ns->iface);

    if (ns->coalescing_saved) {
        fprintf(fp, "rx_usecs=%d\n", ns->rx_usecs);
        fprintf(fp, "tx_usecs=%d\n", ns->tx_usecs);
    }

    if (ns->offloads_saved) {
        fprintf(fp, "gro=%s\n", ns->gro ? "on" : "off");
        fprintf(fp, "gso=%s\n", ns->gso ? "on" : "off");
        fprintf(fp, "tso=%s\n", ns->tso ? "on" : "off");
    }

    fflush(fp);
    fclose(fp);

    /* Atomic rename so we never have a half-written file */
    if (rename(tmp_path, save_file) != 0) {
        plugin_logger_warn(logger, "Cannot rename %s -> %s: %s",
                           tmp_path, save_file, strerror(errno));
        unlink(tmp_path);
        return;
    }

    plugin_logger_info(logger, "NIC settings persisted to %s", save_file);
}

/**
 * @brief Remove the persistence file (called after a successful restore).
 */
static void remove_nic_save_file(const char *iface, plugin_logger_t *logger)
{
    char save_file[128];
    nic_save_path(iface, save_file, sizeof(save_file));
    if (unlink(save_file) == 0) {
        plugin_logger_debug(logger, "Removed %s", save_file);
    }
    /* ENOENT is fine - file already gone */
}

/**
 * @brief Load saved NIC settings from the persistence file for a given interface.
 *
 * Populates a slot in g_nic_saved from the file contents.
 *
 * @param iface  Interface name to load settings for
 * @param logger Logger instance
 * @return Pointer to the loaded settings, or NULL if no file found / invalid
 */
static nic_saved_settings_t *load_nic_settings_from_file(const char *iface, plugin_logger_t *logger)
{
    char save_file[128];
    nic_save_path(iface, save_file, sizeof(save_file));

    FILE *fp = fopen(save_file, "r");
    if (!fp)
        return NULL;

    nic_saved_settings_t tmp;
    memset(&tmp, 0, sizeof(tmp));

    char line[128];
    while (fgets(line, sizeof(line), fp)) {
        /* Strip trailing newline */
        size_t len = strlen(line);
        if (len > 0 && line[len - 1] == '\n')
            line[len - 1] = '\0';

        char *eq = strchr(line, '=');
        if (!eq)
            continue;
        *eq = '\0';
        const char *key = line;
        const char *val = eq + 1;

        if (strcmp(key, "iface") == 0) {
            strncpy(tmp.iface, val, NIC_IFNAME_MAX - 1);
            tmp.iface[NIC_IFNAME_MAX - 1] = '\0';
        } else if (strcmp(key, "rx_usecs") == 0) {
            tmp.rx_usecs = atoi(val);
            tmp.coalescing_saved = 1;
        } else if (strcmp(key, "tx_usecs") == 0) {
            tmp.tx_usecs = atoi(val);
            tmp.coalescing_saved = 1;
        } else if (strcmp(key, "gro") == 0) {
            tmp.gro = (strcmp(val, "on") == 0) ? 1 : 0;
            tmp.offloads_saved = 1;
        } else if (strcmp(key, "gso") == 0) {
            tmp.gso = (strcmp(val, "on") == 0) ? 1 : 0;
            tmp.offloads_saved = 1;
        } else if (strcmp(key, "tso") == 0) {
            tmp.tso = (strcmp(val, "on") == 0) ? 1 : 0;
            tmp.offloads_saved = 1;
        }
    }

    fclose(fp);

    if (tmp.iface[0] == '\0' || !ecat_is_valid_iface_name(tmp.iface)) {
        plugin_logger_warn(logger,
            "Invalid or empty interface in %s - discarding", save_file);
        remove_nic_save_file(iface, logger);
        return NULL;
    }

    /* Allocate a slot and copy the loaded data */
    pthread_mutex_lock(&g_nic_saved_mutex);
    nic_saved_settings_t *slot = get_nic_slot(tmp.iface);
    if (!slot) {
        pthread_mutex_unlock(&g_nic_saved_mutex);
        plugin_logger_warn(logger,
            "No free NIC saved-settings slot for %s - discarding", tmp.iface);
        remove_nic_save_file(iface, logger);
        return NULL;
    }

    *slot = tmp;
    slot->saved = 1;
    pthread_mutex_unlock(&g_nic_saved_mutex);
    return slot;
}

/* ------------------------------------------------------------------ */
/*  Core: save, restore, recover from crash                            */
/* ------------------------------------------------------------------ */

/**
 * @brief Capture current NIC settings and persist to disk.
 */
static void save_nic_settings(const char *iface, plugin_logger_t *logger)
{
    pthread_mutex_lock(&g_nic_saved_mutex);
    nic_saved_settings_t *ns = get_nic_slot(iface);
    if (!ns) {
        pthread_mutex_unlock(&g_nic_saved_mutex);
        plugin_logger_warn(logger,
            "%s: no free NIC saved-settings slot - skipping save", iface);
        return;
    }
    pthread_mutex_unlock(&g_nic_saved_mutex);

    /* Capture current NIC settings (slow shell calls -- run without lock) */
    nic_saved_settings_t local;
    memset(&local, 0, sizeof(local));
    strncpy(local.iface, iface, NIC_IFNAME_MAX - 1);
    local.iface[NIC_IFNAME_MAX - 1] = '\0';

    char output[2048];

    /* Capture coalescing settings */
    {
        char *argv[] = { "ethtool", "-c", (char *)iface, NULL };
        if (ecat_run_argv("ethtool", argv, output, sizeof(output)) == 0) {
            int ok = 0;
            ok += (parse_ethtool_int(output, "rx-usecs", &local.rx_usecs) == 0);
            ok += (parse_ethtool_int(output, "tx-usecs", &local.tx_usecs) == 0);
            if (ok > 0) {
                local.coalescing_saved = 1;
                plugin_logger_info(logger,
                    "%s: saved coalescing (rx-usecs=%d, tx-usecs=%d)",
                    iface, local.rx_usecs, local.tx_usecs);
            }
        }
    }

    /* Capture offload settings */
    {
        char *argv[] = { "ethtool", "-k", (char *)iface, NULL };
        if (ecat_run_argv("ethtool", argv, output, sizeof(output)) == 0) {
            int ok = 0;
            ok += (parse_ethtool_bool(output, "generic-receive-offload", &local.gro) == 0);
            ok += (parse_ethtool_bool(output, "generic-segmentation-offload", &local.gso) == 0);
            ok += (parse_ethtool_bool(output, "tcp-segmentation-offload", &local.tso) == 0);
            if (ok > 0) {
                local.offloads_saved = 1;
                plugin_logger_info(logger,
                    "%s: saved offloads (gro=%s, gso=%s, tso=%s)",
                    iface,
                    local.gro ? "on" : "off",
                    local.gso ? "on" : "off",
                    local.tso ? "on" : "off");
            }
        }
    }

    local.saved = 1;

    /* Commit captured values back to the shared slot */
    pthread_mutex_lock(&g_nic_saved_mutex);
    *ns = local;
    pthread_mutex_unlock(&g_nic_saved_mutex);

    /* Persist to disk so a crash does not lose the original values */
    persist_nic_settings(&local, logger);
}

/**
 * @brief Apply saved settings back to the NIC and clean up.
 *
 * Works both for the in-memory path (normal shutdown) and for settings
 * loaded from the persistence file (crash recovery).
 */
static void restore_nic_settings(const char *iface, plugin_logger_t *logger)
{
    /* Copy saved settings to stack so ethtool runs without the lock */
    nic_saved_settings_t local;
    nic_saved_settings_t *ns;

    pthread_mutex_lock(&g_nic_saved_mutex);
    ns = find_nic_saved(iface);
    if (!ns || !ns->saved) {
        pthread_mutex_unlock(&g_nic_saved_mutex);
        return;
    }
    local = *ns;
    ns->saved = 0;
    pthread_mutex_unlock(&g_nic_saved_mutex);

    /* Restore NIC settings (slow exec calls -- run without lock) */
    if (local.coalescing_saved) {
        char rx_str[16], tx_str[16];
        snprintf(rx_str, sizeof(rx_str), "%d", local.rx_usecs);
        snprintf(tx_str, sizeof(tx_str), "%d", local.tx_usecs);
        char *argv[] = {
            "ethtool", "-C", (char *)iface,
            "rx-usecs", rx_str,
            "tx-usecs", tx_str,
            NULL
        };
        if (ecat_run_argv("ethtool", argv, NULL, 0) == 0) {
            plugin_logger_info(logger,
                "%s: restored coalescing (rx-usecs=%d, tx-usecs=%d)",
                iface, local.rx_usecs, local.tx_usecs);
        }
    }

    if (local.offloads_saved) {
        char *argv[] = {
            "ethtool", "-K", (char *)iface,
            "gro", local.gro ? "on" : "off",
            "gso", local.gso ? "on" : "off",
            "tso", local.tso ? "on" : "off",
            NULL
        };
        if (ecat_run_argv("ethtool", argv, NULL, 0) == 0) {
            plugin_logger_info(logger,
                "%s: restored offloads (gro=%s, gso=%s, tso=%s)",
                iface,
                local.gro ? "on" : "off",
                local.gso ? "on" : "off",
                local.tso ? "on" : "off");
        }
    }

    remove_nic_save_file(iface, logger);
    plugin_logger_info(logger, "%s: NIC settings restored to original values", iface);
}

/**
 * @brief Recover NIC settings left over from a previous crash.
 *
 * If the persistence file exists it means the previous process died
 * before it could restore the NIC.  Load the file and apply the
 * saved settings now, before we capture fresh ones and re-tune.
 */
static void recover_nic_settings(const char *iface, plugin_logger_t *logger)
{
    nic_saved_settings_t *ns = load_nic_settings_from_file(iface, logger);
    if (!ns)
        return;

    char save_file[128];
    nic_save_path(iface, save_file, sizeof(save_file));

    plugin_logger_warn(logger,
        "Found stale NIC settings file (%s) - previous process likely crashed. "
        "Restoring %s to original settings before re-tuning.",
        save_file, iface);

    restore_nic_settings(iface, logger);
}

#endif /* !__CYGWIN__ && !_WIN32 */

/*
 * =============================================================================
 * SOEM Context and IO Map
 * =============================================================================
 */

/** Number of retries when polling for OPERATIONAL state */
#define ECAT_OP_POLL_RETRIES 10

/*
 * =============================================================================
 * Topology Validation
 * =============================================================================
 */

int ecat_master_validate_topology(ecat_master_instance_t *inst, plugin_logger_t *logger)
{
    const ecat_config_t *config = &inst->config;
    int found_count = inst->ecx_context.slavecount;

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

        ec_slavet *found = &inst->ecx_context.slavelist[pos];

        /* Vendor ID check (can be disabled per slave via startup_checks) */
        if (expected->startup_checks.check_vendor_id) {
            if (found->eep_man != expected->vendor_id) {
                plugin_logger_error(logger,
                    "Slave %d (%s) at position %d: vendor_id mismatch - "
                    "expected 0x%08X, found 0x%08X",
                    i, expected->name, pos,
                    expected->vendor_id, found->eep_man);
                return -1;
            }
        } else {
            plugin_logger_debug(logger,
                "Slave %d (%s) at position %d: vendor_id check disabled",
                i, expected->name, pos);
        }

        /* Product code check (can be disabled per slave via startup_checks) */
        if (expected->startup_checks.check_product_code) {
            if (found->eep_id != expected->product_code) {
                plugin_logger_error(logger,
                    "Slave %d (%s) at position %d: product_code mismatch - "
                    "expected 0x%08X, found 0x%08X",
                    i, expected->name, pos,
                    expected->product_code, found->eep_id);
                return -1;
            }
        } else {
            plugin_logger_debug(logger,
                "Slave %d (%s) at position %d: product_code check disabled",
                i, expected->name, pos);
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
 * Phase 1: Open Interface, Scan Bus, Validate Topology
 * =============================================================================
 */

/**
 * @brief Apply low-latency ethtool settings to the EtherCAT network interface.
 *
 * Disables IRQ coalescing and receive offloads (GRO/GSO/TSO) so that
 * EtherCAT frames are delivered to userspace immediately instead of being
 * batched by the NIC driver.  Called once during master init, before the
 * SOEM raw socket is opened.  Requires ethtool on the host; fails silently
 * if the command is not available or the driver does not support a setting.
 */
static void tune_nic(const char *iface, plugin_logger_t *logger)
{
#if !defined(__CYGWIN__) && !defined(_WIN32)
    if (!ecat_is_valid_iface_name(iface)) {
        plugin_logger_warn(logger, "Skipping NIC tuning: invalid interface name '%s'", iface);
        return;
    }

    /* If a previous process crashed, restore the NIC first */
    recover_nic_settings(iface, logger);

    /* Save current settings so they can be restored on close */
    save_nic_settings(iface, logger);

    /* Disable IRQ coalescing - deliver frames immediately */
    {
        char *argv[] = {
            "ethtool", "-C", (char *)iface,
            "rx-usecs", "0",
            "tx-usecs", "0",
            NULL
        };
        if (ecat_run_argv("ethtool", argv, NULL, 0) == 0) {
            plugin_logger_info(logger,
                "%s: IRQ coalescing disabled (rx-usecs=0 tx-usecs=0)", iface);
        }
    }

    /* Disable receive offloads that batch/merge packets */
    {
        char *argv[] = {
            "ethtool", "-K", (char *)iface,
            "gro", "off",
            "gso", "off",
            "tso", "off",
            NULL
        };
        if (ecat_run_argv("ethtool", argv, NULL, 0) == 0) {
            plugin_logger_info(logger, "%s: GRO/GSO/TSO offloads disabled", iface);
        }
    }
#else
    (void)iface;
    (void)logger;
#endif
}

int ecat_master_open_and_scan(ecat_master_instance_t *inst, plugin_logger_t *logger)
{
    const ecat_config_t *config = &inst->config;

    /* Zero-initialize the SOEM context before use */
    memset(&inst->ecx_context, 0, sizeof(inst->ecx_context));
    memset(inst->iomap, 0, sizeof(inst->iomap));
    inst->iomap_used_size = 0;

    /* Step 0: Tune NIC for low-latency EtherCAT frame delivery */
    tune_nic(config->master.interface, logger);

    /* Step 1: Initialize SOEM on the configured network interface */
    plugin_logger_info(logger, "Opening network interface: %s", config->master.interface);

    if (!ecx_init(&inst->ecx_context, config->master.interface)) {
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

    inst->soem_initialized = 1;
    plugin_logger_info(logger, "Network interface opened successfully");

    /* Enable SO_BUSY_POLL on the SOEM raw socket.
     * This makes recvfrom() spin-poll the NIC driver instead of sleeping,
     * eliminating ~5-10us of scheduler wakeup latency per exchange. */
#ifdef ECAT_BUSY_POLL_US
    {
        int busy_us = ECAT_BUSY_POLL_US;
        int sockfd = inst->ecx_context.port.sockhandle;
        if (sockfd >= 0) {
            if (setsockopt(sockfd, SOL_SOCKET, SO_BUSY_POLL,
                           &busy_us, sizeof(busy_us)) == 0) {
                plugin_logger_info(logger,
                    "SO_BUSY_POLL enabled on socket (poll=%d us)", busy_us);
            } else {
                plugin_logger_debug(logger,
                    "SO_BUSY_POLL not supported (kernel may need CONFIG_NET_RX_BUSY_POLL)");
            }
        }
    }
#endif

    /* Step 2: Scan the bus and enumerate slaves */
    plugin_logger_info(logger, "Scanning EtherCAT bus...");

    if (ecx_config_init(&inst->ecx_context) <= 0) {
        plugin_logger_error(logger,
            "No EtherCAT slaves found on interface '%s'. "
            "Check cable connections and slave power.",
            config->master.interface);
        ecx_close(&inst->ecx_context);
        inst->soem_initialized = 0;
        return -1;
    }

    plugin_logger_info(logger, "Found %d slave(s) on the bus", inst->ecx_context.slavecount);

    /* Log discovered slaves */
    for (int i = 1; i <= inst->ecx_context.slavecount; i++) {
        ec_slavet *slave = &inst->ecx_context.slavelist[i];
        plugin_logger_info(logger,
            "  [%d] %s - vendor=0x%08X, product=0x%08X, rev=0x%08X",
            i, slave->name, slave->eep_man, slave->eep_id, slave->eep_rev);
    }

    /* Step 3: Validate topology against JSON configuration */
    if (ecat_master_validate_topology(inst, logger) != 0) {
        plugin_logger_error(logger,
            "Topology validation failed - aborting master initialization");
        ecx_close(&inst->ecx_context);
        inst->soem_initialized = 0;
        return -1;
    }

    /* Step 4: Wait for all slaves to reach PRE-OP state.
     * ecx_config_init() requests PRE-OP but does not wait for the
     * transition to complete. Slaves need to be in PRE-OP before
     * mailbox communication (SDO writes) can work.
     *
     * Use the maximum init_to_preop_timeout across all configured slaves. */
    plugin_logger_info(logger, "Waiting for slaves to reach PRE-OP state...");

    int max_preop_timeout_us = 0;
    for (int i = 0; i < config->slave_count; i++) {
        int t_us = config->slaves[i].timeouts.init_to_preop_timeout_ms * 1000;
        if (t_us > max_preop_timeout_us)
            max_preop_timeout_us = t_us;
    }
    if (max_preop_timeout_us == 0)
        max_preop_timeout_us = EC_TIMEOUTSTATE * 4;
    plugin_logger_debug(logger, "Using INIT->PRE-OP timeout: %d us", max_preop_timeout_us);

    ecx_statecheck(&inst->ecx_context, 0, EC_STATE_PRE_OP, max_preop_timeout_us);
    ecx_readstate(&inst->ecx_context);

    int all_preop = 1;
    for (int i = 1; i <= inst->ecx_context.slavecount; i++) {
        ec_slavet *slave = &inst->ecx_context.slavelist[i];
        if (slave->state < EC_STATE_PRE_OP) {
            plugin_logger_error(logger,
                "Slave %d (%s) failed to reach PRE-OP (state=0x%04X, ALstatus=0x%04X)",
                i, slave->name, slave->state, slave->ALstatuscode);
            all_preop = 0;
        }
    }

    if (!all_preop) {
        plugin_logger_error(logger, "Not all slaves reached PRE-OP - aborting");
        ecx_close(&inst->ecx_context);
        inst->soem_initialized = 0;
        return -1;
    }

    plugin_logger_info(logger, "All slaves in PRE-OP state");

    return 0;
}

/*
 * =============================================================================
 * Phase 2: SDO Configuration
 * =============================================================================
 */

int ecat_master_write_sdos(ecat_master_instance_t *inst, int slave_pos,
                           const ecat_sdo_config_t *sdos,
                           int sdo_count, int sdo_timeout_ms,
                           plugin_logger_t *logger)
{
    if (!inst->soem_initialized) {
        plugin_logger_error(logger, "Cannot write SDOs: SOEM not initialized");
        return -1;
    }

    if (slave_pos < 1 || slave_pos > inst->ecx_context.slavecount) {
        plugin_logger_error(logger, "Invalid slave position %d for SDO write", slave_pos);
        return -1;
    }

    if (sdo_count == 0)
        return 0;

    int written = 0;

    for (int i = 0; i < sdo_count; i++) {
        const ecat_sdo_config_t *sdo = &sdos[i];

        /* Parse index from hex string */
        uint16_t index = (uint16_t)strtol(sdo->index, NULL, 16);

        /* Determine data size from data type */
        ecat_data_type_t dt = sdo->parsed_type;
        int size = ecat_data_type_size(dt);
        if (size <= 0) {
            plugin_logger_warn(logger,
                "Slave %d SDO 0x%04X:%d: unknown data type '%s', assuming 4 bytes",
                slave_pos, index, sdo->subindex, sdo->data_type);
            size = 4;
            dt = ECAT_DTYPE_INT32;
        }

        /* Encode the double value into the correct wire type */
        uint8_t value_buf[8];
        memset(value_buf, 0, sizeof(value_buf));

        switch (dt) {
        case ECAT_DTYPE_BOOL:
        case ECAT_DTYPE_UINT8:  { uint8_t  v = (uint8_t)sdo->value;  memcpy(value_buf, &v, sizeof(v)); break; }
        case ECAT_DTYPE_INT8:   { int8_t   v = (int8_t)sdo->value;   memcpy(value_buf, &v, sizeof(v)); break; }
        case ECAT_DTYPE_UINT16: { uint16_t v = (uint16_t)sdo->value; memcpy(value_buf, &v, sizeof(v)); break; }
        case ECAT_DTYPE_INT16:  { int16_t  v = (int16_t)sdo->value;  memcpy(value_buf, &v, sizeof(v)); break; }
        case ECAT_DTYPE_UINT32: { uint32_t v = (uint32_t)sdo->value; memcpy(value_buf, &v, sizeof(v)); break; }
        case ECAT_DTYPE_INT32:  { int32_t  v = (int32_t)sdo->value;  memcpy(value_buf, &v, sizeof(v)); break; }
        case ECAT_DTYPE_UINT64: { uint64_t v = (uint64_t)sdo->value; memcpy(value_buf, &v, sizeof(v)); break; }
        case ECAT_DTYPE_INT64:  { int64_t  v = (int64_t)sdo->value;  memcpy(value_buf, &v, sizeof(v)); break; }
        case ECAT_DTYPE_REAL32: { float    v = (float)sdo->value;    memcpy(value_buf, &v, sizeof(v)); break; }
        case ECAT_DTYPE_REAL64: { double   v = sdo->value;           memcpy(value_buf, &v, sizeof(v)); break; }
        default:                { int32_t  v = (int32_t)sdo->value;  memcpy(value_buf, &v, sizeof(v)); break; }
        }

        if (dt == ECAT_DTYPE_REAL32 || dt == ECAT_DTYPE_REAL64) {
            plugin_logger_debug(logger,
                "Slave %d: writing SDO 0x%04X:%d = %g (%s, %d bytes)",
                slave_pos, index, sdo->subindex, sdo->value, sdo->data_type, size);
        } else {
            plugin_logger_debug(logger,
                "Slave %d: writing SDO 0x%04X:%d = %lld (%s, %d bytes)",
                slave_pos, index, sdo->subindex, (long long)(int64_t)sdo->value,
                sdo->data_type, size);
        }

        /* Use per-slave SDO timeout if configured, otherwise SOEM default */
        int sdo_timeout_us = (sdo_timeout_ms > 0) ? (sdo_timeout_ms * 1000) : EC_TIMEOUTRXM;

        int wkc = ecx_SDOwrite(&inst->ecx_context, (uint16)slave_pos,
                                index, sdo->subindex,
                                FALSE, size, value_buf, sdo_timeout_us);

        if (wkc <= 0) {
            plugin_logger_warn(logger,
                "Slave %d SDO 0x%04X:%d write failed (wkc=%d, name='%s')",
                slave_pos, index, sdo->subindex, wkc, sdo->name);
        } else {
            plugin_logger_debug(logger,
                "Slave %d SDO 0x%04X:%d write OK (name='%s')",
                slave_pos, index, sdo->subindex, sdo->name);
            written++;
        }
    }

    plugin_logger_info(logger, "Slave %d: %d/%d SDOs written successfully",
                       slave_pos, written, sdo_count);
    return written;
}

/*
 * =============================================================================
 * Phase 3: Process Data Mapping + Distributed Clocks
 * =============================================================================
 */

/**
 * @brief Configure watchdog timers for a single slave via register writes.
 *
 * EtherCAT watchdog registers (addressed by configured address):
 *   0x0400 (2 bytes) - Watchdog divider (default 0x09C2 = 2498)
 *   0x0402 (2 bytes) - PDI watchdog time (in watchdog divider ticks)
 *   0x0420 (2 bytes) - SM watchdog time  (in watchdog divider ticks)
 *
 * Default divider 0x09C2 = 2498 -> (2498+2)*25ns = 62.5us per tick.
 * To set watchdog to X ms: ticks = X * 1000 / 62.5 = X * 16
 *
 * @param slave_pos 1-based slave position on the bus
 * @param wd        Watchdog configuration
 * @param logger    Plugin logger instance
 */
static void ecat_master_configure_watchdog(ecat_master_instance_t *inst, int slave_pos,
                                           const ecat_watchdog_t *wd,
                                           plugin_logger_t *logger)
{
    /* Maximum watchdog timeout in ms that fits in a uint16_t register
     * with the default divider (1 tick = 62.5 us -> ticks = ms * 16).
     * 65535 / 16 = 4095.9 ms */
    const int max_watchdog_ms = UINT16_MAX / 16;

    uint16_t configadr = inst->ecx_context.slavelist[slave_pos].configadr;
    int wkc;

    /* SM watchdog register 0x0420 - only write if explicitly enabled. */
    if (wd->sm_watchdog_enabled) {
        uint16_t sm_wd_ticks = 0;
        if (wd->sm_watchdog_ms > 0) {
            int clamped_ms = wd->sm_watchdog_ms;
            if (clamped_ms > max_watchdog_ms) {
                plugin_logger_warn(logger,
                    "Slave %d: SM watchdog %d ms exceeds max %d ms, clamping",
                    slave_pos, wd->sm_watchdog_ms, max_watchdog_ms);
                clamped_ms = max_watchdog_ms;
            }
            sm_wd_ticks = (uint16_t)(clamped_ms * 16);
        }
        wkc = ecx_FPWR(&inst->ecx_context.port, configadr, 0x0420,
                            sizeof(sm_wd_ticks), &sm_wd_ticks, EC_TIMEOUTRET);
        if (wkc <= 0) {
            plugin_logger_warn(logger,
                "Slave %d: failed to write SM watchdog register 0x0420 (wkc=%d)",
                slave_pos, wkc);
        } else {
            plugin_logger_debug(logger,
                "Slave %d: SM watchdog enabled (ticks=%u, ~%d ms)",
                slave_pos, sm_wd_ticks, wd->sm_watchdog_ms);
        }
    } else {
        plugin_logger_debug(logger,
            "Slave %d: SM watchdog disabled, skipping register write",
            slave_pos);
    }

    /* PDI watchdog register 0x0402 - only write if explicitly enabled,
     * as many slaves do not support PDI watchdog and return wkc=0. */
    if (wd->pdi_watchdog_enabled) {
        uint16_t pdi_wd_ticks = 0;
        if (wd->pdi_watchdog_ms > 0) {
            int clamped_ms = wd->pdi_watchdog_ms;
            if (clamped_ms > max_watchdog_ms) {
                plugin_logger_warn(logger,
                    "Slave %d: PDI watchdog %d ms exceeds max %d ms, clamping",
                    slave_pos, wd->pdi_watchdog_ms, max_watchdog_ms);
                clamped_ms = max_watchdog_ms;
            }
            pdi_wd_ticks = (uint16_t)(clamped_ms * 16);
        }
        wkc = ecx_FPWR(&inst->ecx_context.port, configadr, 0x0402,
                        sizeof(pdi_wd_ticks), &pdi_wd_ticks, EC_TIMEOUTRET);
        if (wkc <= 0) {
            plugin_logger_warn(logger,
                "Slave %d: failed to write PDI watchdog register 0x0402 (wkc=%d)",
                slave_pos, wkc);
        } else {
            plugin_logger_debug(logger,
                "Slave %d: PDI watchdog enabled (ticks=%u, ~%d ms)",
                slave_pos, pdi_wd_ticks, wd->pdi_watchdog_ms);
        }
    } else {
        plugin_logger_debug(logger,
            "Slave %d: PDI watchdog disabled, skipping register write",
            slave_pos);
    }
}

/**
 * @brief Configure Distributed Clocks per slave based on JSON configuration.
 *
 * First calls ecx_configdc() to discover DC-capable slaves and measure
 * propagation delays.  Then for each slave with dc.enabled, configures
 * SYNC0 and/or SYNC1 signals.
 *
 * @param config Parsed EtherCAT configuration
 * @param logger Plugin logger instance
 */
static void ecat_master_configure_dc(ecat_master_instance_t *inst, plugin_logger_t *logger)
{
    const ecat_config_t *config = &inst->config;

    /* Step 1: Let SOEM discover DC-capable slaves and measure delays */
    plugin_logger_info(logger, "Configuring Distributed Clocks...");
    ecx_configdc(&inst->ecx_context);

    /* Step 2: Apply per-slave DC configuration */
    for (int i = 0; i < config->slave_count; i++) {
        const ecat_slave_t *slave = &config->slaves[i];
        int pos = slave->position;

        if (!slave->dc.enabled)
            continue;

        if (pos < 1 || pos > inst->ecx_context.slavecount) {
            plugin_logger_warn(logger,
                "Slave %d (%s): DC config skipped - position out of range",
                pos, slave->name);
            continue;
        }

        if (!inst->ecx_context.slavelist[pos].hasdc) {
            plugin_logger_warn(logger,
                "Slave %d (%s): DC config requested but slave has no DC support",
                pos, slave->name);
            continue;
        }

        /* Determine cycle time: use slave-specific or fall back to master cycle */
        uint32_t cycle_ns;
        if (slave->dc.sync_unit_cycle_us > 0) {
            cycle_ns = (uint32_t)(slave->dc.sync_unit_cycle_us * 1000);
        } else {
            cycle_ns = (uint32_t)(config->master.cycle_time_us * 1000);
        }

        if (slave->dc.sync0_enabled && slave->dc.sync1_enabled) {
            /* Both SYNC0 and SYNC1 */
            uint32_t cycle0_ns = (slave->dc.sync0_cycle_us > 0)
                ? (uint32_t)(slave->dc.sync0_cycle_us * 1000) : cycle_ns;
            uint32_t cycle1_ns = (slave->dc.sync1_cycle_us > 0)
                ? (uint32_t)(slave->dc.sync1_cycle_us * 1000) : cycle_ns;
            int32_t shift_ns = (int32_t)(slave->dc.sync0_shift_us * 1000);

            ecx_dcsync01(&inst->ecx_context, (uint16)pos, TRUE,
                         cycle0_ns, cycle1_ns, shift_ns);

            plugin_logger_info(logger,
                "Slave %d (%s): DC SYNC0+SYNC1 enabled "
                "(cycle0=%u ns, cycle1=%u ns, shift=%d ns)",
                pos, slave->name, cycle0_ns, cycle1_ns, shift_ns);

        } else if (slave->dc.sync0_enabled) {
            /* SYNC0 only */
            uint32_t sync0_ns = (slave->dc.sync0_cycle_us > 0)
                ? (uint32_t)(slave->dc.sync0_cycle_us * 1000) : cycle_ns;
            int32_t shift_ns = (int32_t)(slave->dc.sync0_shift_us * 1000);

            ecx_dcsync0(&inst->ecx_context, (uint16)pos, TRUE,
                        sync0_ns, shift_ns);

            plugin_logger_info(logger,
                "Slave %d (%s): DC SYNC0 enabled (cycle=%u ns, shift=%d ns)",
                pos, slave->name, sync0_ns, shift_ns);

        } else {
            /* DC enabled but no SYNC signals - just log it */
            plugin_logger_debug(logger,
                "Slave %d (%s): DC enabled but no SYNC signals configured",
                pos, slave->name);
        }
    }
}

int ecat_master_configure(ecat_master_instance_t *inst, plugin_logger_t *logger)
{
    const ecat_config_t *config = &inst->config;

    if (!inst->soem_initialized) {
        plugin_logger_error(logger, "Cannot configure: SOEM not initialized");
        return -1;
    }

    /* Step 4: Map process data (IO map) */
    plugin_logger_info(logger, "Mapping process data...");

    ecx_config_map_group(&inst->ecx_context, &inst->iomap, 0);

    ec_groupt *grp = &inst->ecx_context.grouplist[0];

    /* Check that total I/O fits in the IOmap buffer */
    uint32_t total_io = (uint32_t)grp->Obytes + (uint32_t)grp->Ibytes;
    if (total_io > ECAT_IOMAP_SIZE) {
        plugin_logger_error(logger, "IOmap overflow: need %u bytes, have %d",
                            total_io, ECAT_IOMAP_SIZE);
        return -1;
    }

    inst->iomap_used_size = (size_t)total_io;

    plugin_logger_info(logger, "IO map: %d output bytes, %d input bytes, %d segments",
                       grp->Obytes, grp->Ibytes, grp->nsegments);

    /* Step 5: Configure watchdogs per slave */
    plugin_logger_info(logger, "Configuring per-slave watchdogs...");
    for (int i = 0; i < config->slave_count; i++) {
        const ecat_slave_t *slave = &config->slaves[i];
        int pos = slave->position;
        if (pos >= 1 && pos <= inst->ecx_context.slavecount) {
            ecat_master_configure_watchdog(inst, pos, &slave->watchdog, logger);
        }
    }

    /* Step 6: Configure Distributed Clocks per slave */
    ecat_master_configure_dc(inst, logger);

    return 0;
}

/*
 * =============================================================================
 * Phase 4: Transition to OPERATIONAL
 * =============================================================================
 */

int ecat_master_transition_to_op(ecat_master_instance_t *inst, plugin_logger_t *logger)
{
    const ecat_config_t *config = &inst->config;

    if (!inst->soem_initialized) {
        plugin_logger_error(logger, "Cannot transition: SOEM not initialized");
        return -1;
    }

    /* Compute maximum SAFE-OP->OP timeout across all configured slaves */
    int max_safeop_timeout_us = 0;
    for (int i = 0; i < config->slave_count; i++) {
        int t_us = config->slaves[i].timeouts.safeop_to_op_timeout_ms * 1000;
        if (t_us > max_safeop_timeout_us)
            max_safeop_timeout_us = t_us;
    }
    if (max_safeop_timeout_us == 0)
        max_safeop_timeout_us = EC_TIMEOUTSTATE * 4;

    /* Step 6: Wait for SAFE_OP after config */
    plugin_logger_info(logger, "Waiting for SAFE_OP state...");
    plugin_logger_debug(logger, "Using SAFE-OP->OP timeout: %d us", max_safeop_timeout_us);

    ecx_statecheck(&inst->ecx_context, 0, EC_STATE_SAFE_OP, max_safeop_timeout_us);

    /* Read back actual states */
    ecx_readstate(&inst->ecx_context);
    if (inst->ecx_context.slavelist[0].state != EC_STATE_SAFE_OP) {
        plugin_logger_error(logger,
            "Not all slaves reached SAFE_OP state (current state: 0x%04X)",
            inst->ecx_context.slavelist[0].state);

        /* Log individual slave states for debugging */
        for (int i = 1; i <= inst->ecx_context.slavecount; i++) {
            ec_slavet *slave = &inst->ecx_context.slavelist[i];
            if (slave->state != EC_STATE_SAFE_OP) {
                plugin_logger_error(logger,
                    "  Slave %d (%s): state=0x%04X, ALstatuscode=0x%04X",
                    i, slave->name, slave->state, slave->ALstatuscode);
            }
        }

        return -1;
    }

    plugin_logger_info(logger, "All slaves in SAFE_OP state");

    /* Step 7: Send initial process data and request OPERATIONAL */
    plugin_logger_info(logger, "Requesting OPERATIONAL state...");

    /* Send one round of process data to make slave outputs happy */
    ecx_send_processdata(&inst->ecx_context);
    ecx_receive_processdata(&inst->ecx_context, EC_TIMEOUTRET);

    /* Request OP state */
    inst->ecx_context.slavelist[0].state = EC_STATE_OPERATIONAL;
    ecx_writestate(&inst->ecx_context, 0);

    /* Poll for OP state with process data exchange between checks */
    int op_reached = 0;
    int poll_timeout_us = max_safeop_timeout_us / ECAT_OP_POLL_RETRIES;
    if (poll_timeout_us < EC_TIMEOUTRET)
        poll_timeout_us = EC_TIMEOUTRET;

    for (int retry = 0; retry < ECAT_OP_POLL_RETRIES; retry++) {
        ecx_send_processdata(&inst->ecx_context);
        ecx_receive_processdata(&inst->ecx_context, EC_TIMEOUTRET);
        ecx_statecheck(&inst->ecx_context, 0, EC_STATE_OPERATIONAL, poll_timeout_us);

        if (inst->ecx_context.slavelist[0].state == EC_STATE_OPERATIONAL) {
            op_reached = 1;
            break;
        }
    }

    if (!op_reached) {
        plugin_logger_error(logger,
            "Not all slaves reached OPERATIONAL state after %d retries",
            ECAT_OP_POLL_RETRIES);

        /* Log individual slave states for debugging */
        ecx_readstate(&inst->ecx_context);
        for (int i = 1; i <= inst->ecx_context.slavecount; i++) {
            ec_slavet *slave = &inst->ecx_context.slavelist[i];
            if (slave->state != EC_STATE_OPERATIONAL) {
                plugin_logger_error(logger,
                    "  Slave %d (%s): state=0x%04X, ALstatuscode=0x%04X",
                    i, slave->name, slave->state, slave->ALstatuscode);
            }
        }

        return -1;
    }

    plugin_logger_info(logger, "EtherCAT master operational with %d slave(s)",
                       inst->ecx_context.slavecount);

    return 0;
}

/*
 * =============================================================================
 * Master Close
 * =============================================================================
 */

void ecat_master_close(ecat_master_instance_t *inst, plugin_logger_t *logger)
{
    if (inst->soem_initialized) {
        /* Transition all slaves to INIT state */
        plugin_logger_info(logger, "Transitioning slaves to INIT state...");
        inst->ecx_context.slavelist[0].state = EC_STATE_INIT;
        ecx_writestate(&inst->ecx_context, 0);

        /* Close the network interface */
        ecx_close(&inst->ecx_context);
        inst->soem_initialized = 0;
    }

    /* Always attempt to restore NIC settings, even if SOEM init had failed
     * after tune_nic() already modified the interface. The restore function
     * checks the saved flag internally and is a no-op when there is
     * nothing to undo. */
#if !defined(__CYGWIN__) && !defined(_WIN32)
    restore_nic_settings(inst->config.master.interface, logger);
#endif

    /* Clear IO map */
    memset(inst->iomap, 0, sizeof(inst->iomap));
    inst->iomap_used_size = 0;

    plugin_logger_info(logger, "EtherCAT master closed");
}

/*
 * =============================================================================
 * Process Data and State Access
 * =============================================================================
 */

int ecat_master_exchange_processdata(ecat_master_instance_t *inst, int timeout_us)
{
    ecx_send_processdata(&inst->ecx_context);
    int wkc = ecx_receive_processdata(&inst->ecx_context,
                                       (timeout_us > 0) ? timeout_us : EC_TIMEOUTRET);
    return wkc;
}

int ecat_master_get_expected_wkc(ecat_master_instance_t *inst)
{
    ec_groupt *grp = &inst->ecx_context.grouplist[0];
    return (grp->outputsWKC * 2) + grp->inputsWKC;
}

const ec_slavet *ecat_master_get_slave(ecat_master_instance_t *inst, int position)
{
    if (position < 1 || position > inst->ecx_context.slavecount)
        return NULL;
    return &inst->ecx_context.slavelist[position];
}

int ecat_master_is_operational(ecat_master_instance_t *inst)
{
    if (!inst->soem_initialized)
        return 0;
    return (inst->ecx_context.slavelist[0].state == EC_STATE_OPERATIONAL) ? 1 : 0;
}

uint16_t ecat_master_get_slave_state(ecat_master_instance_t *inst, int position)
{
    if (position < 1 || position > inst->ecx_context.slavecount)
        return 0;
    return inst->ecx_context.slavelist[position].state;
}

int ecat_master_request_state(ecat_master_instance_t *inst, int position, uint16_t state, plugin_logger_t *logger)
{
    if (position < 1 || position > inst->ecx_context.slavecount) {
        plugin_logger_error(logger, "Invalid slave position %d for state request", position);
        return -1;
    }

    inst->ecx_context.slavelist[position].state = state;
    ecx_writestate(&inst->ecx_context, (uint16)position);

    plugin_logger_debug(logger, "Requested state 0x%04X for slave %d", state, position);
    return 0;
}

/*
 * =============================================================================
 * Slave Recovery
 * =============================================================================
 */

int ecat_master_recover_slave(ecat_master_instance_t *inst, int position, plugin_logger_t *logger)
{
    if (position < 1 || position > inst->ecx_context.slavecount) {
        plugin_logger_error(logger, "Invalid slave position %d for recovery", position);
        return -1;
    }

    ec_slavet *slave = &inst->ecx_context.slavelist[position];
    uint16_t current_state = slave->state;

    if (current_state == EC_STATE_OPERATIONAL) {
        /* Already operational */
        return 1;
    }

    if (current_state == (EC_STATE_SAFE_OP + EC_STATE_ERROR)) {
        /* SAFE_OP + ERROR: ACK the error, then request OP */
        plugin_logger_info(logger,
            "Slave %d (%s): SAFE_OP+ERROR (ALstatus=0x%04X), sending ACK",
            position, slave->name, slave->ALstatuscode);

        slave->state = EC_STATE_SAFE_OP + EC_STATE_ACK;
        ecx_writestate(&inst->ecx_context, (uint16)position);

        /* Now request OP */
        slave->state = EC_STATE_OPERATIONAL;
        ecx_writestate(&inst->ecx_context, (uint16)position);

        /* Check if it worked */
        ecx_statecheck(&inst->ecx_context, (uint16)position,
                        EC_STATE_OPERATIONAL, EC_TIMEOUTRET);

        if (slave->state == EC_STATE_OPERATIONAL) {
            plugin_logger_info(logger, "Slave %d (%s): recovered to OP",
                               position, slave->name);
            return 1;
        }
        return 0;
    }

    if (current_state == EC_STATE_SAFE_OP) {
        /* SAFE_OP: just request OP */
        plugin_logger_info(logger, "Slave %d (%s): in SAFE_OP, requesting OP",
                           position, slave->name);

        slave->state = EC_STATE_OPERATIONAL;
        ecx_writestate(&inst->ecx_context, (uint16)position);

        ecx_statecheck(&inst->ecx_context, (uint16)position,
                        EC_STATE_OPERATIONAL, EC_TIMEOUTRET);

        if (slave->state == EC_STATE_OPERATIONAL) {
            plugin_logger_info(logger, "Slave %d (%s): recovered to OP",
                               position, slave->name);
            return 1;
        }
        return 0;
    }

    if (current_state > EC_STATE_NONE) {
        /* Lower state but still present: try full reconfiguration */
        plugin_logger_info(logger,
            "Slave %d (%s): state=0x%04X, attempting reconfig",
            position, slave->name, current_state);

        if (ecx_reconfig_slave(&inst->ecx_context, (uint16)position, EC_TIMEOUTRET)) {
            slave->islost = FALSE;
            plugin_logger_info(logger, "Slave %d (%s): reconfigured", position, slave->name);

            /* After reconfig, check if it reached OP */
            ecx_statecheck(&inst->ecx_context, (uint16)position,
                            EC_STATE_OPERATIONAL, EC_TIMEOUTRET);
            if (slave->state == EC_STATE_OPERATIONAL)
                return 1;
            return 0;
        }
        return 0;
    }

    /* EC_STATE_NONE: slave is lost, try recover */
    if (!slave->islost) {
        ecx_statecheck(&inst->ecx_context, (uint16)position,
                        EC_STATE_OPERATIONAL, EC_TIMEOUTRET);
        if (slave->state == EC_STATE_NONE) {
            slave->islost = TRUE;
            plugin_logger_warn(logger, "Slave %d (%s): marked as lost",
                               position, slave->name);
        }
        return 0;
    }

    /* Slave was marked lost - try to recover */
    if (ecx_recover_slave(&inst->ecx_context, (uint16)position, EC_TIMEOUTRET)) {
        slave->islost = FALSE;
        plugin_logger_info(logger, "Slave %d (%s): recovered from lost state",
                           position, slave->name);
        return 1;
    }

    return 0;
}

void ecat_master_read_states(ecat_master_instance_t *inst)
{
    if (inst->soem_initialized)
        ecx_readstate(&inst->ecx_context);
}

/*
 * =============================================================================
 * IOmap Access
 * =============================================================================
 */

uint8_t *ecat_master_get_iomap(ecat_master_instance_t *inst)
{
    if (!inst->soem_initialized)
        return NULL;
    return inst->iomap;
}

size_t ecat_master_get_iomap_size(ecat_master_instance_t *inst)
{
    return inst->iomap_used_size;
}

int ecat_master_get_slave_count(ecat_master_instance_t *inst)
{
    if (!inst->soem_initialized)
        return 0;
    return inst->ecx_context.slavecount;
}
