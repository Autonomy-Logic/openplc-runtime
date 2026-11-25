// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/* Test-only board (VPP) plugin of the HMI end-to-end test: logs each lifecycle call, touches no I/O. */

#include <string.h>

#include "plugin_types.h"

static plugin_runtime_args_t g_args;
static int g_have_args;

static void say(const char *msg)
{
    if (g_have_args && g_args.log_info)
        g_args.log_info("%s", msg);
}

int init(void *args)
{
    g_have_args = args != NULL;
    if (args)
        memcpy(&g_args, args, sizeof(g_args));
    say("E2E-VPP init");
    return 0;
}

int start_loop(void)
{
    say("E2E-VPP start_loop");
    return 0;
}

void stop_loop(void)
{
    say("E2E-VPP stop_loop");
}

void cleanup(void)
{
    say("E2E-VPP cleanup");
}
