/*
 * Host test for the built-in retain file store's identity handling.
 *
 * WHY THIS EXISTS SEPARATELY FROM THE PYTEST SUITE
 * ------------------------------------------------
 * `tests/pytest/plugins/test_apply_retain_conf.py` covers the webserver half:
 * which retain.conf gets installed, and when the device's copy is removed. It
 * says nothing about the half that decides whether stored BYTES still belong to
 * the running program, which is the behaviour the whole design rests on:
 *
 *   - the `[32-byte program md5][payload]` on-disk layout. No length field: a
 *     file has a size, so the payload length is recovered by reading to EOF.
 *     (Baremetal's flash driver DOES carry an explicit length, because its
 *     region is fixed-size and trailing erased bytes read as 0xFF — the two
 *     formats are deliberately not the same, and this test pins this one.);
 *   - discarding the payload when the identity does not match;
 *   - treating a file too short to carry the header as unattributable;
 *   - holding the identity from load() so the next save() can label its bytes.
 *
 * Until this file existed, that path's only evidence was a by-hand run on an
 * SLM-RP4 recorded in a PR body. The case it proves — a program's values are
 * refused for a DIFFERENT program even when the retain layout is identical — is
 * exactly the one a layout hash cannot catch, so it is worth being able to
 * re-run without hardware.
 *
 * WHY NOT CEEDLING, AND WHY NOT THE LIFECYCLE HARNESS
 * --------------------------------------------------
 * Ceedling is configured for C (`:test_file_prefix: test_`, `.c` sources) and
 * this store is C++ with `std::thread`/`std::mutex`, so it is not in that
 * runner's reach. `tests/lifecycle/` could reach it, but it boots a real
 * `plc_main` against a compiled PLC program and needs Linux, Docker and an
 * editor payload — far more machinery than file-header logic warrants, and it
 * would not run on a developer's machine.
 *
 * This is a plain executable instead: no framework, no fixtures, one command.
 *
 *   c++ -std=c++17 -I core/src/plc_app -I core/src \
 *       tests/host/test_plc_retain_file_store.cpp \
 *       core/src/plc_app/plc_retain_file_store.cpp -o /tmp/t && /tmp/t
 *
 * See tests/host/run.sh, which does exactly that.
 */

#include <cassert>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include <sys/stat.h>
#include <unistd.h>

#include "plc_retain.h"
#include "plc_retain_file_store.h"

// ---------------------------------------------------------------------------
// The store logs through the runtime's logger, which is not worth linking here.
// Captured rather than discarded: two cases below assert that the operator is
// TOLD storage was cleared, because a silent discard of retained values is the
// failure mode this design is most likely to be blamed for later.
// ---------------------------------------------------------------------------
static std::string g_log;

extern "C" void log_info(const char *fmt, ...)
{
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    g_log += buf;
    g_log += '\n';
}

extern "C" void log_warn(const char *fmt, ...)
{
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    g_log += buf;
    g_log += '\n';
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------
static int g_failures = 0;
static const char *g_case = "";

#define CHECK(cond, what)                                                                      \
    do {                                                                                       \
        if (!(cond)) {                                                                         \
            fprintf(stderr, "  FAIL  %s: %s\n         (%s:%d)\n", g_case, (what), __FILE__,     \
                    __LINE__);                                                                 \
            g_failures++;                                                                       \
        }                                                                                      \
    } while (0)

static std::string g_dir;
static std::string g_store_path;
static std::string g_conf_path;

/* Two identities that differ, both the right length. Deliberately NOT
 * NUL-terminated in the calls below — the contract says 32 characters and the
 * length travels separately, and a driver reaching for strlen would pass a test
 * that used terminated strings and fail in production. */
static const char MD5_A[PLC_RETAIN_PROGRAM_ID_LEN] = {'a', 'a', 'a', 'a', 'a', 'a', 'a', 'a',
                                                      'a', 'a', 'a', 'a', 'a', 'a', 'a', 'a',
                                                      'a', 'a', 'a', 'a', 'a', 'a', 'a', 'a',
                                                      'a', 'a', 'a', 'a', 'a', 'a', 'a', '1'};
static const char MD5_B[PLC_RETAIN_PROGRAM_ID_LEN] = {'a', 'a', 'a', 'a', 'a', 'a', 'a', 'a',
                                                      'a', 'a', 'a', 'a', 'a', 'a', 'a', 'a',
                                                      'a', 'a', 'a', 'a', 'a', 'a', 'a', 'a',
                                                      'a', 'a', 'a', 'a', 'a', 'a', 'a', '2'};

static void write_conf(int flush_seconds = 1)
{
    FILE *f = fopen(g_conf_path.c_str(), "w");
    assert(f);
    fprintf(f, "enabled=1\npath=%s\nflush_seconds=%d\n", g_store_path.c_str(), flush_seconds);
    fclose(f);
}

/* Start, hand over a blob, commit it, stop. `flush` rather than waiting on the
 * flusher thread: the point here is the bytes and the header, not the timer. */
static void store_blob_as(const char *md5, const uint8_t *blob, uint16_t len)
{
    write_conf();
    bool ok = plc_retain_file_store_start(g_conf_path.c_str());
    CHECK(ok, "the store should enable from a valid retain.conf");

    uint8_t out[512];
    uint16_t got = 0;
    plc_retain_file_store_load(md5, PLC_RETAIN_PROGRAM_ID_LEN, out, sizeof(out), &got);

    plc_retain_file_store_save(blob, len);
    plc_retain_file_store_flush();
    plc_retain_file_store_stop();
}

static bool store_file_exists()
{
    FILE *f = fopen(g_store_path.c_str(), "rb");
    if (!f) return false;
    fclose(f);
    return true;
}

static long store_file_size()
{
    FILE *f = fopen(g_store_path.c_str(), "rb");
    if (!f) return -1;
    fseek(f, 0, SEEK_END);
    const long n = ftell(f);
    fclose(f);
    return n;
}

static void reset()
{
    remove(g_store_path.c_str());
    remove((g_store_path + ".tmp").c_str());
    remove(g_conf_path.c_str());
    g_log.clear();
}

// ---------------------------------------------------------------------------
// Cases
// ---------------------------------------------------------------------------

/* The header is what makes the identity and the payload one unit: a file whose
 * header says program A is guaranteed to hold program A's values, because the
 * same write-and-rename published both. */
static void case_header_layout()
{
    g_case = "the stored file is [32-byte identity][payload]";
    reset();

    const uint8_t blob[] = {1, 2, 3, 4, 5, 6, 7, 8};
    store_blob_as(MD5_A, blob, sizeof(blob));

    CHECK(store_file_exists(), "a committed blob should leave a file");
    CHECK(store_file_size() == (long)(PLC_RETAIN_PROGRAM_ID_LEN + sizeof(blob)),
          "size should be exactly identity + payload — no length field, no padding");

    FILE *f = fopen(g_store_path.c_str(), "rb");
    assert(f);
    char id[PLC_RETAIN_PROGRAM_ID_LEN];
    CHECK(fread(id, 1, sizeof(id), f) == sizeof(id), "the header should be readable");
    CHECK(memcmp(id, MD5_A, sizeof(id)) == 0, "the header should carry the storing program's id");
    fclose(f);
}

static void case_same_program_restores()
{
    g_case = "the same program gets its values back";
    reset();

    const uint8_t blob[] = {9, 8, 7, 6};
    store_blob_as(MD5_A, blob, sizeof(blob));

    write_conf();
    plc_retain_file_store_start(g_conf_path.c_str());
    uint8_t out[512];
    uint16_t got = 0;
    const int rc = plc_retain_file_store_load(MD5_A, PLC_RETAIN_PROGRAM_ID_LEN, out, sizeof(out),
                                              &got);
    plc_retain_file_store_stop();

    CHECK(rc == 0, "a matching identity should not be an error");
    CHECK(got == sizeof(blob), "the whole payload should come back");
    CHECK(memcmp(out, blob, sizeof(blob)) == 0, "the payload should come back unchanged");
}

/* THE CASE A LAYOUT HASH CANNOT CATCH.
 *
 * Both programs here retain the same shape — same length, same bytes would pack
 * identically — and differ only in identity. Without this check the second
 * program silently inherits the first one's state. */
static void case_different_program_is_discarded()
{
    g_case = "a different program's values are discarded, not inherited";
    reset();

    const uint8_t blob[] = {0xDE, 0xAD, 0xBE, 0xEF};
    store_blob_as(MD5_A, blob, sizeof(blob));
    CHECK(store_file_exists(), "precondition: program A's values are stored");

    g_log.clear();
    write_conf();
    plc_retain_file_store_start(g_conf_path.c_str());
    uint8_t out[512];
    uint16_t got = 0;
    const int rc = plc_retain_file_store_load(MD5_B, PLC_RETAIN_PROGRAM_ID_LEN, out, sizeof(out),
                                              &got);

    CHECK(rc == 0, "a stale store is not an error — it is an empty one");
    CHECK(got == 0, "NOTHING may be handed back to a different program");
    CHECK(!store_file_exists(), "the stale file should be removed, not left to be re-read");
    CHECK(g_log.find("different program") != std::string::npos,
          "the operator must be told storage was cleared, and why");

    /* And the new program's first commit must be labelled with ITS identity,
     * from the id the load above held — otherwise the next start discards it
     * too and retention never works for this program. */
    const uint8_t fresh[] = {0x11, 0x22};
    plc_retain_file_store_save(fresh, sizeof(fresh));
    plc_retain_file_store_flush();
    plc_retain_file_store_stop();

    FILE *f = fopen(g_store_path.c_str(), "rb");
    CHECK(f != nullptr, "the new program should be able to store");
    if (f) {
        char id[PLC_RETAIN_PROGRAM_ID_LEN];
        CHECK(fread(id, 1, sizeof(id), f) == sizeof(id), "header readable");
        CHECK(memcmp(id, MD5_B, sizeof(id)) == 0,
              "the commit after a discard must carry the NEW program's id");
        fclose(f);
    }
}

/* A file shorter than the header could be a torn write or a pre-header file from
 * an older runtime. Either way its owner cannot be established, and bytes whose
 * owner is unknown must not be handed to anyone. */
static void case_short_file_is_unattributable()
{
    g_case = "a file too short to carry a header is discarded";
    reset();
    write_conf();

    FILE *f = fopen(g_store_path.c_str(), "wb");
    assert(f);
    const char partial[] = "aaaaaaaa";  // shorter than the identity
    fwrite(partial, 1, sizeof(partial) - 1, f);
    fclose(f);

    plc_retain_file_store_start(g_conf_path.c_str());
    uint8_t out[512];
    uint16_t got = 0;
    const int rc = plc_retain_file_store_load(MD5_A, PLC_RETAIN_PROGRAM_ID_LEN, out, sizeof(out),
                                              &got);
    plc_retain_file_store_stop();

    CHECK(rc == 0, "an unattributable file is an empty store, not an error");
    CHECK(got == 0, "nothing may be handed back from a file with no usable header");
    CHECK(!store_file_exists(), "the unusable file should be removed");
    CHECK(g_log.find("could not be attributed") != std::string::npos,
          "the operator must be told storage was cleared");
}

static void case_empty_store_is_not_an_error()
{
    g_case = "a first boot reports empty rather than failing";
    reset();
    write_conf();

    plc_retain_file_store_start(g_conf_path.c_str());
    uint8_t out[512];
    uint16_t got = 0;
    const int rc = plc_retain_file_store_load(MD5_A, PLC_RETAIN_PROGRAM_ID_LEN, out, sizeof(out),
                                              &got);
    plc_retain_file_store_stop();

    CHECK(rc == 0, "nothing stored yet is the normal first-boot state");
    CHECK(got == 0, "an empty store hands back nothing");
}

/* The identity length is part of the contract, and a caller that gets it wrong
 * is a caller whose bytes cannot be attributed. Refuse rather than store
 * something mislabelled. */
static void case_wrong_identity_length_is_refused()
{
    g_case = "an identity of the wrong length is refused";
    reset();
    write_conf();

    plc_retain_file_store_start(g_conf_path.c_str());
    uint8_t out[512];
    uint16_t got = 0;
    const int rc = plc_retain_file_store_load(MD5_A, PLC_RETAIN_PROGRAM_ID_LEN - 1, out,
                                              sizeof(out), &got);
    plc_retain_file_store_stop();

    CHECK(rc != 0, "a short identity should be refused, not guessed at");
    CHECK(got == 0, "nothing may be handed back");
}

/* Dedupe lives in the driver because only the driver knows what a write costs.
 * Asserting it here keeps that from being quietly removed: without it, a program
 * whose retained values are steady rewrites the file every flush interval for
 * the life of the machine. */
static void case_unchanged_blob_is_not_rewritten()
{
    g_case = "an unchanged blob is not re-committed";
    reset();

    const uint8_t blob[] = {4, 4, 4, 4};
    store_blob_as(MD5_A, blob, sizeof(blob));

    write_conf();
    plc_retain_file_store_start(g_conf_path.c_str());
    uint8_t out[512];
    uint16_t got = 0;
    plc_retain_file_store_load(MD5_A, PLC_RETAIN_PROGRAM_ID_LEN, out, sizeof(out), &got);

    struct stat before {};
    stat(g_store_path.c_str(), &before);

    /* Same bytes the load just primed the buffer with. */
    plc_retain_file_store_save(blob, sizeof(blob));
    const int rc = plc_retain_file_store_flush();
    plc_retain_file_store_stop();

    struct stat after {};
    stat(g_store_path.c_str(), &after);

    CHECK(rc == 0, "a flush with nothing dirty is success, not failure");
    CHECK(before.st_ino == after.st_ino,
          "an unchanged blob should not republish the file (write-and-rename changes the inode)");
}

int main()
{
    char tmpl[] = "/tmp/retain-store-test-XXXXXX";
    const char *dir = mkdtemp(tmpl);
    if (!dir) {
        fprintf(stderr, "could not create a temp directory\n");
        return 2;
    }
    g_dir = dir;
    g_store_path = g_dir + "/retain.bin";
    g_conf_path = g_dir + "/retain.conf";

    printf("plc_retain_file_store — identity handling\n");
    case_header_layout();
    case_same_program_restores();
    case_different_program_is_discarded();
    case_short_file_is_unattributable();
    case_empty_store_is_not_an_error();
    case_wrong_identity_length_is_refused();
    case_unchanged_blob_is_not_rewritten();

    reset();
    rmdir(g_dir.c_str());

    if (g_failures == 0) {
        printf("all cases passed\n");
        return 0;
    }
    fprintf(stderr, "%d check(s) failed\n", g_failures);
    return 1;
}
