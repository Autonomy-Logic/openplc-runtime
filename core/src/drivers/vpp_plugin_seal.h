/**
 * @file vpp_plugin_seal.h
 * @brief Last-metre integrity check for VPP plugin shared objects.
 *
 * The trust anchor for a VPP plugin is the package's Ed25519 signature, which
 * the webserver verifies on the upload path before anything is compiled
 * (webserver/vpp_package_signature.py). That covers the INPUTS: the prebuilt
 * vendor objects and the link-only Makefile.
 *
 * It cannot cover the OUTPUT. The .so is linked on the device, after the
 * signature was made, so its bytes are unknown to whoever signed. Instead
 * scripts/compile.sh records the sha256 of every .so it produced from a
 * verified tree into build/vpp/vpp_plugin.seal, and this module re-checks that
 * hash immediately before dlopen. Without it, an object swapped into
 * build/vpp/ AFTER the compile would load unchecked, and verifying the upload
 * would have proved nothing about the code actually executed.
 *
 * Scope, deliberately narrow: only objects that resolve inside build/vpp/ are
 * sealed. Built-in plugins listed in plugins.conf are produced by the
 * runtime's own CMake build, are not user-supplied, and are left alone.
 *
 * Not a defence against someone with root and a text editor -- the seal is
 * unkeyed, and a runtime the user recompiles can have this call removed. It
 * raises the cost of the cheap attack (drop a .so into build/vpp/) to that of
 * rebuilding the runtime.
 */

#ifndef VPP_PLUGIN_SEAL_H
#define VPP_PLUGIN_SEAL_H

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

#endif /* VPP_PLUGIN_SEAL_H */
