// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file ethercat_plugin.c
 * @brief EtherCAT client plugin: relays process data between EtherDOG and the image tables.
 *
 * The EtherCAT master is EtherDOG, supervised by the webserver:
 *   start_loop: connect, load the bus configuration, start the bus, read the layout, bind it
 *               to ethercat_iomapping.json, open the data session, spawn the relay thread
 *   relay:      per input frame, publish %I through the journal, then read %Q under
 *               image_lock and answer with an output frame
 *   stop_loop:  stop the relay, close the session and stop the bus
 * If EtherDOG goes quiet (restart, crash) the relay reconnects on its own; plc_main keeps
 * running with the last inputs meanwhile and EtherDOG drives the outputs to the safe state.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <errno.h>
#include <libgen.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "cJSON.h"
#include "etherdog_link.h"
#include "ethercat_iomap.h"
#include "plugin_logger.h"
#include "plugin_types.h"

/* link_up result: the program's configuration cannot run on this bus, so retrying cannot help. */
#define LINK_CONFIG_ERROR (-3)

/* Used when the bus configuration gives no task_priority; EtherDOG's own default is 90. */
#define DEFAULT_RELAY_PRIORITY 90
/* Below the runtime's dispatcher (PLC_FIFO_DISPATCHER, 98) and watchdog (99), task_policy.h. */
#define MAX_RELAY_PRIORITY 97
#define RECV_TIMEOUT_MS 100
#define SILENCE_RECONNECT_MS 1000
#define RECONNECT_BACKOFF_MS 1000
#define START_TIMEOUT_MS 60000

static plugin_logger_t g_logger;
static plugin_runtime_args_t g_args;
static char g_session_file[256] = EDL_SESSION_FILE;
static ecat_iomap_t g_map;
static ecat_bound_map_t g_bound;
static edl_link_t g_link;
static bool g_linked = false;
static bool g_have_map = false;
static int g_relay_priority = DEFAULT_RELAY_PRIORITY;

/* Last input flags per master, to log bus state changes once. -1: none seen yet. */
static int g_last_flags[EDL_MAX_MASTERS];

static pthread_t g_relay;
static atomic_bool g_running = false;
static bool g_relay_started = false;

/* ----------------------------------------------------------------------------------------- */

static void sleep_ms(int ms)
{
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

/* Returns 0 with *resp heap-allocated (caller frees), or -1. */
static int call_json(edl_link_t *link, const char *command, char **resp, int timeout)
{
    char req[64];
    snprintf(req, sizeof(req), "{\"command\":\"%s\"}", command);
    return edl_call(link, req, resp, timeout);
}

/* Load the bus configuration; the bus starts only with the program, so both always match. */
static int configure_bus(const edl_session_t *session, char *err, size_t err_size)
{
    cJSON *req = cJSON_CreateObject();
    cJSON_AddStringToObject(req, "command", "configure");
    if (session->busconfig[0] != '\0')
        cJSON_AddStringToObject(cJSON_AddObjectToObject(req, "params"), "path", session->busconfig);
    char *line = cJSON_PrintUnformatted(req);
    cJSON_Delete(req);
    char *resp = NULL;
    int rc = line ? edl_call(&g_link, line, &resp, 15000) : -1;
    free(line);
    if (rc != 0) {
        snprintf(err, err_size, "no reply from EtherDOG to 'configure'");
        return -1;
    }

    cJSON *root = cJSON_Parse(resp);
    const cJSON *e = root ? cJSON_GetObjectItemCaseSensitive(root, "error") : NULL;
    const cJSON *masters = root ? cJSON_GetObjectItemCaseSensitive(root, "masters") : NULL;
    rc = 0;
    if (cJSON_IsString(e)) {
        /* Still running from before a reconnect: it already holds this program's configuration */
        if (strstr(e->valuestring, "running") == NULL) {
            snprintf(err, err_size, "EtherDOG rejected the bus configuration: %.300s",
                     e->valuestring);
            rc = LINK_CONFIG_ERROR;
        }
    } else if (cJSON_IsArray(masters)) {
        int priority = 0;
        const cJSON *m;
        cJSON_ArrayForEach(m, masters)
        {
            const cJSON *p = cJSON_GetObjectItemCaseSensitive(m, "task_priority");
            if (cJSON_IsNumber(p) && p->valueint > priority)
                priority = p->valueint;
        }
        g_relay_priority = priority < 1                    ? DEFAULT_RELAY_PRIORITY
                           : priority > MAX_RELAY_PRIORITY ? MAX_RELAY_PRIORITY
                                                           : priority;
    }
    cJSON_Delete(root);
    free(resp);
    return rc;
}

/* Masters EtherDOG runs that the mapping does not mention: their data is ignored. */
static void warn_unmapped_masters(const cJSON *layout)
{
    const cJSON *masters = cJSON_GetObjectItemCaseSensitive(layout, "masters");
    const cJSON *m;
    cJSON_ArrayForEach(m, masters)
    {
        const cJSON *idx = cJSON_GetObjectItemCaseSensitive(m, "index");
        const cJSON *name = cJSON_GetObjectItemCaseSensitive(m, "name");
        if (cJSON_IsNumber(idx) && idx->valueint >= 0 && idx->valueint < ECAT_IOMAP_MAX_MASTERS &&
            !g_bound.masters[idx->valueint].active)
            plugin_logger_warn(&g_logger,
                               "EtherCAT master '%s' has no I/O mapping; its data is ignored",
                               cJSON_IsString(name) ? name->valuestring : "?");
    }
}

/* Bring the link up: connect, configure and start the bus, bind the layout, open the data session. */
static int link_up(char *err, size_t err_size)
{
    edl_session_t session;
    int session_rc = edl_read_session(g_session_file, &session, err, err_size);
    if (session_rc != 0)
        return session_rc;
    if (edl_connect(&g_link, &session, err, err_size) != 0)
        return -1;

    char *resp = NULL;
    cJSON *layout = NULL;
    int rc = configure_bus(&session, err, err_size);
    if (rc != 0)
        goto fail;
    rc = -1;
    if (call_json(&g_link, "start", &resp, START_TIMEOUT_MS) != 0) {
        snprintf(err, err_size, "no reply from EtherDOG to 'start'");
        goto fail;
    }
    if (strstr(resp, "\"error\"") != NULL) {
        snprintf(err, err_size, "EtherDOG could not start the bus: %.300s", resp);
        goto fail;
    }
    free(resp);
    resp = NULL;
    if (call_json(&g_link, "layout", &resp, 5000) != 0) {
        snprintf(err, err_size, "no reply from EtherDOG to 'layout'");
        goto fail;
    }

    layout = cJSON_Parse(resp);
    if (layout == NULL) {
        snprintf(err, err_size, "EtherDOG layout reply is not JSON");
        goto fail;
    }
    int bind_rc = ecat_iomap_bind(&g_map, layout, &g_args, &g_bound, err, err_size);
    if (bind_rc != 0) {
        rc = bind_rc == ECAT_IOMAP_CONFIG_ERROR ? LINK_CONFIG_ERROR : -1;
        goto fail;
    }
    warn_unmapped_masters(layout);
    for (int i = 0; i < g_bound.not_ready_count; i++)
        plugin_logger_warn(&g_logger,
                           "EtherCAT master '%s' is not operational; its I/O stays off until the "
                           "bus restarts",
                           g_bound.not_ready[i]);
    cJSON_Delete(layout);
    layout = NULL;
    free(resp);
    resp = NULL;

    char dir_buf[256];
    snprintf(dir_buf, sizeof(dir_buf), "%s", g_session_file);
    if (edl_open_data(&g_link, &session, dirname(dir_buf), err, err_size) != 0)
        goto fail;

    for (int i = 0; i < ECAT_IOMAP_MAX_MASTERS; i++) {
        const ecat_bound_master_t *m = &g_bound.masters[i];
        if (m->active)
            plugin_logger_info(&g_logger,
                               "Master %d: %d input(s), %d output(s) bound (image %u/%u bytes)", i,
                               m->input_count, m->output_count, m->input_bytes, m->output_bytes);
    }
    for (int i = 0; i < EDL_MAX_MASTERS; i++)
        g_last_flags[i] = -1;
    g_linked = true;
    return 0;

fail:
    cJSON_Delete(layout);
    free(resp);
    resp = NULL;
    /* The bus was started for a program that cannot use it: stop it before giving up. */
    if (rc == LINK_CONFIG_ERROR && g_link.ctl_fd >= 0) {
        call_json(&g_link, "stop", &resp, 10000);
        free(resp);
    }
    edl_close(&g_link);
    g_linked = false;
    return rc;
}

static void link_down(bool stop_bus)
{
    if (g_link.ctl_fd >= 0) {
        char *resp = NULL;
        call_json(&g_link, "close_data", &resp, 2000);
        free(resp);
        resp = NULL;
        if (stop_bus)
            call_json(&g_link, "stop", &resp, 10000);
        free(resp);
    }
    edl_close(&g_link);
    g_linked = false;
}

/* Link setup (JSON parsing, allocation, blocking calls) runs at normal scheduling. */
static void drop_relay_priority(void)
{
    struct sched_param sp = { .sched_priority = 0 };
    int rc = pthread_setschedparam(pthread_self(), SCHED_OTHER, &sp);
    if (rc != 0)
        plugin_logger_warn(&g_logger, "relay: cannot return to SCHED_OTHER: %s", strerror(rc));
}

static void apply_relay_priority(void)
{
    struct sched_param sp = { .sched_priority = g_relay_priority };
    int rc = pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp);
    if (rc != 0)
        plugin_logger_warn(&g_logger, "relay: SCHED_FIFO(%d) unavailable: %s", g_relay_priority,
                           strerror(rc));
}

/* Logs a master's bus state change; the program keeps running on the last inputs. */
static void track_bus_state(int master, uint8_t flags)
{
    int state = flags & (EDL_FLAG_VALID | EDL_FLAG_WKC_OK);
    int last = g_last_flags[master];
    g_last_flags[master] = state;
    if (last == state)
        return;
    if (!(state & EDL_FLAG_VALID)) {
        plugin_logger_warn(&g_logger,
                           "EtherCAT master %d: bus not operational; inputs keep their last values",
                           master);
    } else if (!(state & EDL_FLAG_WKC_OK)) {
        plugin_logger_warn(&g_logger, "EtherCAT master %d: working counter mismatch", master);
    } else if (last != -1) {
        plugin_logger_info(&g_logger, "EtherCAT master %d: bus operational again", master);
    }
}

static uint64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

/* The first bound master that has sent no valid frame for SILENCE_RECONNECT_MS, or -1. */
static int silent_master(const uint64_t *last_rx, uint64_t now)
{
    for (int i = 0; i < ECAT_IOMAP_MAX_MASTERS; i++)
        if (g_bound.masters[i].active && now - last_rx[i] >= SILENCE_RECONNECT_MS)
            return i;
    return -1;
}

/* A configuration that cannot bind will not bind on retry: stop the PLC with the reason. */
static void fail_configuration(const char *err)
{
    plugin_logger_error(&g_logger, "EtherCAT configuration error: %s", err);
    if (g_args.request_plc_stop) {
        char reason[600];
        snprintf(reason, sizeof(reason), "EtherCAT configuration error: %s", err);
        g_args.request_plc_stop(reason);
    }
}

static void *relay_thread(void *arg)
{
    (void)arg;
    pthread_setname_np(pthread_self(), "ecat-relay");
    bool realtime = false;

    uint8_t frame[EDL_FRAME_HEADER + EDL_MAX_PAYLOAD];
    uint8_t outputs[EDL_MAX_PAYLOAD];
    uint64_t last_rx[ECAT_IOMAP_MAX_MASTERS] = { 0 };
    char err[512];
    char last_err[512] = "";
    bool reported = false;

    while (atomic_load(&g_running)) {
        if (!g_linked) {
            if (realtime) {
                drop_relay_priority();
                realtime = false;
            }
            int rc = link_up(err, sizeof(err));
            if (rc == LINK_CONFIG_ERROR) {
                fail_configuration(err);
                break;
            }
            if (rc != 0) {
                /* Each distinct reason once */
                if (strcmp(err, last_err) != 0) {
                    if (rc == EDL_DISABLED)
                        plugin_logger_warn(&g_logger, "EtherCAT disabled: %s", err);
                    else
                        plugin_logger_warn(&g_logger, "EtherDOG link down, retrying: %s", err);
                    snprintf(last_err, sizeof(last_err), "%s", err);
                }
                reported = true;
                sleep_ms(RECONNECT_BACKOFF_MS);
                continue;
            }
            last_err[0] = '\0';
            plugin_logger_info(&g_logger, "EtherDOG link %s", reported ? "restored" : "up");
            reported = false;
            uint64_t now = now_ms();
            for (int i = 0; i < ECAT_IOMAP_MAX_MASTERS; i++)
                last_rx[i] = now;
            apply_relay_priority();
            realtime = true;
        }

        int master = 0;
        uint8_t flags = 0;
        const uint8_t *payload = NULL;
        size_t len = 0;
        int rc = edl_recv_inputs(&g_link, frame, sizeof(frame), RECV_TIMEOUT_MS, &master, &flags,
                                 &payload, &len);
        const ecat_bound_master_t *m = rc > 0 ? &g_bound.masters[master] : NULL;
        if (m != NULL && m->active)
            last_rx[master] = now_ms();

        int silent = rc < 0 ? -1 : silent_master(last_rx, now_ms());
        if (rc < 0 || silent >= 0) {
            if (rc < 0)
                plugin_logger_warn(&g_logger,
                                   "EtherCAT link to EtherDOG lost (socket error); inputs keep "
                                   "their last values, reconnecting");
            else
                plugin_logger_warn(&g_logger,
                                   "EtherCAT master %d sent no data for 1 s; inputs keep their last "
                                   "values, reconnecting",
                                   silent);
            link_down(false);
            reported = true;
            continue;
        }
        if (m == NULL || !m->active)
            continue; /* no frame, or unmapped (warned at link up); EtherDOG holds its outputs at zero */
        track_bus_state(master, flags);

        if (flags & EDL_FLAG_VALID)
            ecat_iomap_publish_inputs(m, payload, len, &g_args);

        g_args.image_lock();
        ecat_iomap_collect_outputs(m, outputs, m->output_bytes);
        g_args.image_unlock();
        edl_send_outputs(&g_link, master, outputs, m->output_bytes, true);
    }
    return NULL;
}

/* --- plugin entry points ------------------------------------------------------------------- */

int init(void *args)
{
    plugin_logger_init(&g_logger, "ETHERCAT", args);
    if (args == NULL) {
        plugin_logger_error(&g_logger, "init args is NULL");
        return -1;
    }
    memcpy(&g_args, args, sizeof(g_args));

    const char *override = getenv("ETHERDOG_SESSION_FILE");
    if (override != NULL && override[0] != '\0')
        snprintf(g_session_file, sizeof(g_session_file), "%s", override);

    edl_init(&g_link);

    /* Initialized even when disabled (for command forwarding): no mapping file is not an error. */
    const char *path = g_args.plugin_specific_config_file_path;
    if (access(path, R_OK) != 0) {
        plugin_logger_debug(&g_logger, "no I/O mapping at %s", path);
        return 0;
    }

    char err[512];
    if (ecat_iomap_load(path, &g_map, err, sizeof(err)) != 0) {
        plugin_logger_error(&g_logger, "%s", err);
        return -1;
    }
    g_have_map = true;
    int total = 0;
    for (int i = 0; i < g_map.master_count; i++)
        total += g_map.masters[i].entry_count;
    plugin_logger_info(&g_logger, "I/O mapping loaded: %d master(s), %d entries", g_map.master_count,
                       total);
    return 0;
}

int start_loop(void)
{
    if (g_relay_started)
        return 0;
    if (!g_have_map) {
        plugin_logger_error(&g_logger, "no I/O mapping loaded (ethercat_iomapping.json)");
        return -1;
    }
    if (!g_args.image_lock || !g_args.image_unlock || !g_args.journal_write_bool ||
        !g_args.journal_write_byte || !g_args.journal_write_int || !g_args.journal_write_dint ||
        !g_args.journal_write_lint) {
        plugin_logger_error(&g_logger, "runtime did not provide the image/journal entry points");
        return -1;
    }

    /* The relay brings the link up and retries until EtherDOG is ready */
    atomic_store(&g_running, true);
    int rc = pthread_create(&g_relay, NULL, relay_thread, NULL);
    if (rc != 0) {
        plugin_logger_error(&g_logger, "cannot create relay thread: %s", strerror(rc));
        atomic_store(&g_running, false);
        return -1;
    }
    g_relay_started = true;
    plugin_logger_info(&g_logger, "EtherCAT relay running");
    return 0;
}

void stop_loop(void)
{
    if (!g_relay_started)
        return;
    atomic_store(&g_running, false);
    pthread_join(g_relay, NULL);
    g_relay_started = false;

    /* The relay's link may be down; stop the bus over a fresh connection if so. */
    if (!g_linked) {
        edl_session_t session;
        char err[256];
        if (edl_read_session(g_session_file, &session, err, sizeof(err)) == 0 &&
            edl_connect(&g_link, &session, err, sizeof(err)) == 0)
            g_linked = true;
    }
    link_down(true);
    plugin_logger_info(&g_logger, "EtherCAT relay stopped");
}

void cleanup(void)
{
    stop_loop();
}

/**
 * Forward a command (scan, test, status, diagnostics, list-interfaces) to EtherDOG over its
 * own short connection, so callers that still route EtherCAT commands through plc_main work.
 */
int execute_command(const char *command_json, char *response, size_t response_size)
{
    edl_session_t session;
    edl_link_t link;
    char err[512];
    edl_init(&link);
    if (edl_read_session(g_session_file, &session, err, sizeof(err)) != 0 ||
        edl_connect(&link, &session, err, sizeof(err)) != 0) {
        cJSON *resp = cJSON_CreateObject();
        cJSON_AddStringToObject(resp, "error", err);
        char *text = cJSON_PrintUnformatted(resp);
        snprintf(response, response_size, "%s", text ? text : "{\"error\":\"EtherDOG unavailable\"}");
        free(text);
        cJSON_Delete(resp);
        return -1;
    }
    char *reply = NULL;
    int rc = edl_call(&link, command_json, &reply, 30000);
    edl_close(&link);
    if (rc != 0) {
        snprintf(response, response_size, "{\"error\":\"no reply from EtherDOG\"}");
        return -1;
    }
    size_t len = strlen(reply);
    if (len >= response_size) {
        snprintf(response, response_size,
                 "{\"error\":\"EtherDOG reply is %zu bytes, larger than the %zu-byte buffer\"}",
                 len, response_size);
        free(reply);
        return -1;
    }
    memcpy(response, reply, len + 1);
    free(reply);
    return strstr(response, "\"error\"") != NULL ? -1 : 0;
}
