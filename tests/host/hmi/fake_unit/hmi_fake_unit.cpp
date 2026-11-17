// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

// Fake HMI unit for the host tests: four tags; a press toggles LAMP, writes LABEL and bumps
// SPEED (not for the viewer role) and records its position; a key writes its code to SPEED,
// and HMI_FAKE_FAULT_KEY makes the frame report an IEC fault.

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <new>
#include <thread>

#include "hmi_core.h"
#include "hmi_unit.h"

#ifndef HMI_FAKE_MD5
#define HMI_FAKE_MD5 "0123456789abcdef0123456789abcdef"
#endif
// Debug elements (array 0) of the four tags; a test against a real program overrides them.
#ifndef HMI_FAKE_ELEM_LAMP
#define HMI_FAKE_ELEM_LAMP 1u
#endif
#ifndef HMI_FAKE_ELEM_SPEED
#define HMI_FAKE_ELEM_SPEED 2u
#endif
#ifndef HMI_FAKE_ELEM_LABEL
#define HMI_FAKE_ELEM_LABEL 3u
#endif
#ifndef HMI_FAKE_ELEM_TEMP
#define HMI_FAKE_ELEM_TEMP 4u
#endif
/** Key code that makes hmi_unit_frame report an IEC fault for that viewer. */
#define HMI_FAKE_FAULT_KEY 0xE000u
/** Key code that makes hmi_unit_frame never return. */
#define HMI_FAKE_SPIN_KEY 0xE001u
/** Key code that makes the frame ask to sign the viewer out. */
#define HMI_FAKE_SIGN_OUT_KEY 0xE002u

namespace
{
const hmi_tag_access_t *g_access = nullptr;
int g_fail_creates               = 0;
std::atomic<int32_t> g_press_x{-1};
std::atomic<int32_t> g_press_y{-1};

struct Instance
{
    int role;
};

template <typename T> T read_tag(int tag)
{
    T v{};
    if (g_access && g_access->read)
        g_access->read(g_access->ctx, tag, &v);
    return v;
}

void write_tag(int tag, const void *src)
{
    if (g_access && g_access->write)
        g_access->write(g_access->ctx, tag, src);
}
} // namespace

extern "C" {

const hmi_tag_t hmi_tags[4] = {
    {0u, HMI_FAKE_ELEM_LAMP, HMI_TAG_BOOL, 0, "CONFIG0.RES0.INST0.MAIN.LAMP"},
    {0u, HMI_FAKE_ELEM_SPEED, HMI_TAG_INT, 0, "CONFIG0.RES0.INST0.MAIN.SPEED"},
    {0u, HMI_FAKE_ELEM_LABEL, HMI_TAG_STRING, 20, "CONFIG0.RES0.INST0.MAIN.LABEL"},
    {0u, HMI_FAKE_ELEM_TEMP, HMI_TAG_REAL, 0, "CONFIG0.RES0.INST0.MAIN.TEMP"},
};
const int hmi_tag_count             = 4;
const char hmi_plc_md5[]            = HMI_FAKE_MD5;
const uint32_t hmi_frame_ms         = 20;
const char hmi_panel_pin[]          = "";
const uint32_t hmi_relock_minutes   = 0;

/** Test hook: the next n hmi_unit_create calls fail. */
__attribute__((visibility("default"))) void hmi_fake_fail_creates(int n)
{
    g_fail_creates = n;
}

/** Test hook: position, in logical units, of the last press any viewer applied; -1 before one. */
__attribute__((visibility("default"))) void hmi_fake_last_press(int32_t *x, int32_t *y)
{
    *x = g_press_x.load();
    *y = g_press_y.load();
}

void hmi_set_tag_access(const hmi_tag_access_t *access)
{
    g_access = access;
}

size_t hmi_tag_size(int tag)
{
    switch (tag)
    {
    case 0:
        return 1;
    case 1:
        return 2;
    case 2:
        return 21;
    case 3:
        return 4;
    default:
        return 0;
    }
}

int hmi_tag_read(int tag, const void *variable, void *buf)
{
    (void)variable;
    if (tag < 0 || tag >= hmi_tag_count || !g_access || !g_access->read)
        return -1;
    return g_access->read(g_access->ctx, tag, buf) ? 0 : -1;
}

int hmi_tag_write(int tag, void *variable, const void *buf)
{
    (void)tag;
    (void)variable;
    (void)buf;
    return -1;
}

size_t hmi_unit_size(void)
{
    return sizeof(Instance);
}

void *hmi_unit_create(void)
{
    if (g_fail_creates > 0)
    {
        g_fail_creates--;
        return nullptr;
    }
    return new (std::nothrow) Instance{};
}

void hmi_unit_destroy(void *unit)
{
    delete static_cast<Instance *>(unit);
}

void hmi_unit_set_role(void *unit, int role)
{
    if (unit)
        static_cast<Instance *>(unit)->role = role;
}

bool hmi_unit_frame(void *unit)
{
    const Instance *in  = static_cast<const Instance *>(unit);
    const uint8_t lamp  = read_tag<uint8_t>(0);
    const int16_t speed = read_tag<int16_t>(1);

    hmi_clear(lamp ? 0x00FF00u : 0x202020u);
    hmi_fill_rect(0, 0, speed < 0 ? 0 : speed, 10, 0, 0xFF0000u);
    hmi_fill_rect(0, 20, 10 * (in->role + 1), 10, 0, 0x0000FFu);

    if (hmi_ptr_pressed())
    {
        g_press_x.store(hmi_ptr_x());
        g_press_y.store(hmi_ptr_y());
    }
    if (hmi_ptr_pressed() && in->role != HMI_ROLE_VIEWER)
    {
        const uint8_t toggled = lamp ? 0 : 1;
        write_tag(0, &toggled);
        const int16_t next = static_cast<int16_t>(speed + 1);
        write_tag(1, &next);
        char label[21] = "pressed";
        write_tag(2, label);
    }
    const uint32_t key = hmi_key_next();
    if (key == HMI_FAKE_FAULT_KEY)
        return false;
    while (key == HMI_FAKE_SPIN_KEY)
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    if (key == HMI_FAKE_SIGN_OUT_KEY)
    {
        hmi_sign_out();
        return true;
    }
    if (key != 0)
    {
        const int16_t code = static_cast<int16_t>(key);
        write_tag(1, &code);
    }
    return true;
}

} // extern "C"
