// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file plc_retain.cpp
 * @brief Retain-variable persistence — the runtime's half (NODE-94).
 *
 * See plc_retain.h for the split: the .so marshals, a plugin stores, and this
 * file owns the buffer and the call sites.
 */

#include "plc_retain.h"

#include <atomic>
#include <cstring>
#include <vector>

extern "C" {
#include "../drivers/plugin_driver.h"
#include "utils/log.h"
#include "utils/utils.h"  // ext_strucpp_program_md5 — the program's identity
}

#include "debug_write_journal.h"
#include "image_tables.h"
#include "plc_retain_file_store.h"

extern plugin_driver_t *plugin_driver;

namespace {

/* Cap on the retain blob. Read on the scan path, so an unbounded size
 * driven by a program's declaration count is refused at init. */
constexpr size_t RETAIN_BUFFER_MAX = 64 * 1024;

std::vector<uint8_t> g_buffer;
std::atomic<bool>    g_active{false};

/* Restore via runtime_external_write (not direct IECVar poke): routes a
 * located leaf through the image journal so copy_in won't revert it.
 * DBGW_OP_WRITE, never a force — a restore must not pin the slot. */
uint8_t retain_write_leaf(uint8_t arr, uint16_t elem, const uint8_t *bytes, uint16_t len)
{
    return runtime_external_write(arr, elem, (uint8_t)DBGW_OP_WRITE, bytes, len) == 0 ? 0x7E : 0x82;
}

/* Uniform store record (name + 3 fn pointers). Past init() every path
 * calls through this, so plugin and file store share one code path. */
struct RetainDriver
{
    const char *name;
    int (*read)(const char *program_md5, uint16_t md5_len, uint8_t *out, uint16_t cap,
                uint16_t *out_len);
    int (*write)(const uint8_t *blob, uint16_t len);
    int (*flush)(void);
};

/* Written only by plc_retain_init(), read from the scan thread.
 * g_active is the publication barrier: init() stores false (seq_cst)
 * before mutating g_driver and true after; readers gate on g_active. */
RetainDriver g_driver = {nullptr, nullptr, nullptr, nullptr};

/* The plugin acting as the store, when a plugin claimed it. Held only so the
 * thunks below have something to forward to — the plugin hooks are instance
 * methods and the driver record is plain function pointers. */
plugin_instance_t *g_plugin_store = nullptr;

int plugin_read_thunk(const char *program_md5, uint16_t md5_len, uint8_t *out, uint16_t cap,
                      uint16_t *out_len)
{
    return plugin_driver_retain_load(g_plugin_store, program_md5, md5_len, out, cap, out_len);
}

int plugin_write_thunk(const uint8_t *blob, uint16_t len)
{
    return plugin_driver_retain_save(g_plugin_store, blob, len);
}

int plugin_flush_thunk(void)
{
    return plugin_driver_retain_flush(g_plugin_store);
}

/** Whether a store is bound. Cheap enough to ask on the scan path. */
bool driver_bound()
{
    return g_driver.read != nullptr && g_driver.write != nullptr;
}

}  // namespace

void plc_retain_init(void)
{
    g_active.store(false);
    g_plugin_store = nullptr;
    g_driver       = {nullptr, nullptr, nullptr, nullptr};
    g_buffer.clear();
    /* Re-read retain.conf per program load; stopping first forces the
     * re-read and commits anything the previous run was still holding. */
    plc_retain_file_store_stop();

    if (!ext_strucpp_retain_blob_size || !ext_strucpp_retain_pack || !ext_strucpp_retain_unpack)
    {
        /* A program built by an older STruC++. Not an error — retain simply
         * does not run, exactly as before these exports existed. */
        return;
    }

    const size_t needed = ext_strucpp_retain_blob_size();
    if (needed == 0) return; /* the program retains nothing */

    if (needed > RETAIN_BUFFER_MAX)
    {
        log_error("Retain: program needs %zu bytes, this runtime handles at most %zu — "
                  "retained variables will NOT be preserved",
                  needed, RETAIN_BUFFER_MAX);
        return;
    }

    /* Pick a store in rank order: a VPP that owns retention wins over the
     * built-in file store. In a correct device only one offers itself. */
    g_plugin_store = plugin_driver_find_retain_store(plugin_driver);
    if (g_plugin_store)
    {
        g_driver = {g_plugin_store->config.name, plugin_read_thunk, plugin_write_thunk,
                    plugin_flush_thunk};
    }
    else if (plc_retain_file_store_start("./retain.conf"))
    {
        g_driver = {plc_retain_file_store_path(), plc_retain_file_store_load,
                    plc_retain_file_store_save, plc_retain_file_store_flush};
    }
    else
    {
        /* Info, not a warning: a device with no retention configured is a
         * normal state, and the program still runs correctly — its retained
         * variables just behave as NON_RETAIN. Said once so the operator can
         * tell "not switched on here" from "switched on and broken". */
        log_info("Retain: %zu bytes of retained variables, but no storage is configured — "
                 "they will start at their initial values. Turn on persistent storage in the "
                 "project and upload again, or install a VPP that provides a store.",
                 needed);
        return;
    }

    g_buffer.assign(needed, 0);
    g_active.store(true);
    log_info("Retain: %zu bytes across the program's retained variables (layout %08x), stored by %s",
             needed, ext_strucpp_retain_layout_hash ? ext_strucpp_retain_layout_hash() : 0u,
             g_driver.name ? g_driver.name : "?");
}

void plc_retain_read(void)
{
    if (!g_active.load() || !driver_bound()) return;

    /* Program identity (32 hex, NOT NUL-terminated: length travels),
     * resolved from the .so at load; drivers compare it to tell a new
     * program from the old one. */
    if (!ext_strucpp_program_md5)
    {
        /* No identity means a driver cannot tell programs apart; stand
         * the store down (g_active=false) rather than leave saves running
         * against bytes no driver can ever commit. */
        log_warn("Retain: the program exports no MD5 — retained variables start at their "
                 "initial values");
        g_active.store(false);
        return;
    }

    uint16_t  got = 0;
    const int rc  = g_driver.read(ext_strucpp_program_md5, PLC_RETAIN_PROGRAM_ID_LEN,
                                 g_buffer.data(), (uint16_t)g_buffer.size(), &got);
    if (rc != 0 || got == 0)
    {
        log_info("Retain: nothing stored yet — retained variables start at their initial values");
        return;
    }

    const uint8_t res = ext_strucpp_retain_unpack(g_buffer.data(), got, retain_write_leaf);
    if (res == 0)
    {
        log_info("Retain: restored %u bytes of retained variables", (unsigned)got);
        return;
    }

    /* Deliberately explicit about WHICH check failed. "Retain refused" sends
     * someone hunting; "the stored layout is from a different program" tells
     * them it was the upload, and a crc failure tells them it was the store. */
    static const char *why[] = {"ok",        "no data",       "bad magic",
                                "bad format", "crc mismatch", "layout is from a different program",
                                "truncated"};
    log_warn("Retain: stored values refused (%s) — retained variables start at their initial values",
             res < (sizeof(why) / sizeof(why[0])) ? why[res] : "unknown");
}

void plc_retain_save(void)
{
    if (!g_active.load() || !driver_bound()) return;

    const size_t n = ext_strucpp_retain_pack(g_buffer.data(), g_buffer.size());
    if (n == 0) return;

    /* Hand the bytes over and return. Whether this is committed to storage now,
     * in five seconds, or on shutdown is the driver's decision — it is the only
     * layer that knows what its medium costs. */
    g_driver.write(g_buffer.data(), (uint16_t)n);
}

void plc_retain_flush(void)
{
    if (!g_active.load() || !g_driver.flush) return;
    g_driver.flush();
}

