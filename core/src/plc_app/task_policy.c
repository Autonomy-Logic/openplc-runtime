// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#include "task_policy.h"

int plc_task_fifo_priority(int iec_priority, bool *clamped)
{
    int p    = iec_priority;
    bool out = false;
    if (p < PLC_IEC_PRIORITY_MIN)
    {
        p   = PLC_IEC_PRIORITY_MIN;
        out = true;
    }
    else if (p > PLC_IEC_PRIORITY_MAX)
    {
        p   = PLC_IEC_PRIORITY_MAX;
        out = true;
    }
    if (clamped)
        *clamped = out;
    return PLC_FIFO_TASK_MAX - p;
}
