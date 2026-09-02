/**
 * @file plc_retain_file_store.cpp
 * @brief The runtime's own retain backend. See plc_retain_file_store.h.
 */

#include "plc_retain_file_store.h"

#include "plc_retain.h"  // PLC_RETAIN_PROGRAM_ID_LEN — one definition for both sides

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

/* Length of the program identity stored ahead of the blob. Mirrors baremetal's
 * OPLC_RETAIN_PROGRAM_ID_LEN: an MD5 as lower-case hex, 32 characters, never
 * NUL-terminated on the wire or on disk. */
constexpr size_t PROGRAM_ID_LEN = PLC_RETAIN_PROGRAM_ID_LEN;
static_assert(PROGRAM_ID_LEN == PLC_RETAIN_PROGRAM_ID_LEN,
              "The identity this store writes into its on-disk header must be exactly the "
              "identity the runtime hands to read() — a shorter or longer header would be "
              "indistinguishable from a torn write and every load would discard good values.");

std::mutex           g_lock;
std::vector<uint8_t> g_pending;
bool                 g_dirty = false;

/* The running program's identity, taken from the last load() and committed
 * alongside the blob by every save(). Held rather than written at load time so
 * a read never mutates storage, and so identity and bytes always reach the disk
 * as one unit — a file whose header says "program A" is guaranteed to contain
 * program A's values. Empty until the first load(). */
std::string          g_program_md5;

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
void commit(const uint8_t *buf, uint16_t len, const std::string &program_id)
{
    const std::string tmp = g_path + ".tmp";

    /* File layout: [PROGRAM_ID_LEN bytes of md5 hex][blob].
     *
     * The identity goes in the same file as the bytes, and the same
     * write-and-rename publishes both, so the two can never disagree — a
     * separate sidecar could be updated and then lost, leaving one program's
     * values labelled with another's. A file that predates this header, or a
     * torn one shorter than the header, simply fails the identity check on the
     * next load and is discarded, which is the correct outcome for bytes whose
     * owner cannot be established. */

    FILE *f = fopen(tmp.c_str(), "wb");
    if (!f)
    {
        /* Said once per failure rather than swallowed: a store that cannot
         * write is indistinguishable from one that never had anything to
         * write, and the difference matters after a power cut. */
        log_warn("Retain: cannot write %s — retained values will not be kept", tmp.c_str());
        return;
    }
    /* Header first. g_program_md5 is empty only if something committed before
     * any load ran, which the call order rules out — but a short header would
     * be indistinguishable from a torn write, so refuse rather than publish a
     * file whose owner is blank. */
    bool wrote = program_id.size() == PROGRAM_ID_LEN &&
                 fwrite(program_id.data(), 1, PROGRAM_ID_LEN, f) == PROGRAM_ID_LEN;
    if (wrote) wrote = fwrite(buf, 1, len, f) == len;
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

/** Remove the stored file and forget the buffered blob.
 *
 * Not gated on `enabled`: what is being discarded belongs to a PREVIOUS
 * program, and may have been written while the store was configured
 * differently. The identity is deliberately NOT cleared — the caller has just
 * set it to the program now running, and the next save has to label its bytes.
 */
void discard_stored()
{
    {
        std::lock_guard<std::mutex> guard(g_lock);
        g_pending.clear();
        g_dirty = false;
    }
    if (!g_path.empty())
    {
        remove(g_path.c_str());
        remove((g_path + ".tmp").c_str());
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
        std::string          snapshot_id;
        {
            /* Copy under the lock, write outside it: the scan thread calls
             * save() every cycle and must never wait on a disk write. The
             * identity is snapshotted with the bytes so the pair committed
             * below is the pair that was current at this instant. */
            std::lock_guard<std::mutex> guard(g_lock);
            if (!g_dirty) continue;
            snapshot    = g_pending;
            snapshot_id = g_program_md5;
            g_dirty     = false;
        }
        if (!snapshot.empty()) commit(snapshot.data(), (uint16_t)snapshot.size(), snapshot_id);
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
            commit(g_pending.data(), (uint16_t)g_pending.size(), g_program_md5);
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

int plc_retain_file_store_load(const char *program_md5, uint16_t md5_len, uint8_t *out,
                               uint16_t cap, uint16_t *out_len)
{
    if (out_len) *out_len = 0;
    if (!g_enabled.load() || !out || cap == 0) return -1;
    if (!program_md5 || md5_len != PROGRAM_ID_LEN) return -1;

    /* Hold the identity for every subsequent save, whatever the outcome below:
     * a store that just discarded a previous program's values still has to
     * label the new program's first commit. */
    {
        std::lock_guard<std::mutex> guard(g_lock);
        g_program_md5.assign(program_md5, md5_len);
    }

    FILE *f = fopen(g_path.c_str(), "rb");
    if (!f) return 0; /* nothing stored — a first boot, or freshly discarded */

    char stored_id[PROGRAM_ID_LEN];
    const size_t got_id = fread(stored_id, 1, PROGRAM_ID_LEN, f);
    if (got_id != PROGRAM_ID_LEN)
    {
        /* Shorter than the header: a torn write, or a file written before the
         * header existed. Either way its owner cannot be established. */
        fclose(f);
        discard_stored();
        log_info("Retain: stored values could not be attributed to a program — storage cleared");
        return 0;
    }

    if (memcmp(stored_id, program_md5, PROGRAM_ID_LEN) != 0)
    {
        fclose(f);
        discard_stored();
        log_info("Retain: stored values belong to a different program — storage cleared, "
                 "retained variables start at their initial values");
        return 0;
    }

    const size_t n = fread(out, 1, cap, f);
    fclose(f);
    if (n == 0) return 0; /* header only: nothing was ever committed for it */

    if (out_len) *out_len = (uint16_t)n;

    /* Prime the in-memory copy so the first flush after start does not rewrite
     * a byte-identical file. */
    std::lock_guard<std::mutex> guard(g_lock);
    g_pending.assign(out, out + n);
    g_dirty = false;
    return 0;
}

int plc_retain_file_store_flush(void)
{
    /* Commit now, WITHOUT touching the flusher thread. Deliberately not
     * plc_retain_file_store_stop(): the PLC can be started again without the
     * daemon restarting, and joining the thread here would leave the next run
     * with nothing committing on a timer. */
    std::lock_guard<std::mutex> guard(g_lock);
    if (!g_dirty || g_pending.empty()) return 0;
    commit(g_pending.data(), (uint16_t)g_pending.size(), g_program_md5);
    g_dirty = false;
    return 0;
}
