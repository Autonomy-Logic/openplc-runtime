// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file ethercat_iface_state.c
 * @brief Implementation of ethercat_iface_state.h.
 *
 * This module owns the NIC tuning the EtherCAT bus thread depends on:
 *   - ethtool -C  (interrupt coalescing: rx-usecs / tx-usecs = 0)
 *   - ethtool -K  (offload aggregation: GRO / GSO / TSO off)
 *
 * Both are runtime-only NIC settings (not persisted across reboots by
 * the OS), so the only correctness concern is making sure a graceful
 * shutdown — and a *crashed* runtime's next start — both restore them
 * to the values that were in effect before the master was brought up.
 * That's what the `/run/runtime/ecat_iface_<iface>.state` file is for:
 * we write the captured "before" values there, and the next apply call
 * checks for a leftover file and rolls back before re-applying.
 *
 * On non-Linux platforms apply/revert are no-ops; the SOEM raw socket
 * is the only thing that interacts with the NIC there.
 */

#include "ethercat_iface_state.h"
#include "ethercat_proc.h"
#include "ethercat_config.h"   /* ecat_iface_validate */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if !defined(__CYGWIN__) && !defined(_WIN32)

#include <ctype.h>
#include <errno.h>
#include <sys/stat.h>
#include <unistd.h>

#define ECAT_IFACE_STATE_DIR  "/run/runtime"
#define ECAT_IFACE_STATE_FMT  ECAT_IFACE_STATE_DIR "/ecat_iface_%s.state"

/* ------------------------------------------------------------------ */
/*  ethtool output parsing                                             */
/* ------------------------------------------------------------------ */

/*
 * Look for "<key>:" (not "<key>-something:") and read the integer that
 * follows.  Avoids matching "rx-usecs-irq" when searching for "rx-usecs".
 */
static int parse_ethtool_int(const char *output, const char *key, int *value)
{
    size_t key_len = strlen(key);
    const char *p = output;
    while ((p = strstr(p, key)) != NULL) {
        char next = p[key_len];
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

static int parse_ethtool_bool(const char *output, const char *key, int *value)
{
    const char *p = strstr(output, key);
    if (!p)
        return -1;
    p += strlen(key);
    while (*p == ':' || *p == ' ' || *p == '\t')
        p++;
    *value = (strncmp(p, "on", 2) == 0) ? 1 : 0;
    return 0;
}

/* ------------------------------------------------------------------ */
/*  NIC capture / apply / restore                                      */
/* ------------------------------------------------------------------ */

static void capture_nic_settings(ecat_iface_state_t *s, plugin_logger_t *logger)
{
    char output[2048];

    /* Coalescing (-c) */
    {
        char *argv[] = { "ethtool", "-c", (char *)s->iface, NULL };
        if (ecat_run_argv("ethtool", argv, output, sizeof(output)) == 0) {
            int ok = 0;
            ok += (parse_ethtool_int(output, "rx-usecs", &s->rx_usecs) == 0);
            ok += (parse_ethtool_int(output, "tx-usecs", &s->tx_usecs) == 0);
            if (ok > 0) {
                s->coalescing_saved = true;
                plugin_logger_info(logger,
                    "%s: saved coalescing (rx-usecs=%d, tx-usecs=%d)",
                    s->iface, s->rx_usecs, s->tx_usecs);
            }
        }
    }

    /* Offloads (-k) */
    {
        char *argv[] = { "ethtool", "-k", (char *)s->iface, NULL };
        if (ecat_run_argv("ethtool", argv, output, sizeof(output)) == 0) {
            int gro = 0, gso = 0, tso = 0;
            int ok = 0;
            ok += (parse_ethtool_bool(output, "generic-receive-offload", &gro) == 0);
            ok += (parse_ethtool_bool(output, "generic-segmentation-offload", &gso) == 0);
            ok += (parse_ethtool_bool(output, "tcp-segmentation-offload", &tso) == 0);
            if (ok > 0) {
                s->offloads_saved = true;
                s->gro = (gro != 0);
                s->gso = (gso != 0);
                s->tso = (tso != 0);
                plugin_logger_info(logger,
                    "%s: saved offloads (gro=%s, gso=%s, tso=%s)",
                    s->iface,
                    s->gro ? "on" : "off",
                    s->gso ? "on" : "off",
                    s->tso ? "on" : "off");
            }
        }
    }
}

static void apply_low_latency_nic(const char *iface, plugin_logger_t *logger)
{
    /* Disable IRQ coalescing -- deliver frames immediately */
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
}

static void restore_nic_settings(const ecat_iface_state_t *s, plugin_logger_t *logger)
{
    if (s->coalescing_saved) {
        char rx_str[16], tx_str[16];
        snprintf(rx_str, sizeof(rx_str), "%d", s->rx_usecs);
        snprintf(tx_str, sizeof(tx_str), "%d", s->tx_usecs);
        char *argv[] = {
            "ethtool", "-C", (char *)s->iface,
            "rx-usecs", rx_str,
            "tx-usecs", tx_str,
            NULL
        };
        if (ecat_run_argv("ethtool", argv, NULL, 0) == 0) {
            plugin_logger_info(logger,
                "%s: restored coalescing (rx-usecs=%d, tx-usecs=%d)",
                s->iface, s->rx_usecs, s->tx_usecs);
        }
    }

    if (s->offloads_saved) {
        char *argv[] = {
            "ethtool", "-K", (char *)s->iface,
            "gro", s->gro ? "on" : "off",
            "gso", s->gso ? "on" : "off",
            "tso", s->tso ? "on" : "off",
            NULL
        };
        if (ecat_run_argv("ethtool", argv, NULL, 0) == 0) {
            plugin_logger_info(logger,
                "%s: restored offloads (gro=%s, gso=%s, tso=%s)",
                s->iface,
                s->gro ? "on" : "off",
                s->gso ? "on" : "off",
                s->tso ? "on" : "off");
        }
    }
}

/* ------------------------------------------------------------------ */
/*  Persistence                                                        */
/* ------------------------------------------------------------------ */

static void state_path(const char *iface, char *buf, size_t size)
{
    snprintf(buf, size, ECAT_IFACE_STATE_FMT, iface);
}

static void persist_state(const ecat_iface_state_t *s, plugin_logger_t *logger)
{
    if (mkdir(ECAT_IFACE_STATE_DIR, 0755) != 0 && errno != EEXIST) {
        plugin_logger_warn(logger,
            "Cannot create %s: %s - iface state will not survive a crash",
            ECAT_IFACE_STATE_DIR, strerror(errno));
        return;
    }

    char path[160];
    state_path(s->iface, path, sizeof(path));

    char tmp[176];
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);

    FILE *fp = fopen(tmp, "w");
    if (!fp) {
        plugin_logger_warn(logger,
            "Cannot write %s: %s - iface state will not survive a crash",
            tmp, strerror(errno));
        return;
    }

    fprintf(fp, "iface=%s\n", s->iface);
    if (s->coalescing_saved) {
        fprintf(fp, "rx_usecs=%d\n", s->rx_usecs);
        fprintf(fp, "tx_usecs=%d\n", s->tx_usecs);
    }
    if (s->offloads_saved) {
        fprintf(fp, "gro=%s\n", s->gro ? "on" : "off");
        fprintf(fp, "gso=%s\n", s->gso ? "on" : "off");
        fprintf(fp, "tso=%s\n", s->tso ? "on" : "off");
    }
    fflush(fp);
    fclose(fp);

    if (rename(tmp, path) != 0) {
        plugin_logger_warn(logger, "Cannot rename %s -> %s: %s",
                           tmp, path, strerror(errno));
        unlink(tmp);
    }
}

static void remove_state_file(const char *iface)
{
    char path[160];
    state_path(iface, path, sizeof(path));
    unlink(path);
}

/*
 * Read /run/runtime/ecat_iface_<iface>.state into @p s.  Returns true if
 * a valid file was parsed.
 */
static bool load_state(const char *iface, ecat_iface_state_t *s)
{
    char path[160];
    state_path(iface, path, sizeof(path));
    FILE *fp = fopen(path, "r");
    if (!fp)
        return false;

    memset(s, 0, sizeof(*s));
    strncpy(s->iface, iface, sizeof(s->iface) - 1);
    s->iface[sizeof(s->iface) - 1] = '\0';

    char line[128];
    while (fgets(line, sizeof(line), fp)) {
        size_t len = strlen(line);
        if (len > 0 && line[len - 1] == '\n')
            line[len - 1] = '\0';
        char *eq = strchr(line, '=');
        if (!eq)
            continue;
        *eq = '\0';
        const char *key = line;
        const char *val = eq + 1;
        if (strcmp(key, "rx_usecs") == 0) {
            s->rx_usecs = atoi(val);
            s->coalescing_saved = true;
        } else if (strcmp(key, "tx_usecs") == 0) {
            s->tx_usecs = atoi(val);
            s->coalescing_saved = true;
        } else if (strcmp(key, "gro") == 0) {
            s->gro = (strcmp(val, "on") == 0);
            s->offloads_saved = true;
        } else if (strcmp(key, "gso") == 0) {
            s->gso = (strcmp(val, "on") == 0);
            s->offloads_saved = true;
        } else if (strcmp(key, "tso") == 0) {
            s->tso = (strcmp(val, "on") == 0);
            s->offloads_saved = true;
        }
    }
    fclose(fp);
    return true;
}

/* ------------------------------------------------------------------ */
/*  Crash recovery from the unified state file                         */
/* ------------------------------------------------------------------ */

static void recover_from_crash(const char *iface, plugin_logger_t *logger)
{
    ecat_iface_state_t prev;
    if (!load_state(iface, &prev))
        return;

    plugin_logger_warn(logger,
        "Found stale iface state for %s "
        "(coalescing_saved=%d, offloads_saved=%d) - "
        "previous process likely crashed.  Reverting before re-applying.",
        iface,
        (int)prev.coalescing_saved, (int)prev.offloads_saved);

    restore_nic_settings(&prev, logger);
    remove_state_file(iface);
}

/* ------------------------------------------------------------------ */
/*  Public API (Linux)                                                 */
/* ------------------------------------------------------------------ */

void ecat_iface_state_apply(ecat_iface_state_t *state, const char *iface,
                            plugin_logger_t *logger)
{
    if (!state || !iface || iface[0] == '\0')
        return;

    memset(state, 0, sizeof(*state));

    if (!ecat_iface_validate(iface, ECAT_IFACE_LINUX_STRICT)) {
        plugin_logger_warn(logger,
            "iface '%s' not a valid Linux interface name -- "
            "NIC tuning (ethtool) is disabled for this master.  "
            "Jitter may be higher.",
            iface);
        return;
    }

    strncpy(state->iface, iface, sizeof(state->iface) - 1);
    state->iface[sizeof(state->iface) - 1] = '\0';

    /* Recover from a crash of the current-format file */
    recover_from_crash(iface, logger);

    /* Capture the live NIC settings before we change them */
    capture_nic_settings(state, logger);

    /* Apply low-latency NIC tuning */
    apply_low_latency_nic(iface, logger);

    /* Persist whatever we captured/applied so a crash can roll it back */
    persist_state(state, logger);
}

void ecat_iface_state_revert(ecat_iface_state_t *state, plugin_logger_t *logger)
{
    if (!state || state->iface[0] == '\0')
        return;

    /* Only undo what we applied, in reverse order */
    restore_nic_settings(state, logger);
    state->coalescing_saved = false;
    state->offloads_saved = false;

    remove_state_file(state->iface);
}

#else /* MSYS2 / Cygwin / native Windows */

void ecat_iface_state_apply(ecat_iface_state_t *state, const char *iface,
                            plugin_logger_t *logger)
{
    (void)state;
    (void)iface;
    (void)logger;
}

void ecat_iface_state_revert(ecat_iface_state_t *state, plugin_logger_t *logger)
{
    (void)state;
    (void)logger;
}

#endif
