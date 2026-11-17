// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file hmi_host.c
 * @brief HMI plugin of Runtime v4: runs the generated HMI unit for every viewer the web server
 *        opens over a Unix socket (docs/HMI.md, "Host" and "Wire protocol").
 *
 * Threads, both at the default priority:
 * - the I/O thread owns the socket: it accepts the web server, decodes its messages, queues
 *   input and sends control messages and frames without blocking;
 * - the frame thread owns the HMI core and the unit (both single threaded): it creates and frees
 *   viewers, applies input, renders every open viewer each hmi_frame_ms and offers the display
 *   list to the viewer's latest-frame slot.
 * One logical unit is one CSS pixel; the viewport's density is validated and otherwise unused.
 * g_lock guards the viewer list, each viewer's input queue, viewport and slot, and the control
 * queue. It is never held while rendering or sending.
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

#include "hmi_codec.h"
#include "hmi_core.h"
#include "hmi_input.h"
#include "hmi_slot.h"
#include "hmi_unit.h"
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
/** How long stop_loop waits for the frame in progress to return. */
#ifndef HMI_STOP_TIMEOUT_MS
#define HMI_STOP_TIMEOUT_MS 5000u
#endif
/** plc_main exit status when screen code never returns during a stop. */
#define HMI_EXIT_SCREEN_STUCK 43

#define HMI_EXPORT __attribute__((visibility("default")))

#define FRAME_MS_DEFAULT 100u
#define FRAME_MS_MIN 10u
#define FRAME_MS_MAX 10000u
/** Control bytes queued for the web server above which the host stops reading its messages. */
#define CONTROL_SOFT_MAX (64u * 1024u)
#define READ_CHUNK 4096u
#define LOG_LINE 768u
/** Pointer coordinates are clamped to this many Q4 units either way. */
#define POINTER_Q4_LIMIT (1 << 24)

#define SAME_VALUE(a, b) ((int)(a) == (int)(b))
_Static_assert(
    SAME_VALUE(HMI_CODEC_BOOL, HMI_TAG_BOOL) && SAME_VALUE(HMI_CODEC_SINT, HMI_TAG_SINT) &&
        SAME_VALUE(HMI_CODEC_INT, HMI_TAG_INT) && SAME_VALUE(HMI_CODEC_DINT, HMI_TAG_DINT) &&
        SAME_VALUE(HMI_CODEC_LINT, HMI_TAG_LINT) && SAME_VALUE(HMI_CODEC_USINT, HMI_TAG_USINT) &&
        SAME_VALUE(HMI_CODEC_UINT, HMI_TAG_UINT) && SAME_VALUE(HMI_CODEC_UDINT, HMI_TAG_UDINT) &&
        SAME_VALUE(HMI_CODEC_ULINT, HMI_TAG_ULINT) && SAME_VALUE(HMI_CODEC_BYTE, HMI_TAG_BYTE) &&
        SAME_VALUE(HMI_CODEC_WORD, HMI_TAG_WORD) && SAME_VALUE(HMI_CODEC_DWORD, HMI_TAG_DWORD) &&
        SAME_VALUE(HMI_CODEC_LWORD, HMI_TAG_LWORD) && SAME_VALUE(HMI_CODEC_REAL, HMI_TAG_REAL) &&
        SAME_VALUE(HMI_CODEC_LREAL, HMI_TAG_LREAL) && SAME_VALUE(HMI_CODEC_STRING, HMI_TAG_STRING),
    "hmi_codec_type_t must mirror hmi_tag_type");
_Static_assert(SAME_VALUE(HMI_INPUT_DOWN, HMI_PTR_DOWN) &&
                   SAME_VALUE(HMI_INPUT_MOVE, HMI_PTR_MOVE) && SAME_VALUE(HMI_INPUT_UP, HMI_PTR_UP),
               "hmi_input_ptr_t must mirror hmi_ptr_kind");
_Static_assert(HMI_ROLE_ADMIN == 0 && HMI_ROLE_VIEWER == 3, "wire roles are hmi_role values");

typedef enum
{
    VIEWER_PENDING, /**< OPEN received; the frame thread has not created its session yet */
    VIEWER_OPEN,
    VIEWER_CLOSING /**< closed or dropped; the frame thread frees it */
} viewer_state_t;

typedef struct viewer
{
    struct viewer *next;
    uint32_t id;
    viewer_state_t state;
    uint8_t role;
    char account[HMI_WIRE_NAME_MAX + 1];

    /* Under g_lock. */
    uint16_t width;
    uint16_t height;
    bool viewport_dirty;
    hmi_input_queue_t input;
    hmi_slot_t slot;

    /* Frame thread only. */
    hmi_session_t *session;
    void *unit;
    uint32_t status_warned;
    bool offer_warned;
    bool was_open;      /**< reached VIEWER_OPEN, so its close is logged */
    bool sign_out_sent; /**< its screen asked to sign out; SIGN_OUT went to the web server */
} viewer_t;

static plugin_runtime_args_t g_args;
static bool g_have_args;
static bool g_live;
static uint32_t g_frame_ms = FRAME_MS_DEFAULT;

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_cond;
static bool g_cond_ready;
static viewer_t *g_viewers;
static size_t g_open_count;
static hmi_buf_t g_control;
static bool g_frames_pending;
static bool g_structural; /**< a viewer is pending or closing */
static bool g_stop;
static bool g_frame_done; /**< the frame thread has left its loop and freed its viewers */

static bool g_running;
static pthread_t g_io_thread;
static pthread_t g_frame_thread;
static int g_listen_fd = -1;
static int g_wake_rd   = -1;
static int g_wake_wr   = -1;

/** Viewer whose frame runs; its account is named in write logs. Frame thread only. */
static const viewer_t *g_current;

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

static bool control_append(const uint8_t *msg, size_t len)
{
    if (g_control.cap - g_control.len < len)
    {
        size_t cap = g_control.cap ? g_control.cap * 2u : 256u;
        while (cap - g_control.len < len)
            cap *= 2u;
        uint8_t *p = realloc(g_control.data, cap);
        if (!p)
            return false;
        g_control.data = p;
        g_control.cap  = cap;
    }
    memcpy(g_control.data + g_control.len, msg, len);
    g_control.len += len;
    return true;
}

static void control_viewer(uint8_t type, uint32_t id)
{
    uint8_t msg[HMI_WIRE_VIEWER_MSG];
    if (!control_append(msg, hmi_wire_encode_viewer(type, id, msg)))
        hmi_log(LOG_ERROR, "no memory to answer viewer %u", (unsigned)id);
}

static void control_status(uint8_t state)
{
    uint8_t msg[HMI_WIRE_STATUS_MSG];
    if (!control_append(msg, hmi_wire_encode_status(state, msg)))
        hmi_log(LOG_ERROR, "no memory to report the HMI state");
}

/** The viewer with this id that is not closing, or NULL. */
static viewer_t *viewer_find(uint32_t id)
{
    for (viewer_t *v = g_viewers; v; v = v->next)
        if (v->id == id && v->state != VIEWER_CLOSING)
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
    if (v->state == VIEWER_CLOSING)
        return;
    if (v->state == VIEWER_OPEN && g_open_count > 0)
        g_open_count--;
    v->state     = VIEWER_CLOSING;
    g_structural = true;
}

/* ---------------------------------------------------------------------------------------- */
/* Tag access (frame thread)                                                                 */
/* ---------------------------------------------------------------------------------------- */

static bool tag_address(int tag, uint8_t *arr, uint16_t *elem)
{
    if (tag < 0 || tag >= hmi_tag_count)
        return false;
    const hmi_tag_t *t = &hmi_tags[tag];
    if (t->arr > UINT8_MAX || t->elem > UINT16_MAX)
        return false;
    *arr  = (uint8_t)t->arr;
    *elem = (uint16_t)t->elem;
    return true;
}

static bool tag_read(void *ctx, int tag, void *dest)
{
    (void)ctx;
    uint8_t arr;
    uint16_t elem;
    if (!g_live || !dest || !g_args.debug_read || !g_args.debug_size ||
        !tag_address(tag, &arr, &elem))
        return false;
    const uint16_t dsize = g_args.debug_size(arr, elem);
    if (dsize == 0 || dsize > HMI_CODEC_DEBUG_MAX)
        return false;
    uint8_t wire[HMI_CODEC_DEBUG_MAX];
    if (g_args.debug_read(arr, elem, wire) != dsize)
        return false;
    return hmi_codec_from_debug(hmi_tags[tag].type, hmi_tags[tag].max_len, wire, dsize, dest);
}

static void tag_write(void *ctx, int tag, const void *src)
{
    (void)ctx;
    uint8_t arr;
    uint16_t elem;
    if (!g_live || !src || !g_args.debug_write || !g_args.debug_size ||
        !tag_address(tag, &arr, &elem))
        return;
    const hmi_tag_t *t  = &hmi_tags[tag];
    const char *account = g_current ? g_current->account : "?";
    const char *path    = t->path ? t->path : "?";
    char value[160];
    hmi_codec_format(t->type, t->max_len, src, value, sizeof value);

    const uint16_t dsize = g_args.debug_size(arr, elem);
    uint8_t wire[HMI_CODEC_DEBUG_MAX];
    bool truncated = false;
    if (dsize == 0 || dsize > HMI_CODEC_DEBUG_MAX ||
        !hmi_codec_to_debug(t->type, t->max_len, src, wire, dsize, &truncated))
    {
        hmi_log(LOG_WARN, "write by %s: %s = %s refused: the variable does not match the HMI",
                account, path, value);
        return;
    }
    const uint8_t status = g_args.debug_write(arr, elem, wire, dsize);
    if (status != 0x7E)
    {
        hmi_log(LOG_WARN, "write by %s: %s = %s failed (status 0x%02X)", account, path, value,
                status);
        return;
    }
    hmi_log(LOG_INFO, "write by %s: %s = %s%s", account, path, value,
            truncated ? " (cut to the debugger's string length)" : "");
}

static const hmi_tag_access_t g_access = {tag_read, tag_write, NULL};

/* ---------------------------------------------------------------------------------------- */
/* Frame thread                                                                              */
/* ---------------------------------------------------------------------------------------- */

static int32_t css_to_lu(uint16_t css)
{
    return css > 0 ? (int32_t)css : 1;
}

static void viewer_free(viewer_t *v)
{
    if (v->unit)
        hmi_unit_destroy(v->unit);
    if (v->session)
        hmi_session_destroy(v->session);
    hmi_slot_free(&v->slot);
    free(v);
}

/** Creates the session and unit of pending viewers and frees closing ones. */
static void frame_structural(void)
{
    viewer_t *to_free = NULL;

    pthread_mutex_lock(&g_lock);
    g_structural  = false;
    viewer_t **pp = &g_viewers;
    while (*pp)
    {
        viewer_t *v = *pp;
        if (v->state == VIEWER_CLOSING)
        {
            *pp     = v->next;
            v->next = to_free;
            to_free = v;
            continue;
        }
        pp = &v->next;
    }
    /* Pending viewers stay listed (so messages reach their queue); collect pointers only. */
    size_t npending = 0;
    for (viewer_t *v = g_viewers; v; v = v->next)
        if (v->state == VIEWER_PENDING && !v->session)
            npending++;
    pthread_mutex_unlock(&g_lock);

    while (to_free)
    {
        viewer_t *v         = to_free;
        to_free             = v->next;
        const uint32_t id   = v->id;
        const bool was_open = v->was_open;
        viewer_free(v);
        if (was_open)
        {
            pthread_mutex_lock(&g_lock);
            size_t count = g_open_count;
            pthread_mutex_unlock(&g_lock);
            hmi_log(LOG_INFO, "viewer %u closed: %zu viewer(s)", (unsigned)id, count);
        }
    }

    while (npending > 0)
    {
        npending--;
        viewer_t *v = NULL;
        uint16_t w = 0, h = 0;
        uint8_t role = 0;
        pthread_mutex_lock(&g_lock);
        for (viewer_t *it = g_viewers; it; it = it->next)
        {
            if (it->state == VIEWER_PENDING && !it->session)
            {
                v                  = it;
                w                  = it->width;
                h                  = it->height;
                role               = it->role;
                it->viewport_dirty = false;
                break;
            }
        }
        pthread_mutex_unlock(&g_lock);
        if (!v)
            break;

        /* v cannot be freed meanwhile: only this thread frees viewers. */
        hmi_session_t *session = hmi_session_create();
        void *unit             = session ? hmi_unit_create() : NULL;
        if (session && unit)
        {
            hmi_session_use(session);
            hmi_core_init(css_to_lu(w), css_to_lu(h));
            hmi_unit_set_role(unit, role);
        }

        pthread_mutex_lock(&g_lock);
        v->session   = session;
        v->unit      = unit;
        bool opened  = false;
        size_t count = 0;
        if (!session || !unit)
        {
            if (v->state == VIEWER_PENDING)
                control_viewer(HMI_MSG_BUSY, v->id);
            v->state     = VIEWER_CLOSING;
            g_structural = true;
        }
        else if (v->state == VIEWER_PENDING)
        {
            v->state    = VIEWER_OPEN;
            v->was_open = true;
            g_open_count++;
            count  = g_open_count;
            opened = true;
            control_viewer(HMI_MSG_OPENED, v->id);
        }
        const uint32_t id = v->id;
        char account[HMI_WIRE_NAME_MAX + 1];
        memcpy(account, v->account, sizeof account);
        const uint8_t vrole = v->role;
        pthread_mutex_unlock(&g_lock);
        wake_io();

        if (opened)
            hmi_log(LOG_INFO, "viewer %u opened by %s (%s): %zu viewer(s)", (unsigned)id, account,
                    role_name(vrole), count);
        else if (!session || !unit)
            hmi_log(LOG_WARN, "viewer %u refused: no memory for its screens", (unsigned)id);
    }
}

static void apply_input(const hmi_input_event_t *ev, size_t n)
{
    for (size_t i = 0; i < n; i++)
    {
        if (ev[i].kind == HMI_INPUT_POINTER)
            hmi_input_pointer(ev[i].ptr_kind, ev[i].x_q4, ev[i].y_q4);
        else
            hmi_input_key(ev[i].code);
    }
}

/** Closes a viewer whose screen code raised an IEC fault and tells the web server with FAULT. */
static void viewer_fault(viewer_t *v)
{
    pthread_mutex_lock(&g_lock);
    const bool was_open = v->state == VIEWER_OPEN;
    if (was_open)
    {
        viewer_close_locked(v);
        control_viewer(HMI_MSG_FAULT, v->id);
    }
    pthread_mutex_unlock(&g_lock);
    hmi_log(LOG_WARN, "screen fault for %s: viewer %u closed", v->account, (unsigned)v->id);
    if (was_open)
        wake_io();
}

static void render_viewer(viewer_t *v, uint32_t time_ms, bool *stored_any)
{
    hmi_input_event_t ev[HMI_INPUT_QUEUE_CAP];
    size_t n     = 0;
    bool resize  = false;
    int32_t w_lu = 0;
    int32_t h_lu = 0;

    pthread_mutex_lock(&g_lock);
    if (v->state != VIEWER_OPEN)
    {
        pthread_mutex_unlock(&g_lock);
        return;
    }
    n = hmi_input_take_frame(&v->input, ev, HMI_INPUT_QUEUE_CAP);
    if (v->viewport_dirty)
    {
        resize            = true;
        w_lu              = css_to_lu(v->width);
        h_lu              = css_to_lu(v->height);
        v->viewport_dirty = false;
    }
    pthread_mutex_unlock(&g_lock);

    hmi_session_use(v->session);
    if (resize)
        hmi_set_screen_size(w_lu, h_lu);
    apply_input(ev, n);
    hmi_set_time_ms(time_ms);

    g_current = v;
    hmi_frame_begin();
    const bool ran      = hmi_unit_frame(v->unit);
    size_t len          = 0;
    const uint8_t *dl   = hmi_frame_end(&len);
    const uint64_t hash = hmi_frame_hash();
    g_current           = NULL;
    if (!ran)
    {
        viewer_fault(v);
        return;
    }

    if (hmi_frame_sign_out() && !v->sign_out_sent)
    {
        v->sign_out_sent = true;
        pthread_mutex_lock(&g_lock);
        const bool open = v->state == VIEWER_OPEN;
        if (open)
            control_viewer(HMI_MSG_SIGN_OUT, v->id);
        pthread_mutex_unlock(&g_lock);
        if (open)
        {
            hmi_log(LOG_INFO, "%s signed out from the screen (viewer %u)", v->account,
                    (unsigned)v->id);
            wake_io();
        }
    }

    const uint32_t status = hmi_frame_status();
    if (status & ~v->status_warned)
    {
        v->status_warned |= status;
        hmi_log(LOG_WARN, "viewer %u: the screen exceeds the HMI's limits (status 0x%X)",
                (unsigned)v->id, (unsigned)status);
    }

    pthread_mutex_lock(&g_lock);
    hmi_slot_result_t r = HMI_SLOT_UNCHANGED;
    if (v->state == VIEWER_OPEN)
    {
        r = hmi_slot_offer(&v->slot, v->id, dl, len, hash);
        if (r == HMI_SLOT_STORED)
        {
            g_frames_pending = true;
            *stored_any      = true;
        }
    }
    pthread_mutex_unlock(&g_lock);

    if (r == HMI_SLOT_FAILED && !v->offer_warned)
    {
        v->offer_warned = true;
        hmi_log(LOG_ERROR, "viewer %u: frame of %zu bytes could not be queued", (unsigned)v->id,
                len);
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

static void *frame_main(void *arg)
{
    (void)arg;
    set_default_priority();

    viewer_t **list       = NULL;
    size_t list_cap       = 0;
    const uint64_t t0     = now_ns();
    uint64_t next         = t0;
    const uint64_t period = (uint64_t)g_frame_ms * 1000000u;

    for (;;)
    {
        pthread_mutex_lock(&g_lock);
        while (!g_stop && !g_structural && now_ns() < next)
            cond_wait_until(next);
        const bool stop       = g_stop;
        const bool structural = g_structural;
        pthread_mutex_unlock(&g_lock);
        if (stop)
            break;
        if (structural)
            frame_structural();

        const uint64_t now = now_ns();
        if (now < next)
            continue;
        next += period;
        if (next <= now)
            next = now + period;

        /* Snapshot the open viewers; they stay allocated until this thread frees them. */
        size_t count = 0;
        pthread_mutex_lock(&g_lock);
        for (viewer_t *v = g_viewers; v; v = v->next)
        {
            if (v->state != VIEWER_OPEN)
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

        const uint32_t time_ms = (uint32_t)((now - t0) / 1000000u);
        bool stored            = false;
        for (size_t i = 0; i < count; i++)
            render_viewer(list[i], time_ms, &stored);
        if (stored)
            wake_io();
    }

    free(list);
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
    pthread_mutex_lock(&g_lock);
    g_frame_done = true;
    pthread_cond_broadcast(&g_cond);
    pthread_mutex_unlock(&g_lock);
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

/** CSS pixels x100 to Q4 logical units (1/16 of a CSS pixel), clamped. */
static int32_t to_q4(int32_t v100)
{
    int64_t q = ((int64_t)v100 * 16) / 100;
    if (q > POINTER_Q4_LIMIT)
        q = POINTER_Q4_LIMIT;
    if (q < -POINTER_Q4_LIMIT)
        q = -POINTER_Q4_LIMIT;
    return (int32_t)q;
}

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
        if (v->state != VIEWER_CLOSING)
        {
            viewer_close_locked(v);
            dropped++;
        }
    }
    g_control.len    = 0;
    g_frames_pending = false;
    pthread_cond_signal(&g_cond);
    pthread_mutex_unlock(&g_lock);
    hmi_log(LOG_INFO, "web server connection closed (%s): %zu viewer(s) dropped", why, dropped);
}

/** Queues input (g_lock held); returns false when the viewer was dropped for overflowing. */
static bool push_input(viewer_t *v, const hmi_input_event_t *ev)
{
    if (hmi_input_push(&v->input, ev))
        return true;
    /* More presses, releases and keys than frames apply: the viewer cannot be served. */
    viewer_close_locked(v);
    control_viewer(HMI_MSG_BUSY, v->id);
    pthread_cond_signal(&g_cond);
    return false;
}

/** Applies one message; returns false when it breaks the protocol. */
static bool handle_message(const hmi_wire_msg_t *m)
{
    bool ok         = true;
    bool overflowed = false;
    pthread_mutex_lock(&g_lock);
    viewer_t *v = m->type == HMI_MSG_OPEN ? NULL : viewer_find(m->viewer);

    switch (m->type)
    {
    case HMI_MSG_OPEN:
    {
        if (!g_live)
        {
            control_status(HMI_STATE_OUT_OF_DATE);
            break;
        }
        if (viewer_find(m->viewer))
        {
            ok = false;
            break;
        }
        if (!m->name_valid)
        {
            hmi_log(LOG_WARN, "viewer %u refused: its account name is not printable UTF-8",
                    (unsigned)m->viewer);
            control_viewer(HMI_MSG_BUSY, m->viewer);
            break;
        }
        viewer_t *nv = calloc(1, sizeof *nv);
        if (!nv)
        {
            control_viewer(HMI_MSG_BUSY, m->viewer);
            break;
        }
        nv->id     = m->viewer;
        nv->state  = VIEWER_PENDING;
        nv->role   = m->role;
        nv->width  = m->width;
        nv->height = m->height;
        memcpy(nv->account, m->name, (size_t)m->name_len + 1u);
        hmi_input_reset(&nv->input);
        viewer_append(nv);
        g_structural = true;
        pthread_cond_signal(&g_cond);
        break;
    }
    case HMI_MSG_CLOSE:
        if (v)
        {
            viewer_close_locked(v);
            pthread_cond_signal(&g_cond);
        }
        break;
    case HMI_MSG_VIEWPORT:
        if (v)
        {
            v->width          = m->width;
            v->height         = m->height;
            v->viewport_dirty = true;
        }
        break;
    case HMI_MSG_POINTER:
        if (v)
        {
            hmi_input_event_t ev = {HMI_INPUT_POINTER, m->kind, to_q4(m->x100), to_q4(m->y100), 0};
            overflowed           = !push_input(v, &ev);
        }
        break;
    case HMI_MSG_KEY:
        if (v)
        {
            hmi_input_event_t ev = {HMI_INPUT_KEY, 0, 0, 0, m->code};
            overflowed           = !push_input(v, &ev);
        }
        break;
    default:
        ok = false;
        break;
    }
    pthread_mutex_unlock(&g_lock);
    if (overflowed)
        hmi_log(LOG_WARN, "viewer %u dropped: its input queue overflowed", (unsigned)m->viewer);
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

/** Moves the next message to send into c->out: control first, then frames in turn. */
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
    else if (g_frames_pending)
    {
        viewer_t *taken = NULL;
        for (viewer_t *v = g_viewers; v; v = v->next)
        {
            if (v->state == VIEWER_OPEN && hmi_slot_take(&v->slot, &c->out))
            {
                taken = v;
                break;
            }
        }
        if (taken)
        {
            /* Round robin: the viewer just served goes last. */
            viewer_unlink(taken);
            viewer_append(taken);
            got = true;
        }
        else
        {
            g_frames_pending = false;
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
    bool pending = g_control.len > 0 || g_frames_pending;
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

static bool tag_table_valid(void)
{
    if (hmi_tag_count < 0)
        return false;
    for (int i = 0; i < hmi_tag_count; i++)
    {
        const hmi_tag_t *t = &hmi_tags[i];
        const size_t size  = hmi_codec_buffer_size(t->type, t->max_len);
        if (size == 0 || hmi_tag_size(i) != size || t->arr > UINT8_MAX || t->elem > UINT16_MAX)
        {
            hmi_log(LOG_ERROR, "tag %d (%s) cannot be served", i, t->path ? t->path : "?");
            return false;
        }
    }
    return true;
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

    g_frame_ms = hmi_frame_ms;
    if (g_frame_ms == 0)
        g_frame_ms = FRAME_MS_DEFAULT;
    if (g_frame_ms < FRAME_MS_MIN)
        g_frame_ms = FRAME_MS_MIN;
    if (g_frame_ms > FRAME_MS_MAX)
        g_frame_ms = FRAME_MS_MAX;

    g_live = hmi_md5_live(g_args.program_md5, hmi_plc_md5) && tag_table_valid();
    if (g_live)
        hmi_log(LOG_INFO, "screens match the PLC program (%d tags, frame %u ms)", hmi_tag_count,
                (unsigned)g_frame_ms);
    else
        hmi_log(LOG_INFO, "screens out of date: PLC program '%s', screens built for '%s'",
                g_args.program_md5, hmi_plc_md5);
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
    g_frame_done     = false;
    g_structural     = false;
    g_frames_pending = false;
    g_open_count     = 0;
    g_control.len    = 0;
    pthread_mutex_unlock(&g_lock);

    hmi_set_tag_access(&g_access);
    if (start_thread(&g_frame_thread, frame_main) != 0)
    {
        hmi_set_tag_access(NULL);
        close_fds();
        (void)unlink(HMI_SOCKET_PATH);
        hmi_log(LOG_ERROR, "cannot start the frame thread");
        return -1;
    }
    if (start_thread(&g_io_thread, io_main) != 0)
    {
        pthread_mutex_lock(&g_lock);
        g_stop = true;
        pthread_cond_signal(&g_cond);
        pthread_mutex_unlock(&g_lock);
        pthread_join(g_frame_thread, NULL);
        hmi_set_tag_access(NULL);
        close_fds();
        (void)unlink(HMI_SOCKET_PATH);
        hmi_log(LOG_ERROR, "cannot start the socket thread");
        return -1;
    }
    g_running = true;
    hmi_log(LOG_INFO, "serving viewers on %s", HMI_SOCKET_PATH);
    return 0;
}

/**
 * Joins the frame thread once its current frame returns. Screen code that never returns would keep
 * running code plc_main is about to unload, so past HMI_STOP_TIMEOUT_MS plc_main exits and the
 * runtime restarts it.
 */
static void join_frame_thread(void)
{
    const uint64_t deadline = now_ns() + (uint64_t)HMI_STOP_TIMEOUT_MS * 1000000u;
    pthread_mutex_lock(&g_lock);
    while (!g_frame_done && now_ns() < deadline)
        cond_wait_until(deadline);
    const bool done = g_frame_done;
    pthread_mutex_unlock(&g_lock);
    if (!done)
    {
        hmi_log(LOG_ERROR, "screen code has not returned for %u ms; ending plc_main",
                (unsigned)HMI_STOP_TIMEOUT_MS);
        _exit(HMI_EXIT_SCREEN_STUCK);
    }
    pthread_join(g_frame_thread, NULL);
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
    join_frame_thread();
    g_running = false;

    hmi_set_tag_access(NULL);
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
    g_have_args = false;
}
