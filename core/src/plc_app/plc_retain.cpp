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
}

#include "debug_write_journal.h"
#include "image_tables.h"

extern plugin_driver_t *plugin_driver;

namespace {

/**
 * Cap on the blob this runtime will handle.
 *
 * Generous compared with baremetal's 512 bytes — there is no SRAM pressure
 * here — but bounded on purpose: the buffer is read from the scan path, and an
 * unbounded allocation driven by a program's declaration count is not
 * something to discover on a running machine. A program needing more is
 * refused at init with a message naming both numbers.
 */
constexpr size_t RETAIN_BUFFER_MAX = 64 * 1024;

std::vector<uint8_t> g_buffer;
std::atomic<bool>    g_active{false};

/**
 * Restore writes go through the runtime's external-write path, NOT straight to
 * the IECVar.
 *
 * A retained variable may also be located (`VAR RETAIN x AT %MW10`). Poking
 * such a leaf's storage directly is undone by the next copy-in from the process
 * image, so the value would appear to restore and then silently revert on the
 * first scan. `runtime_external_write` classifies the leaf and routes a located
 * one through the image journal — the same path OPC-UA writes take.
 *
 * DBGW_OP_WRITE, never a force: restoring a retained value must not pin it. The
 * program has to be able to move it on the very next scan, and an operator's
 * force has to stay authoritative over whatever was stored.
 */
uint8_t retain_write_leaf(uint8_t arr, uint16_t elem, const uint8_t *bytes, uint16_t len)
{
    return runtime_external_write(arr, elem, (uint8_t)DBGW_OP_WRITE, bytes, len) == 0 ? 0x7E : 0x82;
}

/** The first plugin that exports both hooks wins; see plc_retain_init(). */
plugin_instance_t *g_store = nullptr;

}  // namespace

void plc_retain_init(void)
{
    g_active.store(false);
    g_store = nullptr;
    g_buffer.clear();

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

    g_store = plugin_driver_find_retain_store(plugin_driver);
    if (!g_store)
    {
        /* Info, not a warning: a device with no retention plugin is a normal
         * configuration, and the program still runs correctly — its retained
         * variables just behave as NON_RETAIN. Said once so the operator can
         * tell "not supported here" from "supported and broken". */
        log_info("Retain: %zu bytes of retained variables, but no plugin provides storage — "
                 "they will start at their initial values",
                 needed);
        return;
    }

    g_buffer.assign(needed, 0);
    g_active.store(true);
    log_info("Retain: %zu bytes across the program's retained variables (layout %08x)",
             needed, ext_strucpp_retain_layout_hash ? ext_strucpp_retain_layout_hash() : 0u);
}

void plc_retain_load(void)
{
    if (!g_active.load() || !g_store) return;

    uint16_t got = 0;
    const int rc = plugin_driver_retain_load(g_store, g_buffer.data(),
                                             (uint16_t)g_buffer.size(), &got);
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
    if (!g_active.load() || !g_store) return;

    const size_t n = ext_strucpp_retain_pack(g_buffer.data(), g_buffer.size());
    if (n == 0) return;

    /* Hand the bytes over and return. Whether this is committed to storage now,
     * in five seconds, or on shutdown is the plugin's decision — it is the only
     * layer that knows what its medium costs. */
    plugin_driver_retain_save(g_store, g_buffer.data(), (uint16_t)n);
}

void plc_retain_clear(void)
{
    /* Not gated on g_active: a clear has to work even when this program has
     * nothing retained, because what it is discarding belongs to the PREVIOUS
     * program. That is the whole point of clearing on upload. */
    plugin_instance_t *store = g_store ? g_store : plugin_driver_find_retain_store(plugin_driver);
    if (!store) return;
    plugin_driver_retain_clear(store);
}

bool plc_retain_active(void)
{
    return g_active.load();
}
