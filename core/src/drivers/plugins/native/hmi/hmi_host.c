// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file hmi_host.c
 * @brief HMI plugin of Runtime v4: serves PLC values to the browser viewers the web server opens
 *        over a Unix socket and applies their writes (docs/HMI.md, "Host" and "Wire protocol").
 *        Screens run in the browser; the host runs no screen code. It serves a viewer only the
 *        tags of the screen it shows, when its role may open that screen, and applies only the
 *        writes that screen makes, after checking their type and range, from the build's
 *        manifest (hmi_access.h), never from what a viewer sends.
 *
 * Threads, both at the default priority:
 * - the I/O thread owns the socket: it accepts the web server, decodes its messages, opens and
 *   closes viewers, queues each viewer's SHOW and WRITE requests and sends control messages and
 *   VALUES without blocking;
 * - the value thread, every hmi_access_period_ms: applies each viewer's queued requests in order,
 *   reads the tags of the screen it shows and offers the changed values to its pending message.
 * g_lock guards the viewer list, each viewer's request queue and values, and the control queue.
 * It is never held while reading or writing PLC variables or sending.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/syscall.h>
#endif

#include "hmi_access.h"
#include "hmi_codec.h"
#include "hmi_values.h"
#include "hmi_wire.h"
#include "plugin_types.h"

/* The socket lives in a directory only this process's user can enter, so its own mode does not
 * matter. */
#ifndef HMI_SOCKET_DIR
#define HMI_SOCKET_DIR "/run/runtime/hmi"
#endif
#ifndef HMI_SOCKET_PATH
#define HMI_SOCKET_PATH HMI_SOCKET_DIR "/hmi.socket"
#endif

#define HMI_EXPORT __attribute__((visibility("default")))

#define PERIOD_MS_DEFAULT 100u
#define PERIOD_MS_MIN 10u
#define PERIOD_MS_MAX 10000u
/** Control bytes queued for the web server above which the host stops reading its messages. */
#define CONTROL_SOFT_MAX (64u * 1024u)
#define READ_CHUNK 4096u
#define LOG_LINE 768u
/** SHOW and WRITE requests one viewer may have queued, and their bytes. */
#define REQUEST_QUEUE_CAP 128u
#define REQUEST_QUEUE_BYTES (64u * 1024u)
/** Bytes of a queued request before its value: kind, id, length. */
#define REQUEST_HEADER 5u
/** The debugger's success status for a write. */
#define DEBUG_WRITE_OK 0x7E

#define SAME_VALUE(a, b) ((int)(a) == (int)(b))
_Static_assert(SAME_VALUE(HMI_CODEC_BOOL, HMI_ACCESS_BOOL) &&
                   SAME_VALUE(HMI_CODEC_SINT, HMI_ACCESS_SINT) &&
                   SAME_VALUE(HMI_CODEC_INT, HMI_ACCESS_INT) &&
                   SAME_VALUE(HMI_CODEC_DINT, HMI_ACCESS_DINT) &&
                   SAME_VALUE(HMI_CODEC_LINT, HMI_ACCESS_LINT) &&
                   SAME_VALUE(HMI_CODEC_USINT, HMI_ACCESS_USINT) &&
                   SAME_VALUE(HMI_CODEC_UINT, HMI_ACCESS_UINT) &&
                   SAME_VALUE(HMI_CODEC_UDINT, HMI_ACCESS_UDINT) &&
                   SAME_VALUE(HMI_CODEC_ULINT, HMI_ACCESS_ULINT) &&
                   SAME_VALUE(HMI_CODEC_BYTE, HMI_ACCESS_BYTE) &&
                   SAME_VALUE(HMI_CODEC_WORD, HMI_ACCESS_WORD) &&
                   SAME_VALUE(HMI_CODEC_DWORD, HMI_ACCESS_DWORD) &&
                   SAME_VALUE(HMI_CODEC_LWORD, HMI_ACCESS_LWORD) &&
                   SAME_VALUE(HMI_CODEC_REAL, HMI_ACCESS_REAL) &&
                   SAME_VALUE(HMI_CODEC_LREAL, HMI_ACCESS_LREAL) &&
                   SAME_VALUE(HMI_CODEC_STRING, HMI_ACCESS_STRING) &&
                   SAME_VALUE(HMI_CODEC_TIME, HMI_ACCESS_TIME) &&
                   SAME_VALUE(HMI_CODEC_LTIME, HMI_ACCESS_LTIME) &&
                   SAME_VALUE(HMI_CODEC_DATE, HMI_ACCESS_DATE) &&
                   SAME_VALUE(HMI_CODEC_TOD, HMI_ACCESS_TOD) &&
                   SAME_VALUE(HMI_CODEC_DT, HMI_ACCESS_DT),
               "hmi_codec_type_t must mirror hmi_access_type");

typedef enum
{
    REQUEST_SHOW  = HMI_MSG_SHOW,
    REQUEST_WRITE = HMI_MSG_WRITE,
    REQUEST_BIT   = HMI_MSG_BIT
} request_kind_t;

typedef struct viewer
{
    struct viewer *next;
    uint32_t id;
    bool closing; /**< closed or dropped; the value thread frees it */
    uint8_t role;
    char account[HMI_WIRE_NAME_MAX + 1];

    /* Under g_lock. */
    hmi_buf_t requests; /**< queued SHOW and WRITE requests: kind, u16 id, u16 length, value */
    size_t request_count;
    hmi_values_t values;
    /** Page whose tags the viewer is served, -1 for none; set by the value thread. */
    int32_t page;
    /** Writes the value thread has processed, applied or refused. */
    uint32_t writes;
    /** A VALUES message must be sent even without a changed value (a new page, a write). */
    bool announce;
} viewer_t;

static plugin_runtime_args_t g_args;
static bool g_have_args;
static bool g_live;
static uint32_t g_period_ms = PERIOD_MS_DEFAULT;
static hmi_values_layout_t g_layout;
/** Largest buffer-format value of any tag. */
static size_t g_value_max = 1;

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_cond;
static bool g_cond_ready;
static viewer_t *g_viewers;
static size_t g_open_count;
static hmi_buf_t g_control;
static bool g_values_pending;
static bool g_stop;

static bool g_running;
static pthread_t g_io_thread;
static pthread_t g_value_thread;
static int g_listen_fd = -1;
static int g_wake_rd   = -1;
static int g_wake_wr   = -1;

/* ---------------------------------------------------------------------------------------- */
/* Logging                                                                                   */
/* ---------------------------------------------------------------------------------------- */

typedef enum
{
    LOG_INFO,
    LOG_WARN,
    LOG_ERROR
} log_level_t;

static void hmi_log(log_level_t level, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

static void hmi_log(log_level_t level, const char *fmt, ...)
{
    char line[LOG_LINE];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);

    plugin_log_info_func_t fn = NULL;
    if (g_have_args)
    {
        fn = level == LOG_ERROR  ? g_args.log_error
             : level == LOG_WARN ? g_args.log_warn
                                 : g_args.log_info;
    }
    if (fn)
        fn("[HMI] %s", line);
    else
        fprintf(stderr, "[HMI] %s\n", line);
}

static const char *role_name(uint8_t role)
{
    static const char *const names[] = {"admin", "engineer", "operator", "viewer"};
    return role < 4 ? names[role] : "?";
}

static const char *tag_path(int tag)
{
    if (tag < 0 || tag >= hmi_access_tag_count || !hmi_access_tags[tag].path)
        return "?";
    return hmi_access_tags[tag].path;
}

/* ---------------------------------------------------------------------------------------- */
/* Time and waking                                                                           */
/* ---------------------------------------------------------------------------------------- */

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000u + (uint64_t)ts.tv_nsec;
}

static int cond_init(void)
{
    pthread_condattr_t attr;
    if (pthread_condattr_init(&attr) != 0)
        return -1;
#ifdef __linux__
    if (pthread_condattr_setclock(&attr, CLOCK_MONOTONIC) != 0)
    {
        pthread_condattr_destroy(&attr);
        return -1;
    }
#endif
    int rc = pthread_cond_init(&g_cond, &attr);
    pthread_condattr_destroy(&attr);
    return rc == 0 ? 0 : -1;
}

/** Waits on g_cond (g_lock held) until the monotonic deadline_ns at the latest. */
static void cond_wait_until(uint64_t deadline_ns)
{
    struct timespec ts;
#ifdef __linux__
    ts.tv_sec  = (time_t)(deadline_ns / 1000000000u);
    ts.tv_nsec = (long)(deadline_ns % 1000000000u);
#else
    const uint64_t now = now_ns();
    const uint64_t rel = deadline_ns > now ? deadline_ns - now : 0;
    struct timespec real;
    clock_gettime(CLOCK_REALTIME, &real);
    uint64_t abs_ns = (uint64_t)real.tv_sec * 1000000000u + (uint64_t)real.tv_nsec + rel;
    ts.tv_sec       = (time_t)(abs_ns / 1000000000u);
    ts.tv_nsec      = (long)(abs_ns % 1000000000u);
#endif
    (void)pthread_cond_timedwait(&g_cond, &g_lock, &ts);
}

static void wake_io(void)
{
    const uint8_t b = 1;
    ssize_t rc      = write(g_wake_wr, &b, 1);
    (void)rc; /* a full pipe already holds a wake-up */
}

static void drain_wake(void)
{
    uint8_t buf[64];
    while (read(g_wake_rd, buf, sizeof buf) > 0)
    {
    }
}

static int set_nonblocking(int fd)
{
    int fl = fcntl(fd, F_GETFL, 0);
    if (fl < 0 || fcntl(fd, F_SETFL, fl | O_NONBLOCK) < 0)
        return -1;
    int fdfl = fcntl(fd, F_GETFD, 0);
    if (fdfl < 0 || fcntl(fd, F_SETFD, fdfl | FD_CLOEXEC) < 0)
        return -1;
    return 0;
}

/* ---------------------------------------------------------------------------------------- */
/* Control queue and viewer list (g_lock held)                                               */
/* ---------------------------------------------------------------------------------------- */

static void control_append(const uint8_t *msg, size_t len)
{
    if (!hmi_buf_append(&g_control, msg, len))
        hmi_log(LOG_ERROR, "no memory to answer the web server");
}

static void control_viewer(uint8_t type, uint32_t id)
{
    uint8_t msg[HMI_WIRE_VIEWER_MSG];
    control_append(msg, hmi_wire_encode_viewer(type, id, msg));
}

static void control_status(uint8_t state)
{
    uint8_t msg[HMI_WIRE_STATUS_MSG];
    control_append(msg, hmi_wire_encode_status(state, msg));
}

static void control_refused(uint32_t id, uint8_t what, uint16_t which)
{
    uint8_t msg[HMI_WIRE_REFUSED_MSG];
    control_append(msg, hmi_wire_encode_refused(id, what, which, msg));
}

/** The viewer with this id that is not closing, or NULL. */
static viewer_t *viewer_find(uint32_t id)
{
    for (viewer_t *v = g_viewers; v; v = v->next)
        if (v->id == id && !v->closing)
            return v;
    return NULL;
}

static void viewer_unlink(viewer_t *v)
{
    for (viewer_t **pp = &g_viewers; *pp; pp = &(*pp)->next)
    {
        if (*pp == v)
        {
            *pp     = v->next;
            v->next = NULL;
            return;
        }
    }
}

static void viewer_append(viewer_t *v)
{
    viewer_t **pp = &g_viewers;
    while (*pp)
        pp = &(*pp)->next;
    v->next = NULL;
    *pp     = v;
}

static void viewer_close_locked(viewer_t *v)
{
    if (v->closing)
        return;
    v->closing = true;
    if (g_open_count > 0)
        g_open_count--;
}

static void viewer_free(viewer_t *v)
{
    hmi_buf_free(&v->requests);
    hmi_values_free(&v->values);
    free(v);
}

/* ---------------------------------------------------------------------------------------- */
/* Access checks                                                                             */
/* ---------------------------------------------------------------------------------------- */

/** Whether a role may open page. */
static bool page_granted(int32_t page, uint8_t role)
{
    return page >= 0 && page < hmi_access_page_count && role <= HMI_WIRE_ROLE_MAX &&
           (hmi_access_pages[page].roles & (1u << role)) != 0;
}

/** Whether a sorted id list holds id. */
static bool list_has(const uint16_t *ids, uint16_t count, uint16_t id)
{
    size_t lo = 0, hi = count;
    while (lo < hi)
    {
        const size_t mid = lo + (hi - lo) / 2u;
        if (ids[mid] == id)
            return true;
        if (ids[mid] < id)
            lo = mid + 1u;
        else
            hi = mid;
    }
    return false;
}

static bool page_reads(int32_t page, uint16_t tag)
{
    return page >= 0 && page < hmi_access_page_count &&
           list_has(hmi_access_pages[page].reads, hmi_access_pages[page].read_count, tag);
}

/** How the shown page writes tag, or NULL when it does not write it. */
static const hmi_access_write_t *page_write(int32_t page, uint16_t tag)
{
    if (page < 0 || page >= hmi_access_page_count)
        return NULL;
    const hmi_access_write_t *w = hmi_access_pages[page].writes;
    size_t lo = 0, hi = hmi_access_pages[page].write_count;
    while (lo < hi)
    {
        const size_t mid = lo + (hi - lo) / 2u;
        if (w[mid].tag == tag)
            return &w[mid];
        if (w[mid].tag < tag)
            lo = mid + 1u;
        else
            hi = mid;
    }
    return NULL;
}

/** Whether a tag's type takes bit assignments (integers and bit strings); its width in bits, else
 * 0. */
static unsigned bit_width(uint8_t type)
{
    switch (type)
    {
    case HMI_CODEC_SINT:
    case HMI_CODEC_INT:
    case HMI_CODEC_DINT:
    case HMI_CODEC_LINT:
    case HMI_CODEC_USINT:
    case HMI_CODEC_UINT:
    case HMI_CODEC_UDINT:
    case HMI_CODEC_ULINT:
    case HMI_CODEC_BYTE:
    case HMI_CODEC_WORD:
    case HMI_CODEC_DWORD:
    case HMI_CODEC_LWORD:
        return (unsigned)hmi_codec_width(type) * 8u;
    default:
        return 0;
    }
}

/* ---------------------------------------------------------------------------------------- */
/* PLC variables (value thread)                                                              */
/* ---------------------------------------------------------------------------------------- */

static bool tag_address(int tag, uint8_t *arr, uint16_t *elem)
{
    if (tag < 0 || tag >= hmi_access_tag_count)
        return false;
    const hmi_access_tag_t *t = &hmi_access_tags[tag];
    if (t->arr > UINT8_MAX || t->elem > UINT16_MAX)
        return false;
    *arr  = (uint8_t)t->arr;
    *elem = (uint16_t)t->elem;
    return true;
}

/** Reads a tag into buffer format; false when it cannot be read. */
static bool tag_read(int tag, uint8_t *dest)
{
    uint8_t arr;
    uint16_t elem;
    if (!g_live || !g_args.debug_read || !g_args.debug_size || !tag_address(tag, &arr, &elem))
        return false;
    const uint16_t dsize = g_args.debug_size(arr, elem);
    if (dsize == 0 || dsize > HMI_CODEC_DEBUG_MAX)
        return false;
    uint8_t wire[HMI_CODEC_DEBUG_MAX];
    if (g_args.debug_read(arr, elem, wire) != dsize)
        return false;
    const hmi_access_tag_t *t = &hmi_access_tags[tag];
    return hmi_codec_from_debug(t->type, t->max_len, wire, dsize, dest);
}

/**
 * Soft-writes a checked value in buffer format, logged with the viewer's account; bit >= 0 names
 * the one bit a bit assignment changed, logged with the variable's resulting value.
 */
static void tag_write(const viewer_t *v, int tag, int bit, const uint8_t *src)
{
    uint8_t arr;
    uint16_t elem;
    const hmi_access_tag_t *t = &hmi_access_tags[tag];
    char value[160];
    hmi_codec_format(t->type, t->max_len, src, value, sizeof value);
    if (!g_args.debug_write || !g_args.debug_size || !tag_address(tag, &arr, &elem))
    {
        hmi_log(LOG_WARN, "write by %s: %s = %s failed: no debug access", v->account, tag_path(tag),
                value);
        return;
    }
    const uint16_t dsize = g_args.debug_size(arr, elem);
    uint8_t wire[HMI_CODEC_DEBUG_MAX];
    bool truncated = false;
    if (dsize == 0 || dsize > HMI_CODEC_DEBUG_MAX ||
        !hmi_codec_to_debug(t->type, t->max_len, src, wire, dsize, &truncated))
    {
        hmi_log(LOG_WARN, "write by %s: %s = %s refused: the variable does not match the HMI",
                v->account, tag_path(tag), value);
        return;
    }
    const uint8_t status = g_args.debug_write(arr, elem, wire, dsize);
    if (status != DEBUG_WRITE_OK)
    {
        hmi_log(LOG_WARN, "write by %s: %s = %s failed (status 0x%02X)", v->account, tag_path(tag),
                value, status);
        return;
    }
    if (bit >= 0)
        hmi_log(LOG_INFO, "write by %s: %s.%d = %s (now %s)", v->account, tag_path(tag), bit,
                (src[bit / 8] >> (bit % 8)) & 1u ? "TRUE" : "FALSE", value);
    else
        hmi_log(LOG_INFO, "write by %s: %s = %s%s", v->account, tag_path(tag), value,
                truncated ? " (cut to the debugger's string length)" : "");
}

/* ---------------------------------------------------------------------------------------- */
/* Value thread                                                                              */
/* ---------------------------------------------------------------------------------------- */

static void refuse(viewer_t *v, uint8_t what, uint16_t which)
{
    pthread_mutex_lock(&g_lock);
    if (!v->closing)
        control_refused(v->id, what, which);
    pthread_mutex_unlock(&g_lock);
    wake_io();
}

/** SHOW: serve the page's tags when the viewer's role may open it, nothing otherwise. */
static void apply_show(viewer_t *v, uint16_t page)
{
    const bool granted = page_granted(page, v->role);
    pthread_mutex_lock(&g_lock);
    v->page = granted ? (int32_t)page : -1;
    hmi_values_forget_all(&v->values, &g_layout);
    v->announce = true;
    pthread_mutex_unlock(&g_lock);
    if (!granted)
    {
        hmi_log(LOG_WARN, "screen %u refused for %s (%s): not a screen this role may open",
                (unsigned)page, v->account, role_name(v->role));
        refuse(v, HMI_REFUSED_SHOW, page);
    }
}

/** Counts a processed write and has a tag the shown page reads sent again with its value after it.
 */
static void count_write(viewer_t *v, int32_t page, uint16_t tag)
{
    pthread_mutex_lock(&g_lock);
    v->writes++;
    v->announce = true;
    if (page_reads(page, tag))
        hmi_values_forget(&v->values, &g_layout, tag);
    pthread_mutex_unlock(&g_lock);
}

static void refuse_write(viewer_t *v, uint16_t tag, const char *why)
{
    hmi_log(LOG_WARN, "write by %s (%s) of %s refused: %s", v->account, role_name(v->role),
            tag < hmi_access_tag_count ? tag_path(tag) : "an unknown tag", why);
    refuse(v, HMI_REFUSED_WRITE, tag);
}

/**
 * WRITE: applied when the shown screen writes the tag as a whole value, the value fits its type and
 * range, and lies within the screen's limit for the tag when it has one (its widgets' ranges and
 * the literals it assigns). A tag the screen only assigns bit by bit takes no whole value.
 * Conditions in screen code are not checked: they run in the browser.
 */
static void apply_write(viewer_t *v, uint16_t tag, const uint8_t *value, uint16_t len,
                        uint8_t *buffer)
{
    const int32_t page            = v->page;
    const hmi_access_write_t *how = page_write(page, tag);
    const char *why               = NULL;
    if (tag >= hmi_access_tag_count)
        why = "no such variable";
    else if (page < 0)
        why = "no screen shown";
    else if (!how)
        why = "the screen shown does not write it";
    else if (!how->whole)
        why = "the screen shown only sets single bits of it";
    else if (!hmi_codec_from_wire(hmi_access_tags[tag].type, hmi_access_tags[tag].max_len, value,
                                  len, buffer))
        why = "the value does not fit its type";
    else if (how->limited &&
             !hmi_codec_within(hmi_access_tags[tag].type, hmi_access_tags[tag].max_len, buffer,
                               how->min, how->max))
        why = "the value is outside what the screen shown can set";

    if (why)
        refuse_write(v, tag, why);
    else
        tag_write(v, tag, -1, buffer);
    count_write(v, page, tag);
}

/**
 * BIT: one bit assignment of the shown screen, applied to the variable's current value (read,
 * change the bit, soft-write), so bits the PLC changed meanwhile stay. Only bits the screen
 * assigns.
 */
static void apply_bit(viewer_t *v, uint16_t tag, uint8_t bit, uint8_t on, uint8_t *buffer)
{
    const int32_t page            = v->page;
    const hmi_access_write_t *how = page_write(page, tag);
    const char *why               = NULL;
    if (tag >= hmi_access_tag_count)
        why = "no such variable";
    else if (page < 0)
        why = "no screen shown";
    else if (!how || ((how->bits >> bit) & 1u) == 0)
        why = "the screen shown does not set that bit";
    else if (bit >= bit_width(hmi_access_tags[tag].type))
        why = "the variable has no such bit";
    else if (!tag_read(tag, buffer))
        why = "the variable cannot be read";

    if (why)
    {
        refuse_write(v, tag, why);
    }
    else
    {
        const uint8_t mask = (uint8_t)(1u << (bit % 8u));
        buffer[bit / 8u]   = (uint8_t)(on ? buffer[bit / 8u] | mask : buffer[bit / 8u] & ~mask);
        tag_write(v, tag, bit, buffer);
    }
    count_write(v, page, tag);
}

/** Applies the viewer's queued requests in arrival order. */
static void apply_requests(viewer_t *v, hmi_buf_t *scratch, uint8_t *buffer)
{
    pthread_mutex_lock(&g_lock);
    hmi_buf_t taken  = v->requests;
    v->requests      = *scratch;
    v->requests.len  = 0;
    v->request_count = 0;
    pthread_mutex_unlock(&g_lock);

    size_t off = 0;
    while (off + REQUEST_HEADER <= taken.len)
    {
        const uint8_t *r   = taken.data + off;
        const uint8_t kind = r[0];
        const uint16_t id  = (uint16_t)(r[1] | (r[2] << 8));
        const uint16_t len = (uint16_t)(r[3] | (r[4] << 8));
        off += REQUEST_HEADER + len;
        if (kind == REQUEST_SHOW)
            apply_show(v, id);
        else if (kind == REQUEST_BIT && len == 2)
            apply_bit(v, id, r[REQUEST_HEADER], r[REQUEST_HEADER + 1], buffer);
        else
            apply_write(v, id, r + REQUEST_HEADER, len, buffer);
    }
    taken.len = 0;
    *scratch  = taken;
}

/** Reads the shown page's tags and offers them to the viewer's pending message. */
static bool serve_values(viewer_t *v, uint8_t *buffer, uint8_t *wire)
{
    const int32_t page = v->page;
    bool stored        = false;
    if (page >= 0)
    {
        const hmi_access_page_t *p = &hmi_access_pages[page];
        for (uint16_t i = 0; i < p->read_count; i++)
        {
            const int tag = p->reads[i];
            if (!tag_read(tag, buffer))
                continue;
            const hmi_access_tag_t *t = &hmi_access_tags[tag];
            const size_t len          = hmi_codec_to_wire(t->type, t->max_len, buffer, wire);
            pthread_mutex_lock(&g_lock);
            if (hmi_values_offer(&v->values, &g_layout, tag, wire, (uint16_t)len))
                v->announce = true;
            pthread_mutex_unlock(&g_lock);
        }
    }
    pthread_mutex_lock(&g_lock);
    if (v->announce && !v->closing)
    {
        const uint16_t wire_page = page >= 0 ? (uint16_t)page : UINT16_MAX;
        if (hmi_values_encode(&v->values, &g_layout, v->id, wire_page, v->writes))
        {
            v->announce      = false;
            g_values_pending = true;
            stored           = true;
        }
        else
        {
            hmi_log(LOG_ERROR, "viewer %u: its values could not be queued", (unsigned)v->id);
        }
    }
    pthread_mutex_unlock(&g_lock);
    return stored;
}

/** Frees closing viewers. */
static void free_closing(void)
{
    viewer_t *to_free = NULL;
    pthread_mutex_lock(&g_lock);
    viewer_t **pp = &g_viewers;
    while (*pp)
    {
        viewer_t *v = *pp;
        if (v->closing)
        {
            *pp     = v->next;
            v->next = to_free;
            to_free = v;
            continue;
        }
        pp = &v->next;
    }
    const size_t count = g_open_count;
    pthread_mutex_unlock(&g_lock);
    while (to_free)
    {
        viewer_t *v = to_free;
        to_free     = v->next;
        hmi_log(LOG_INFO, "viewer %u closed: %zu viewer(s)", (unsigned)v->id, count);
        viewer_free(v);
    }
}

static void set_default_priority(void)
{
#ifdef __linux__
    errno = 0;
    if (setpriority(PRIO_PROCESS, (id_t)syscall(SYS_gettid), 0) != 0)
        hmi_log(LOG_WARN, "could not set nice 0: %s", strerror(errno));
#endif
}

/**
 * The value thread: every period it frees closing viewers, then applies each open viewer's
 * requests and serves its values. Viewers stay allocated until this thread frees them, so the
 * list it snapshots stays valid without g_lock.
 */
static void *value_main(void *arg)
{
    (void)arg;
    set_default_priority();

    viewer_t **list       = NULL;
    size_t list_cap       = 0;
    uint64_t next         = now_ns();
    const uint64_t period = (uint64_t)g_period_ms * 1000000u;
    hmi_buf_t scratch     = {0};
    uint8_t *buffer       = malloc(g_value_max);
    uint8_t *wire         = malloc(g_value_max);
    if (!buffer || !wire)
        hmi_log(LOG_ERROR, "no memory for the value thread: no values are served");

    for (;;)
    {
        pthread_mutex_lock(&g_lock);
        while (!g_stop && now_ns() < next)
            cond_wait_until(next);
        const bool stop = g_stop;
        pthread_mutex_unlock(&g_lock);
        if (stop)
            break;
        const uint64_t now = now_ns();
        next += period;
        if (next <= now)
            next = now + period;

        free_closing();
        if (!buffer || !wire)
            continue;

        size_t count = 0;
        pthread_mutex_lock(&g_lock);
        for (viewer_t *v = g_viewers; v; v = v->next)
        {
            if (v->closing)
                continue;
            if (count == list_cap)
            {
                size_t cap   = list_cap ? list_cap * 2u : 16u;
                viewer_t **p = realloc(list, cap * sizeof *list);
                if (!p)
                    break;
                list     = p;
                list_cap = cap;
            }
            list[count++] = v;
        }
        pthread_mutex_unlock(&g_lock);

        bool stored = false;
        for (size_t i = 0; i < count; i++)
        {
            apply_requests(list[i], &scratch, buffer);
            stored |= serve_values(list[i], buffer, wire);
        }
        if (stored)
            wake_io();
    }

    free(list);
    free(buffer);
    free(wire);
    hmi_buf_free(&scratch);
    pthread_mutex_lock(&g_lock);
    viewer_t *all = g_viewers;
    g_viewers     = NULL;
    g_open_count  = 0;
    pthread_mutex_unlock(&g_lock);
    while (all)
    {
        viewer_t *v = all;
        all         = v->next;
        viewer_free(v);
    }
    return NULL;
}

/* ---------------------------------------------------------------------------------------- */
/* I/O thread                                                                                */
/* ---------------------------------------------------------------------------------------- */

typedef struct
{
    int fd;
    uint8_t in[READ_CHUNK + HMI_WIRE_MAX_INBOUND];
    size_t in_len;
    hmi_buf_t out;
    size_t out_off;
} conn_t;

/** Drops every viewer and the connection. */
static void conn_close(conn_t *c, const char *why)
{
    if (c->fd < 0)
        return;
    close(c->fd);
    c->fd      = -1;
    c->in_len  = 0;
    c->out.len = 0;
    c->out_off = 0;

    size_t dropped = 0;
    pthread_mutex_lock(&g_lock);
    for (viewer_t *v = g_viewers; v; v = v->next)
    {
        if (!v->closing)
        {
            viewer_close_locked(v);
            dropped++;
        }
    }
    g_control.len    = 0;
    g_values_pending = false;
    pthread_mutex_unlock(&g_lock);
    hmi_log(LOG_INFO, "web server connection closed (%s): %zu viewer(s) dropped", why, dropped);
}

/** Queues a SHOW, WRITE or BIT request (g_lock held); false when the viewer was dropped for
 * overflowing its queue: more requests than the value thread takes, so it cannot be served. */
static bool push_request(viewer_t *v, uint8_t kind, uint16_t id, const uint8_t *value, uint16_t len)
{
    const uint8_t head[REQUEST_HEADER] = {kind, (uint8_t)id, (uint8_t)(id >> 8), (uint8_t)len,
                                          (uint8_t)(len >> 8)};
    const size_t need                  = v->requests.len + REQUEST_HEADER + len;
    if (v->request_count < REQUEST_QUEUE_CAP && need <= REQUEST_QUEUE_BYTES &&
        hmi_buf_reserve(&v->requests, need))
    {
        memcpy(v->requests.data + v->requests.len, head, REQUEST_HEADER);
        if (len > 0)
            memcpy(v->requests.data + v->requests.len + REQUEST_HEADER, value, len);
        v->requests.len = need;
        v->request_count++;
        return true;
    }
    viewer_close_locked(v);
    control_viewer(HMI_MSG_BUSY, v->id);
    return false;
}

/** Opens a viewer (g_lock held). */
static void open_viewer(const hmi_wire_msg_t *m)
{
    if (!g_live)
    {
        control_status(HMI_STATE_OUT_OF_DATE);
        return;
    }
    if (!m->name_valid)
    {
        hmi_log(LOG_WARN, "viewer %u refused: its account name is not printable UTF-8",
                (unsigned)m->viewer);
        control_viewer(HMI_MSG_BUSY, m->viewer);
        return;
    }
    viewer_t *nv = calloc(1, sizeof *nv);
    if (!nv || !hmi_values_init(&nv->values, &g_layout))
    {
        free(nv);
        hmi_log(LOG_WARN, "viewer %u refused: no memory", (unsigned)m->viewer);
        control_viewer(HMI_MSG_BUSY, m->viewer);
        return;
    }
    nv->id   = m->viewer;
    nv->role = m->role;
    nv->page = -1;
    memcpy(nv->account, m->name, (size_t)m->name_len + 1u);
    viewer_append(nv);
    g_open_count++;
    control_viewer(HMI_MSG_OPENED, nv->id);
    hmi_log(LOG_INFO, "viewer %u opened by %s (%s): %zu viewer(s)", (unsigned)nv->id, nv->account,
            role_name(nv->role), g_open_count);
}

/** Applies one message; returns false when it breaks the protocol. */
static bool handle_message(const hmi_wire_msg_t *m)
{
    bool ok         = true;
    bool overflowed = false;
    pthread_mutex_lock(&g_lock);
    viewer_t *v = viewer_find(m->viewer);

    switch (m->type)
    {
    case HMI_MSG_OPEN:
        if (v)
            ok = false;
        else
            open_viewer(m);
        break;
    case HMI_MSG_CLOSE:
        if (v)
            viewer_close_locked(v);
        break;
    case HMI_MSG_SHOW:
        if (v)
            overflowed = !push_request(v, REQUEST_SHOW, m->page, NULL, 0);
        break;
    case HMI_MSG_WRITE:
        if (v)
            overflowed = !push_request(v, REQUEST_WRITE, m->tag, m->value, m->value_len);
        break;
    case HMI_MSG_BIT:
        if (v)
        {
            const uint8_t bit[2] = {m->bit, m->bit_value};
            overflowed           = !push_request(v, REQUEST_BIT, m->tag, bit, sizeof bit);
        }
        break;
    default:
        ok = false;
        break;
    }
    pthread_mutex_unlock(&g_lock);
    if (overflowed)
        hmi_log(LOG_WARN, "viewer %u dropped: its request queue overflowed", (unsigned)m->viewer);
    return ok;
}

/** Reads and applies what the web server sent; returns false when the connection must close. */
static bool conn_read(conn_t *c, const char **why)
{
    for (;;)
    {
        ssize_t n = read(c->fd, c->in + c->in_len, sizeof c->in - c->in_len);
        if (n == 0)
        {
            *why = "closed by the web server";
            return false;
        }
        if (n < 0)
        {
            if (errno == EINTR)
                continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                return true;
            *why = "read error";
            return false;
        }
        c->in_len += (size_t)n;

        size_t off = 0;
        for (;;)
        {
            hmi_wire_msg_t m;
            size_t used         = 0;
            hmi_wire_result_t r = hmi_wire_decode(c->in + off, c->in_len - off, &m, &used);
            if (r == HMI_WIRE_NEED_MORE)
                break;
            if (r == HMI_WIRE_MALFORMED || !handle_message(&m))
            {
                *why = "malformed message";
                return false;
            }
            off += used;
        }
        memmove(c->in, c->in + off, c->in_len - off);
        c->in_len -= off;

        pthread_mutex_lock(&g_lock);
        const bool backlog = g_control.len >= CONTROL_SOFT_MAX;
        pthread_mutex_unlock(&g_lock);
        if (backlog)
            return true;
    }
}

/** Moves the next message to send into c->out: control first, then VALUES round robin (the
 * viewer just served moves to the end of the list). */
static bool next_output(conn_t *c)
{
    bool got = false;
    pthread_mutex_lock(&g_lock);
    if (g_control.len > 0)
    {
        hmi_buf_t tmp = c->out;
        c->out        = g_control;
        g_control     = tmp;
        g_control.len = 0;
        got           = true;
    }
    else if (g_values_pending)
    {
        viewer_t *taken = NULL;
        for (viewer_t *v = g_viewers; v; v = v->next)
        {
            if (!v->closing && hmi_values_take(&v->values, &g_layout, &c->out))
            {
                taken = v;
                break;
            }
        }
        if (taken)
        {
            viewer_unlink(taken);
            viewer_append(taken);
            got = true;
        }
        else
        {
            g_values_pending = false;
        }
    }
    pthread_mutex_unlock(&g_lock);
    c->out_off = 0;
    return got;
}

static bool conn_has_output(const conn_t *c)
{
    if (c->out_off < c->out.len)
        return true;
    pthread_mutex_lock(&g_lock);
    bool pending = g_control.len > 0 || g_values_pending;
    pthread_mutex_unlock(&g_lock);
    return pending;
}

/** Sends what the socket takes without blocking; returns false on a send error. */
static bool conn_write(conn_t *c)
{
    for (;;)
    {
        if (c->out_off >= c->out.len)
        {
            c->out.len = 0;
            if (!next_output(c))
                return true;
        }
#ifdef MSG_NOSIGNAL
        const int flags = MSG_NOSIGNAL | MSG_DONTWAIT;
#else
        const int flags = MSG_DONTWAIT;
#endif
        ssize_t n = send(c->fd, c->out.data + c->out_off, c->out.len - c->out_off, flags);
        if (n < 0)
        {
            if (errno == EINTR)
                continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                return true;
            return false;
        }
        c->out_off += (size_t)n;
    }
}

static void conn_adopt(conn_t *c, int fd)
{
    if (c->fd >= 0)
        conn_close(c, "replaced by a new connection");
#ifdef SO_NOSIGPIPE
    const int one = 1;
    (void)setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
    c->fd      = fd;
    c->in_len  = 0;
    c->out.len = 0;
    c->out_off = 0;
    pthread_mutex_lock(&g_lock);
    control_status(g_live ? HMI_STATE_LIVE : HMI_STATE_OUT_OF_DATE);
    pthread_mutex_unlock(&g_lock);
    hmi_log(LOG_INFO, "web server connected (%s)", g_live ? "live" : "out of date");
}

static void *io_main(void *arg)
{
    (void)arg;
    set_default_priority();

    conn_t *c = calloc(1, sizeof *c);
    if (!c)
    {
        hmi_log(LOG_ERROR, "no memory for the web server connection");
        return NULL;
    }
    c->fd = -1;

    for (;;)
    {
        struct pollfd fds[3];
        fds[0].fd     = g_wake_rd;
        fds[0].events = POLLIN;
        fds[1].fd     = g_listen_fd;
        fds[1].events = POLLIN;
        nfds_t nfds   = 2;
        if (c->fd >= 0)
        {
            short ev = 0;
            pthread_mutex_lock(&g_lock);
            const bool backlog = g_control.len >= CONTROL_SOFT_MAX;
            pthread_mutex_unlock(&g_lock);
            if (!backlog)
                ev |= POLLIN;
            if (conn_has_output(c))
                ev |= POLLOUT;
            fds[2].fd     = c->fd;
            fds[2].events = ev;
            nfds          = 3;
        }
        for (nfds_t i = 0; i < nfds; i++)
            fds[i].revents = 0;

        if (poll(fds, nfds, -1) < 0 && errno != EINTR)
        {
            hmi_log(LOG_ERROR, "poll failed: %s", strerror(errno));
            break;
        }

        pthread_mutex_lock(&g_lock);
        const bool stop = g_stop;
        pthread_mutex_unlock(&g_lock);
        if (stop)
            break;

        if (fds[0].revents & POLLIN)
            drain_wake();

        if (fds[1].revents & POLLIN)
        {
            int fd = accept(g_listen_fd, NULL, NULL);
            if (fd >= 0)
            {
                if (set_nonblocking(fd) != 0)
                    close(fd);
                else
                    conn_adopt(c, fd);
            }
        }

        if (c->fd >= 0 && nfds == 3)
        {
            const char *why = NULL;
            if (fds[2].revents & POLLNVAL)
                conn_close(c, "socket error");
            else if ((fds[2].revents & (POLLIN | POLLHUP | POLLERR)) && !conn_read(c, &why))
                conn_close(c, why ? why : "closed");
            else if (fds[2].revents & POLLERR)
                conn_close(c, "socket error");
        }
        if (c->fd >= 0 && !conn_write(c))
            conn_close(c, "send error");
    }

    conn_close(c, "plugin stopping");
    hmi_buf_free(&c->out);
    free(c);
    return NULL;
}

/* ---------------------------------------------------------------------------------------- */
/* Plugin interface                                                                          */
/* ---------------------------------------------------------------------------------------- */

/** Whether a page's id list is ascending, without repeats, and names only known tags. */
static bool id_list_valid(const uint16_t *ids, uint16_t count)
{
    for (uint16_t i = 0; i < count; i++)
        if (ids[i] >= hmi_access_tag_count || (i > 0 && ids[i] <= ids[i - 1]))
            return false;
    return true;
}

/**
 * Whether a page's write list is ascending by tag without repeats, names only known tags, writes
 * each one somehow, and sets bits only of integer and bit-string tags, within their width.
 */
static bool write_list_valid(const hmi_access_write_t *w, uint16_t count)
{
    for (uint16_t i = 0; i < count; i++)
    {
        if (w[i].tag >= hmi_access_tag_count || (i > 0 && w[i].tag <= w[i - 1].tag))
            return false;
        if (!w[i].whole && w[i].bits == 0)
            return false;
        if (w[i].limited && (!w[i].whole || !(w[i].min <= w[i].max)))
            return false;
        const unsigned width = bit_width(hmi_access_tags[w[i].tag].type);
        if (w[i].bits != 0 && (width == 0 || (width < 64u && (w[i].bits >> width) != 0)))
            return false;
    }
    return true;
}

/** Checks the manifest and builds the value layout; false when it cannot be served. */
static bool manifest_valid(void)
{
    if (hmi_access_tag_count < 0 || hmi_access_tag_count > UINT16_MAX ||
        hmi_access_page_count <= 0 || hmi_access_page_count >= UINT16_MAX)
    {
        hmi_log(LOG_ERROR, "the HMI manifest has %d tags and %d screens", hmi_access_tag_count,
                hmi_access_page_count);
        return false;
    }
    uint16_t *max = calloc(hmi_access_tag_count > 0 ? (size_t)hmi_access_tag_count : 1u, 2);
    if (!max)
        return false;
    bool ok     = true;
    g_value_max = 1;
    for (int i = 0; i < hmi_access_tag_count && ok; i++)
    {
        const hmi_access_tag_t *t = &hmi_access_tags[i];
        const size_t size         = hmi_codec_buffer_size(t->type, t->max_len);
        const size_t wire         = t->type == HMI_CODEC_STRING ? t->max_len : size;
        if (size == 0 || wire > HMI_WIRE_VALUE_MAX || t->arr > UINT8_MAX || t->elem > UINT16_MAX)
        {
            hmi_log(LOG_ERROR, "tag %d (%s) cannot be served", i, tag_path(i));
            ok = false;
            break;
        }
        max[i] = (uint16_t)wire;
        if (size > g_value_max)
            g_value_max = size;
    }
    for (int p = 0; p < hmi_access_page_count && ok; p++)
    {
        const hmi_access_page_t *page = &hmi_access_pages[p];
        if (!id_list_valid(page->reads, page->read_count) ||
            !write_list_valid(page->writes, page->write_count))
        {
            hmi_log(LOG_ERROR, "screen %d of the HMI manifest lists its tags wrongly", p);
            ok = false;
        }
    }
    if (ok)
    {
        hmi_values_layout_free(&g_layout);
        ok = hmi_values_layout_init(&g_layout, max, hmi_access_tag_count);
    }
    free(max);
    return ok;
}

/** The HMI never touches the image tables, so any per-table sizes suit it. */
HMI_EXPORT int set_image_sizes(const uint32_t *sizes, uint32_t count)
{
    (void)sizes;
    (void)count;
    return 0;
}

HMI_EXPORT int init(void *args)
{
    if (!args)
        return -1;
    memcpy(&g_args, args, sizeof g_args);
    g_have_args                                       = true;
    g_args.program_md5[sizeof g_args.program_md5 - 1] = '\0';

    g_period_ms = hmi_access_period_ms;
    if (g_period_ms == 0)
        g_period_ms = PERIOD_MS_DEFAULT;
    if (g_period_ms < PERIOD_MS_MIN)
        g_period_ms = PERIOD_MS_MIN;
    if (g_period_ms > PERIOD_MS_MAX)
        g_period_ms = PERIOD_MS_MAX;

    g_live = hmi_md5_live(g_args.program_md5, hmi_access_plc_md5) && manifest_valid();
    if (g_live)
        hmi_log(LOG_INFO, "screens match the PLC program (%d tags, %d screens, values every %u ms)",
                hmi_access_tag_count, hmi_access_page_count, (unsigned)g_period_ms);
    else
        hmi_log(LOG_INFO, "screens out of date: PLC program '%s', screens built for '%s'",
                g_args.program_md5, hmi_access_plc_md5);
    return 0;
}

/** Creates HMI_SOCKET_DIR, or takes the existing one when it is a real directory of this user; mode
 * 0700. */
static int prepare_socket_dir(void)
{
    if (mkdir(HMI_SOCKET_DIR, S_IRWXU) != 0 && errno != EEXIST)
    {
        hmi_log(LOG_ERROR, "cannot create %s: %s", HMI_SOCKET_DIR, strerror(errno));
        return -1;
    }
    const int dfd = open(HMI_SOCKET_DIR, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (dfd < 0)
    {
        hmi_log(LOG_ERROR, "cannot open %s: %s", HMI_SOCKET_DIR, strerror(errno));
        return -1;
    }
    struct stat st;
    const bool ok = fstat(dfd, &st) == 0 && S_ISDIR(st.st_mode) && st.st_uid == geteuid() &&
                    fchmod(dfd, S_IRWXU) == 0;
    close(dfd);
    if (!ok)
        hmi_log(LOG_ERROR, "%s is not a private directory of this user", HMI_SOCKET_DIR);
    return ok ? 0 : -1;
}

static int create_listener(void)
{
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    if (strlen(HMI_SOCKET_PATH) >= sizeof addr.sun_path)
        return -1;
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", HMI_SOCKET_PATH);

    if (prepare_socket_dir() != 0)
        return -1;
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return -1;
    if (set_nonblocking(fd) != 0)
    {
        close(fd);
        return -1;
    }
    if (unlink(HMI_SOCKET_PATH) != 0 && errno != ENOENT)
        hmi_log(LOG_WARN, "could not remove the old %s: %s", HMI_SOCKET_PATH, strerror(errno));

    if (bind(fd, (const struct sockaddr *)&addr, sizeof addr) != 0 || listen(fd, 4) != 0)
    {
        hmi_log(LOG_ERROR, "cannot listen on %s: %s", HMI_SOCKET_PATH, strerror(errno));
        close(fd);
        (void)unlink(HMI_SOCKET_PATH);
        return -1;
    }
    return fd;
}

static int start_thread(pthread_t *t, void *(*fn)(void *))
{
    pthread_attr_t attr;
    if (pthread_attr_init(&attr) != 0)
        return -1;
    struct sched_param sp;
    memset(&sp, 0, sizeof sp);
    int rc = pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    if (rc == 0)
        rc = pthread_attr_setschedpolicy(&attr, SCHED_OTHER);
    if (rc == 0)
        rc = pthread_attr_setschedparam(&attr, &sp);
    if (rc == 0)
        rc = pthread_create(t, &attr, fn, NULL);
    pthread_attr_destroy(&attr);
    return rc == 0 ? 0 : -1;
}

static void close_fds(void)
{
    if (g_listen_fd >= 0)
        close(g_listen_fd);
    if (g_wake_rd >= 0)
        close(g_wake_rd);
    if (g_wake_wr >= 0)
        close(g_wake_wr);
    g_listen_fd = g_wake_rd = g_wake_wr = -1;
}

HMI_EXPORT int start_loop(void)
{
    if (g_running)
        return 0;
    if (!g_have_args)
        return -1;
    if (!g_cond_ready)
    {
        if (cond_init() != 0)
            return -1;
        g_cond_ready = true;
    }

    int wake[2];
    if (pipe(wake) != 0)
        return -1;
    g_wake_rd = wake[0];
    g_wake_wr = wake[1];
    if (set_nonblocking(g_wake_rd) != 0 || set_nonblocking(g_wake_wr) != 0)
    {
        close_fds();
        return -1;
    }
    g_listen_fd = create_listener();
    if (g_listen_fd < 0)
    {
        close_fds();
        return -1;
    }

    pthread_mutex_lock(&g_lock);
    g_stop           = false;
    g_values_pending = false;
    g_open_count     = 0;
    g_control.len    = 0;
    pthread_mutex_unlock(&g_lock);

    if (start_thread(&g_value_thread, value_main) != 0)
    {
        close_fds();
        (void)unlink(HMI_SOCKET_PATH);
        hmi_log(LOG_ERROR, "cannot start the value thread");
        return -1;
    }
    if (start_thread(&g_io_thread, io_main) != 0)
    {
        pthread_mutex_lock(&g_lock);
        g_stop = true;
        pthread_cond_signal(&g_cond);
        pthread_mutex_unlock(&g_lock);
        pthread_join(g_value_thread, NULL);
        close_fds();
        (void)unlink(HMI_SOCKET_PATH);
        hmi_log(LOG_ERROR, "cannot start the socket thread");
        return -1;
    }
    g_running = true;
    hmi_log(LOG_INFO, "serving viewers on %s", HMI_SOCKET_PATH);
    return 0;
}

HMI_EXPORT void stop_loop(void)
{
    if (!g_running)
        return;
    pthread_mutex_lock(&g_lock);
    g_stop = true;
    pthread_cond_signal(&g_cond);
    pthread_mutex_unlock(&g_lock);
    wake_io();
    pthread_join(g_io_thread, NULL);
    pthread_join(g_value_thread, NULL);
    g_running = false;

    close_fds();
    if (unlink(HMI_SOCKET_PATH) != 0 && errno != ENOENT)
        hmi_log(LOG_WARN, "could not remove %s: %s", HMI_SOCKET_PATH, strerror(errno));
    pthread_mutex_lock(&g_lock);
    hmi_buf_free(&g_control);
    pthread_mutex_unlock(&g_lock);
    hmi_log(LOG_INFO, "stopped");
}

HMI_EXPORT void cleanup(void)
{
    stop_loop();
    hmi_values_layout_free(&g_layout);
    g_have_args = false;
}
