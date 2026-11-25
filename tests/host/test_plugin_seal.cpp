// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

// Host test: the seal checks the plugin loader runs before dlopen, for the HMI plugin
// (build/hmi/hmi.seal, names relative to build/hmi) and for VPP plugins (build/vpp). Links the
// real vpp_plugin_seal.c in a scratch runtime root; logging is faked.

#include "drivers/vpp_plugin_seal.h"

#include <sys/stat.h>
#include <unistd.h>

#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

static int g_failures = 0;
static std::vector<std::string> g_errors;

#define CHECK(cond, what)                                                                          \
    do                                                                                             \
    {                                                                                              \
        if (!(cond))                                                                               \
        {                                                                                          \
            std::printf("  FAIL  %s (line %d)\n", what, __LINE__);                                \
            g_failures++;                                                                          \
        }                                                                                          \
    } while (0)

extern "C" void log_error(const char *fmt, ...)
{
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    std::vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    g_errors.push_back(buf);
}
extern "C" void log_info(const char *, ...) {}
extern "C" void log_warn(const char *, ...) {}
extern "C" void log_debug(const char *, ...) {}

static void write_file(const std::string &path, const std::string &text)
{
    FILE *f = std::fopen(path.c_str(), "wb");
    if (!f)
        return;
    std::fwrite(text.data(), 1, text.size(), f);
    std::fclose(f);
}

static std::string sha(const std::string &path)
{
    char hex[65] = {0};
    if (vpp_plugin_seal_sha256_file(path.c_str(), hex) != 0)
        return "";
    return hex;
}

static void seal(const std::string &file, const std::vector<std::string> &lines)
{
    std::string text;
    for (const std::string &l : lines)
        text += l + "\n";
    write_file(file, text);
}

static void test_sha256()
{
    std::printf("sha256\n");
    write_file("abc.txt", "abc");
    CHECK(sha("abc.txt") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
          "FIPS 180-4 test vector");
}

static void test_hmi_seal()
{
    std::printf("HMI seal\n");
    mkdir("build", 0755);
    mkdir("build/hmi", 0755);
    mkdir("build/hmi/client", 0755);
    write_file("build/hmi/libhmi_plugin.so", "plugin bytes");
    write_file("build/hmi/client/index.html", "<html>");
    const std::string so = sha("build/hmi/libhmi_plugin.so");

    CHECK(hmi_plugin_seal_required("build/hmi/libhmi_plugin.so"), "build/hmi needs a seal");
    CHECK(hmi_plugin_seal_required("./build/hmi/libhmi_plugin.so"), "also as ./build/hmi");
    CHECK(!hmi_plugin_seal_required("build/plugins/libs7comm.so"), "built-in plugins do not");

    g_errors.clear();
    CHECK(plugin_seal_check("build/hmi/libhmi_plugin.so") != 0, "no seal file refuses");

    seal("build/hmi/hmi.seal", {sha("build/hmi/client/index.html") + "  client/index.html",
                                so + "  libhmi_plugin.so"});
    CHECK(plugin_seal_check("build/hmi/libhmi_plugin.so") == 0, "matching line loads");
    CHECK(hmi_plugin_seal_verify("build/hmi/client/index.html") != 0,
          "a sealed client file is never loaded as the plugin");
    CHECK(plugin_seal_check("build/hmi/client/index.html") != 0,
          "... through plugin_seal_check either");

    write_file("build/hmi/libhmi_plugin.so", "swapped bytes");
    g_errors.clear();
    CHECK(plugin_seal_check("build/hmi/libhmi_plugin.so") != 0, "a swapped plugin is refused");
    CHECK(!g_errors.empty() && g_errors.back().find("does not match") != std::string::npos,
          "the mismatch is logged");

    seal("build/hmi/hmi.seal", {so + "  other.so"});
    write_file("build/hmi/libhmi_plugin.so", "plugin bytes");
    CHECK(plugin_seal_check("build/hmi/libhmi_plugin.so") != 0, "no line for the plugin refuses");

    seal("build/hmi/hmi.seal", {so.substr(0, 63) + "  libhmi_plugin.so"});
    CHECK(plugin_seal_check("build/hmi/libhmi_plugin.so") != 0, "a short hash refuses");

    /* A symlink out of build/hmi still needs the seal, and its target does not match it. */
    seal("build/hmi/hmi.seal", {so + "  libhmi_plugin.so"});
    write_file("evil.so", "other bytes");
    unlink("build/hmi/libhmi_plugin.so");
    char cwd[4096];
    CHECK(getcwd(cwd, sizeof cwd) != nullptr, "cwd");
    const std::string target = std::string(cwd) + "/evil.so";
    CHECK(symlink(target.c_str(), "build/hmi/libhmi_plugin.so") == 0, "symlink made");
    CHECK(hmi_plugin_seal_required("build/hmi/libhmi_plugin.so"),
          "a symlink pointing outside still needs the seal");
    CHECK(plugin_seal_check("build/hmi/libhmi_plugin.so") != 0, "and is refused");

    unlink("build/hmi/libhmi_plugin.so");
    CHECK(plugin_seal_check("build/hmi/libhmi_plugin.so") != 0, "a missing plugin is refused");
}

static void test_vpp_seal()
{
    std::printf("VPP seal\n");
    mkdir("build/vpp", 0755);
    write_file("build/vpp/libboard_plugin.so", "board");
    seal("build/vpp/vpp_plugin.seal", {sha("build/vpp/libboard_plugin.so") + "  libboard_plugin.so"});
    CHECK(plugin_seal_check("./build/vpp/libboard_plugin.so") == 0, "a sealed VPP plugin loads");
    write_file("build/vpp/libboard_plugin.so", "tampered");
    CHECK(plugin_seal_check("./build/vpp/libboard_plugin.so") != 0, "a tampered one does not");
    CHECK(plugin_seal_check("build/plugins/libs7comm.so") == 0, "unsealed paths pass through");
}

int main()
{
    char tmpl[] = "/tmp/plugin_seal_XXXXXX";
    char *dir   = mkdtemp(tmpl);
    if (!dir || chdir(dir) != 0)
    {
        std::printf("  FAIL  scratch directory\n");
        return 1;
    }
    test_sha256();
    test_hmi_seal();
    test_vpp_seal();
    const std::string cleanup = std::string("rm -rf '") + dir + "'";
    if (std::system(cleanup.c_str()) != 0)
        std::printf("  note  could not remove %s\n", dir);

    if (g_failures == 0)
        std::printf("all cases passed\n");
    else
        std::printf("%d check(s) failed\n", g_failures);
    return g_failures == 0 ? 0 : 1;
}
