/**
 * @file plc_retain_file_store.cpp
 * @brief The runtime's own retain backend. See plc_retain_file_store.h.
 */

#include "plc_retain_file_store.h"

#include <atomic>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <libgen.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

extern "C" {
#include "utils/log.h"
}

namespace {

/* Matches plc_retain.cpp's RETAIN_BUFFER_MAX. A blob larger than the runtime
 * will marshal cannot reach us, so this is a ceiling on what we will hold, not
 * a limit anyone is expected to meet. */
constexpr size_t RETAIN_MAX = 64 * 1024;

std::mutex           g_lock;
std::vector<uint8_t> g_pending;
bool                 g_dirty = false;

std::string       g_path;
int               g_flush_seconds = 5;
std::atomic<bool> g_enabled{false};
std::atomic<bool> g_running{false};
std::thread       g_flusher;

/** Trim ASCII whitespace from both ends, in place. */
std::string trimmed(const std::string &s)
{
    const size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) return "";
    const size_t e = s.find_last_not_of(" \t\r\n");
    return s.substr(b, e - b + 1);
}

/**
 * Parse `retain.conf`. A missing file is not an error — it means nobody has
 * configured retention on this device, which is the default state.
 */
void read_config(const char *config_path)
{
    g_enabled.store(false);
    g_path.clear();
    g_flush_seconds = 5;

    FILE *f = fopen(config_path, "r");
    if (!f) return;

    bool enabled = false;
    char line[1024];
    while (fgets(line, sizeof(line), f))
    {
        std::string s = trimmed(line);
        if (s.empty() || s[0] == '#') continue;
        const size_t eq = s.find('=');
        if (eq == std::string::npos) continue;
        const std::string key = trimmed(s.substr(0, eq));
        const std::string val = trimmed(s.substr(eq + 1));

        if (key == "enabled")            enabled = (val == "1" || val == "true");
        else if (key == "path")          g_path = val;
        else if (key == "flush_seconds") g_flush_seconds = atoi(val.c_str());
    }
    fclose(f);

    if (g_flush_seconds < 1) g_flush_seconds = 1;
    /* Enabled with no path is a misconfiguration, not a request to write
     * somewhere arbitrary. Treat it as off and say so. */
    if (enabled && g_path.empty())
    {
        log_warn("Retain: the built-in store is enabled but no path is set — leaving it off");
        enabled = false;
    }
    g_enabled.store(enabled);
}

/**
 * Publish the blob.
 *
 * Write-and-rename, so a power loss mid-write leaves the PREVIOUS good blob
 * rather than a half-written one. The runtime's crc would catch a torn write
 * and fall back to initial values anyway, but losing the previous values as
 * well would be gratuitous.
 */
/*
 * Guards the STORE, not the staging buffer.
 *
 * `g_lock` covers `g_pending` and is deliberately released before the write, so
 * the scan thread never waits on a disk I/O. That leaves the file itself
 * unguarded, and two threads reach it: the flusher, and `clear()` on the
 * control-socket thread when a program is uploaded. Without this, a clear could
 * `remove()` the file while an in-flight commit was between its write and its
 * rename — the rename then republished the blob a moment after it was supposed
 * to be gone, and the next start restored values from the PREVIOUS program.
 */
std::mutex g_store_lock;

void commit(const uint8_t *buf, uint16_t len)
{
    std::lock_guard<std::mutex> store(g_store_lock);

    const std::string tmp = g_path + ".tmp";

    FILE *f = fopen(tmp.c_str(), "wb");
    if (!f)
    {
        /* Said once per failure rather than swallowed: a store that cannot
         * write is indistinguishable from one that never had anything to
         * write, and the difference matters after a power cut. */
        log_warn("Retain: cannot write %s — retained values will not be kept", tmp.c_str());
        return;
    }
    const bool wrote = fwrite(buf, 1, len, f) == len;
    if (wrote)
    {
        fflush(f);
        fsync(fileno(f));
    }
    fclose(f);
    if (!wrote)
    {
        remove(tmp.c_str());
        log_warn("Retain: short write to %s — keeping the previous stored values", tmp.c_str());
        return;
    }

    if (rename(tmp.c_str(), g_path.c_str()) != 0)
    {
        remove(tmp.c_str());
        log_warn("Retain: cannot publish %s — keeping the previous stored values", g_path.c_str());
        return;
    }

    /* fsync the DIRECTORY too. fsync on the file commits its contents; the
     * rename that publishes them is a directory operation, and on ext4 it can
     * still be lost to a power cut after the data is safely on disk. Without
     * this the store can come back holding the previous blob even though the
     * new one was written — the failure that looks like retain silently
     * skipping an interval. */
    std::vector<char> dircopy(g_path.begin(), g_path.end());
    dircopy.push_back('\0');
    const int dirfd = open(dirname(dircopy.data()), O_RDONLY | O_DIRECTORY);
    if (dirfd >= 0)
    {
        fsync(dirfd);
        close(dirfd);
    }
}

void flush_loop()
{
    while (g_running.load())
    {
        for (int i = 0; i < g_flush_seconds && g_running.load(); i++)
        {
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }
        if (!g_running.load()) break;

        std::vector<uint8_t> snapshot;
        {
            /* Copy under the lock, write outside it: the scan thread calls
             * save() every cycle and must never wait on a disk write. */
            std::lock_guard<std::mutex> guard(g_lock);
            if (!g_dirty) continue;
            snapshot = g_pending;
            g_dirty  = false;
        }
        if (!snapshot.empty()) commit(snapshot.data(), (uint16_t)snapshot.size());
    }
}

}  // namespace

bool plc_retain_file_store_start(const char *config_path)
{
    plc_retain_file_store_stop();
    read_config(config_path ? config_path : "./retain.conf");
    if (!g_enabled.load()) return false;

    g_running.store(true);
    g_flusher = std::thread(flush_loop);
    log_info("Retain: built-in file store enabled — %s, flushing every %ds",
             g_path.c_str(), g_flush_seconds);
    return true;
}

void plc_retain_file_store_stop(void)
{
    if (g_running.exchange(false))
    {
        if (g_flusher.joinable()) g_flusher.join();

        /* Final flush: a clean stop should not discard the last interval. */
        std::lock_guard<std::mutex> guard(g_lock);
        if (g_dirty && !g_pending.empty())
        {
            commit(g_pending.data(), (uint16_t)g_pending.size());
            g_dirty = false;
        }
    }
    g_enabled.store(false);
}

bool plc_retain_file_store_active(void)
{
    return g_enabled.load();
}

const char *plc_retain_file_store_path(void)
{
    return g_path.c_str();
}

int plc_retain_file_store_save(const uint8_t *blob, uint16_t len)
{
    if (!g_enabled.load() || !blob || len == 0 || len > RETAIN_MAX) return -1;

    std::lock_guard<std::mutex> guard(g_lock);
    /* Only mark dirty on an actual change. The runtime deliberately does not
     * diff — it cannot know what a write costs here — so doing it at this layer
     * is how a slow medium avoids rewriting an unchanged blob every interval. */
    if (g_pending.size() != len || memcmp(g_pending.data(), blob, len) != 0)
    {
        g_pending.assign(blob, blob + len);
        g_dirty = true;
    }
    return 0;
}

int plc_retain_file_store_load(uint8_t *out, uint16_t cap, uint16_t *out_len)
{
    if (out_len) *out_len = 0;
    if (!g_enabled.load() || !out || cap == 0) return -1;

    FILE *f = fopen(g_path.c_str(), "rb");
    if (!f) return -1; /* nothing stored — a first boot, or freshly cleared */
    const size_t n = fread(out, 1, cap, f);
    fclose(f);
    if (n == 0) return -1;

    if (out_len) *out_len = (uint16_t)n;

    /* Prime the in-memory copy so the first flush after start does not rewrite
     * a byte-identical file. */
    std::lock_guard<std::mutex> guard(g_lock);
    g_pending.assign(out, out + n);
    g_dirty = false;
    return 0;
}

int plc_retain_file_store_clear(void)
{
    {
        std::lock_guard<std::mutex> guard(g_lock);
        g_pending.clear();
        g_dirty = false;
    }
    /* Not gated on `enabled`: a clear has to remove what a PREVIOUS
     * configuration stored, which is the whole point of clearing on upload.
     *
     * Under `g_store_lock`, so a commit that is mid write-and-rename finishes
     * first and this removes the file it published — rather than the rename
     * landing after the remove and resurrecting the blob. */
    if (!g_path.empty())
    {
        std::lock_guard<std::mutex> store(g_store_lock);
        remove(g_path.c_str());
        remove((g_path + ".tmp").c_str());
    }
    return 0;
}
