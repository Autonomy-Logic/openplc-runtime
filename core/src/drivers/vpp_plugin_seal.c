#include "vpp_plugin_seal.h"
#include "../plc_app/utils/log.h"

#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#define VPP_BUILD_SUBDIR "build/vpp"
#define VPP_SEAL_FILE VPP_BUILD_SUBDIR "/vpp_plugin.seal"
#define SHA256_HEX_LEN 64
#define SHA256_BLOCK_LEN 64

/* ------------------------------------------------------------------------- */
/* SHA-256 (FIPS 180-4).                                                     */
/*                                                                           */
/* Written out here because the runtime links no crypto library: CMakeLists   */
/* has no OpenSSL, and pulling one in for a 32-byte digest would add a build  */
/* dependency to every target we ship to. This is the standard construction   */
/* with no shortcuts; it hashes a file in 64-byte blocks so a multi-megabyte  */
/* .so does not have to be resident.                                         */
/* ------------------------------------------------------------------------- */

typedef struct
{
    uint32_t state[8];
    uint64_t bit_len;
    uint8_t buffer[SHA256_BLOCK_LEN];
    size_t buffer_len;
} sha256_ctx_t;

static const uint32_t SHA256_K[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u,
    0xab1c5ed5u, 0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu,
    0x9bdc06a7u, 0xc19bf174u, 0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu,
    0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau, 0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u,
    0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu,
    0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u, 0xa2bfe8a1u, 0xa81a664bu,
    0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u, 0x19a4c116u,
    0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u,
    0xc67178f2u};

static uint32_t rotr32(uint32_t value, unsigned int bits)
{
    return (value >> bits) | (value << (32u - bits));
}

static void sha256_init(sha256_ctx_t *ctx)
{
    ctx->state[0] = 0x6a09e667u;
    ctx->state[1] = 0xbb67ae85u;
    ctx->state[2] = 0x3c6ef372u;
    ctx->state[3] = 0xa54ff53au;
    ctx->state[4] = 0x510e527fu;
    ctx->state[5] = 0x9b05688cu;
    ctx->state[6] = 0x1f83d9abu;
    ctx->state[7] = 0x5be0cd19u;
    ctx->bit_len = 0;
    ctx->buffer_len = 0;
}

static void sha256_compress(sha256_ctx_t *ctx, const uint8_t *block)
{
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
    {
        w[i] = ((uint32_t)block[i * 4] << 24) | ((uint32_t)block[i * 4 + 1] << 16) |
               ((uint32_t)block[i * 4 + 2] << 8) | (uint32_t)block[i * 4 + 3];
    }
    for (int i = 16; i < 64; i++)
    {
        uint32_t s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }

    uint32_t a = ctx->state[0];
    uint32_t b = ctx->state[1];
    uint32_t c = ctx->state[2];
    uint32_t d = ctx->state[3];
    uint32_t e = ctx->state[4];
    uint32_t f = ctx->state[5];
    uint32_t g = ctx->state[6];
    uint32_t h = ctx->state[7];

    for (int i = 0; i < 64; i++)
    {
        uint32_t s1 = rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25);
        uint32_t ch = (e & f) ^ ((~e) & g);
        uint32_t temp1 = h + s1 + ch + SHA256_K[i] + w[i];
        uint32_t s0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t temp2 = s0 + maj;

        h = g;
        g = f;
        f = e;
        e = d + temp1;
        d = c;
        c = b;
        b = a;
        a = temp1 + temp2;
    }

    ctx->state[0] += a;
    ctx->state[1] += b;
    ctx->state[2] += c;
    ctx->state[3] += d;
    ctx->state[4] += e;
    ctx->state[5] += f;
    ctx->state[6] += g;
    ctx->state[7] += h;
}

static void sha256_update(sha256_ctx_t *ctx, const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; i++)
    {
        ctx->buffer[ctx->buffer_len++] = data[i];
        if (ctx->buffer_len == SHA256_BLOCK_LEN)
        {
            sha256_compress(ctx, ctx->buffer);
            ctx->bit_len += 512;
            ctx->buffer_len = 0;
        }
    }
}

static void sha256_final_hex(sha256_ctx_t *ctx, char *out_hex)
{
    size_t i = ctx->buffer_len;
    ctx->bit_len += (uint64_t)ctx->buffer_len * 8u;

    ctx->buffer[i++] = 0x80;
    if (i > 56)
    {
        while (i < SHA256_BLOCK_LEN)
        {
            ctx->buffer[i++] = 0x00;
        }
        sha256_compress(ctx, ctx->buffer);
        i = 0;
    }
    while (i < 56)
    {
        ctx->buffer[i++] = 0x00;
    }
    for (int b = 7; b >= 0; b--)
    {
        ctx->buffer[i++] = (uint8_t)((ctx->bit_len >> (b * 8)) & 0xffu);
    }
    sha256_compress(ctx, ctx->buffer);

    static const char hex[] = "0123456789abcdef";
    for (int w = 0; w < 8; w++)
    {
        for (int b = 3; b >= 0; b--)
        {
            uint8_t byte = (uint8_t)((ctx->state[w] >> (b * 8)) & 0xffu);
            *out_hex++ = hex[byte >> 4];
            *out_hex++ = hex[byte & 0x0fu];
        }
    }
    *out_hex = '\0';
}

int vpp_plugin_seal_sha256_file(const char *path, char *out_hex)
{
    if (!path || !out_hex)
    {
        return -1;
    }

    FILE *file = fopen(path, "rb");
    if (!file)
    {
        return -1;
    }

    sha256_ctx_t ctx;
    sha256_init(&ctx);

    uint8_t chunk[4096];
    size_t read_len;
    while ((read_len = fread(chunk, 1, sizeof(chunk), file)) > 0)
    {
        sha256_update(&ctx, chunk, read_len);
    }

    int failed = ferror(file) ? -1 : 0;
    fclose(file);
    if (failed != 0)
    {
        return -1;
    }

    sha256_final_hex(&ctx, out_hex);
    return 0;
}

/* ------------------------------------------------------------------------- */
/* Seal lookup                                                               */
/* ------------------------------------------------------------------------- */

/** Basename of @p path, i.e. everything after the last '/'. Never NULL. */
static const char *path_basename(const char *path)
{
    const char *slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

int vpp_plugin_seal_required(const char *path)
{
    if (!path || path[0] == '\0')
    {
        return 0;
    }

    /* Compare RESOLVED paths, not the literal string: the config may say
     * ./build/vpp/libx.so, build/vpp/libx.so, or an absolute path, and a
     * symlink anywhere in the chain would defeat a textual match. When the
     * file does not exist yet realpath fails -- fall back to a textual test so
     * a missing object is still treated as VPP (and therefore still refused
     * below) instead of being waved through as a built-in. */
    char resolved_path[PATH_MAX];
    char resolved_vpp[PATH_MAX];
    if (realpath(path, resolved_path) && realpath(VPP_BUILD_SUBDIR, resolved_vpp))
    {
        size_t len = strlen(resolved_vpp);
        if (strncmp(resolved_path, resolved_vpp, len) == 0 &&
            (resolved_path[len] == '/' || resolved_path[len] == '\0'))
        {
            return 1;
        }
        return 0;
    }

    return strstr(path, VPP_BUILD_SUBDIR "/") != NULL ? 1 : 0;
}

int vpp_plugin_seal_verify(const char *path)
{
    if (!path || path[0] == '\0')
    {
        return -1;
    }

    char actual_hex[SHA256_HEX_LEN + 1];
    if (vpp_plugin_seal_sha256_file(path, actual_hex) != 0)
    {
        log_error("[PLUGIN] cannot hash VPP plugin '%s' for its integrity seal", path);
        return -1;
    }

    FILE *seal = fopen(VPP_SEAL_FILE, "r");
    if (!seal)
    {
        log_error("[PLUGIN] no VPP plugin seal at %s -- refusing to load '%s'. The plugin was "
                  "not produced by this runtime's compile step; re-upload the program from the editor.",
                  VPP_SEAL_FILE, path);
        return -1;
    }

    const char *wanted_name = path_basename(path);
    char line[512];
    int found = 0;
    int matched = 0;

    /* Seal lines are "<64 hex>  <basename>", written by scripts/compile.sh. */
    while (fgets(line, sizeof(line), seal))
    {
        if (line[0] == '#' || line[0] == '\n' || line[0] == '\r')
        {
            continue;
        }

        char sealed_hex[SHA256_HEX_LEN + 1];
        char sealed_name[256];
        if (sscanf(line, "%64s %255s", sealed_hex, sealed_name) != 2)
        {
            continue;
        }
        if (strcmp(sealed_name, wanted_name) != 0)
        {
            continue;
        }

        found = 1;
        matched = (strncmp(sealed_hex, actual_hex, SHA256_HEX_LEN) == 0);
        break;
    }

    fclose(seal);

    if (!found)
    {
        log_error("[PLUGIN] VPP plugin '%s' has no entry in %s -- refusing to load it. Only "
                  "objects built by this runtime's compile step are sealed.",
                  path, VPP_SEAL_FILE);
        return -1;
    }

    if (!matched)
    {
        log_error("[PLUGIN] VPP plugin '%s' does not match its sealed hash -- refusing to load "
                  "it. The .so changed after it was built.",
                  path);
        return -1;
    }

    return 0;
}
