// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file ptrprobe.c
 * @brief Test-only native plugin that reports the I/O image pointer it holds.
 *
 * init() copies the runtime args and logs "ptrprobe: image=0x<pointer>", or
 * "ptrprobe: image=null". detach_image() drops every image, journal and debug
 * entry point from that copy and logs "ptrprobe: image=none".
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "plugin_detach.h"
#include "plugin_types.h"

static plugin_runtime_args_t g_args;
static int g_have_args = 0;

static void probe_log(const char *msg)
{
    if (g_have_args && g_args.log_info)
    {
        g_args.log_info("%s", msg);
    }
    else
    {
        fprintf(stderr, "[PTRPROBE] %s\n", msg);
    }
}

static void log_held_image(void)
{
    char msg[64];
    const void *image = (const void *)g_args.bool_input;
    if (image)
    {
        snprintf(msg, sizeof(msg), "ptrprobe: image=0x%llx",
                 (unsigned long long)(uintptr_t)image);
    }
    else
    {
        snprintf(msg, sizeof(msg), "ptrprobe: image=null");
    }
    probe_log(msg);
}

int init(void *args)
{
    memset(&g_args, 0, sizeof(g_args));
    g_have_args = 0;
    if (args)
    {
        memcpy(&g_args, args, sizeof(g_args));
        g_have_args = 1;
    }
    log_held_image();
    return 0;
}

int start_loop(void)
{
    return 0;
}

void stop_loop(void) {}

int detach_image(void)
{
    plugin_args_drop_image(&g_args);
    if (plugin_args_hold_image(&g_args))
    {
        log_held_image();
        return -1;
    }
    probe_log("ptrprobe: image=none");
    return 0;
}

void cleanup(void)
{
    probe_log("ptrprobe: cleanup");
    memset(&g_args, 0, sizeof(g_args));
    g_have_args = 0;
}
