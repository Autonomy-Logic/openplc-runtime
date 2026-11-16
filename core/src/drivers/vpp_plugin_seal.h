// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * @file vpp_plugin_seal.h
 * @brief Last-metre integrity check for VPP plugin shared objects.
 *
 * The VPP plugin's .so is linked on the device by scripts/compile.sh, which
 * records the sha256 of every .so it produced into
 * build/vpp/vpp_plugin.seal. This module re-checks that hash immediately
 * before dlopen, so an object swapped into build/vpp/ AFTER the compile does
 * not load: only what THIS runtime's compile step built gets executed.
 * (Package provenance of the upload itself is the editor's concern -- it
 * verifies the installed VPP's Ed25519 signature before building the bundle.)
 *
 * Scope, deliberately narrow: only objects that resolve inside build/vpp/ are
 * sealed, and the HMI plugin inside build/hmi/ against build/hmi/hmi.seal. Built-in plugins listed in plugins.conf are produced by the
 * runtime's own CMake build, are not user-supplied, and are left alone.
 *
 * Not a defence against someone with root and a text editor -- the seal is
 * unkeyed, and a runtime the user recompiles can have this call removed. It
 * raises the cost of the cheap attack (drop a .so into build/vpp/) to that of
 * rebuilding the runtime.
 */

#ifndef VPP_PLUGIN_SEAL_H
#define VPP_PLUGIN_SEAL_H

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Whether @p path must carry a seal (i.e. resolves inside build/vpp/).
 *
 * @param path Plugin path exactly as it appears in the plugin config.
 * @return 1 when the path is a VPP build artefact, 0 otherwise.
 */
int vpp_plugin_seal_required(const char *path);

/**
 * @brief Verify @p path against build/vpp/vpp_plugin.seal.
 *
 * Fails closed: a missing seal file, a missing entry for this object, an
 * unreadable object, or a hash mismatch all return non-zero.
 *
 * @param path Plugin path as it appears in the plugin config.
 * @return 0 when the object matches its sealed hash, non-zero otherwise.
 */
int vpp_plugin_seal_verify(const char *path);

/**
 * @brief sha256 of a file, written as 64 lower-case hex chars plus NUL.
 *
 * @param path    File to hash.
 * @param out_hex Buffer of at least 65 bytes.
 * @return 0 on success, non-zero when the file cannot be read.
 */
int vpp_plugin_seal_sha256_file(const char *path, char *out_hex);

/**
 * @brief Whether @p path must carry an HMI seal (i.e. resolves inside build/hmi/).
 */
int hmi_plugin_seal_required(const char *path);

/**
 * @brief Verify @p path, which must be build/hmi/libhmi_plugin.so, against its line in
 *        build/hmi/hmi.seal. Any other file, sealed client files included, is refused. Fails closed
 *        like vpp_plugin_seal_verify.
 */
int hmi_plugin_seal_verify(const char *path);

/**
 * @brief Verify @p path against the line named @p sealed_name in @p seal_file.
 *
 * Seal lines are "<64 hex>  <name>". Fails closed: a missing seal file, a missing entry, an
 * unreadable object or a hash mismatch all return non-zero and log why.
 *
 * @param what Label used in the log lines (e.g. "VPP plugin").
 */
int plugin_seal_verify_entry(const char *seal_file, const char *path, const char *sealed_name,
                             const char *what);

/**
 * @brief The seal check the loader runs before dlopen: VPP objects against build/vpp's seal,
 *        HMI objects against build/hmi's, anything else needs none.
 *
 * @return 0 when @p path may be loaded, non-zero otherwise.
 */
int plugin_seal_check(const char *path);

#ifdef __cplusplus
}
#endif

#endif /* VPP_PLUGIN_SEAL_H */
