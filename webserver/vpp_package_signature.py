"""Ed25519 verification of the VPP package signature that rides in an upload.

Why this exists
---------------
A licensed VPP ships the closed enforcement objects (``license_core.o``,
``license_gate.o``) plus a link-only ``Makefile`` INSIDE the user's upload, and
``scripts/compile.sh`` runs that Makefile as root. Nothing in that path proved
where those bytes came from: swapping an object, or adding a three-line
``license_gate.c`` stub that always returns "licensed" and listing it in the
Makefile's ``VENDOR_OBJECTS``, was enough to run a licensed VPP in FULL mode.

The signing machinery already existed and was simply not wired end to end:
``openplc-packages`` signs every ``.vpp`` with Ed25519 and emits a
``signature.json`` holding a sha256 per packaged file, and the editor already
carries the matching public key. What was missing was (a) forwarding that file
to the runtime and (b) checking it here, BEFORE any ``make`` runs.

The contract, byte for byte
---------------------------
Mirrors ``openplc-packages/scripts/lib/package-signing.ts`` and the editor's
``src/backend/shared/utils/vpp/verify-package-signature.ts``. All three MUST
agree on:

1. **Payload** — every key of ``signature.json`` except ``signature``. Extra
   keys are NOT ignored: they are canonicalized in, so a doctored file fails.
2. **Canonicalization** — recursive, key-sorted JSON with no whitespace. This
   is the exact byte string Ed25519 covers (see :func:`canonicalize`).
3. **File hashing** — sha256 over the raw bytes, lower-case hex.

What is verified here, and what is deliberately not
---------------------------------------------------
The signature covers the WHOLE package, but the upload only carries the plugin
directory. So: the Ed25519 signature is checked over the ENTIRE payload (never
a filtered slice -- a slice is not what was signed and would never verify), and
then the file hashes are compared only for the files that actually travelled,
mapped back to their package-relative paths through ``pluginDir``.

``pluginDir`` itself is unsigned routing information. That is safe by
construction: it only selects WHICH signed subtree the uploaded bytes are
compared against, and every subtree of the package was signed by us. It cannot
be pointed at anything that would let unsigned bytes through.

Trust model (write this down, do not rediscover it)
---------------------------------------------------
The verifier lives inside an open-source runtime the user can recompile, so a
determined owner can delete this gate and reinstall. That is accepted: the goal
is to turn "edit a Makefile inside a tarball" (minutes, low skill) into "fork
and reinstall the runtime" (visible, does not survive an upgrade). It is NOT a
cryptographic barrier against the device owner.
"""

from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Optional
import json
import os

# ---------------------------------------------------------------------------
# Trust anchor
# ---------------------------------------------------------------------------
# keyId -> PEM Ed25519 public key. Mirrors, byte for byte, the editor's
# src/backend/shared/utils/vpp/trusted-keys.ts:20. The map (rather than a
# single constant) is what makes rotation possible: ship both keys, retire the
# old one once no supported package still depends on it. The private halves
# live only in the openplc-packages signing pipeline (CI secret).
TRUSTED_PACKAGE_KEYS: Final[dict[str, str]] = {
    "openplc-2026": (
        "-----BEGIN PUBLIC KEY-----\n"
        "MCowBQYDK2VwAyEABdweEuJAfYG923RkmZLYsmonLvCcgVtgpJ7mngbRJQk=\n"
        "-----END PUBLIC KEY-----\n"
    ),
}

# ---------------------------------------------------------------------------
# Upload layout — the editor side of the contract
# ---------------------------------------------------------------------------
# Written by openplc-editor's CompilerModule.handleVendorPluginPackaging.
SIGNATURE_SIDECAR_NAME: Final[str] = "vpp_signature.json"
VPP_PLUGIN_DIR_NAME: Final[str] = "vpp_plugin"
# Seal consumed by scripts/compile.sh so a direct invocation of the script
# cannot build an unverified plugin tree.
VERIFICATION_SEAL_NAME: Final[str] = "vpp_plugin.verified"

# Files the editor deliberately drops when it copies the package's plugin
# directory into vpp_plugin/ (see compiler-module.ts EXCLUDE_FILES). They are
# signed in the package but never travel, so their absence is expected and
# must not be read as a dropped object. Matched by basename at any depth,
# exactly as the editor matches them.
EDITOR_EXCLUDED_BASENAMES: Final[frozenset[str]] = frozenset(
    {"config_template.json", "requirements.txt"}
)

# Files the editor GENERATES into vpp_plugin/ and which therefore cannot be
# covered by the package signature. Top-level only. checksum.sha256 is the
# recompilation cache key (scripts/compile.sh) -- it is not integrity, and it
# is not compiled or linked into anything.
EDITOR_GENERATED_FILES: Final[frozenset[str]] = frozenset({"checksum.sha256"})


# ---------------------------------------------------------------------------
# SINGLE POINT OF POLICY — what happens to an upload without a signature
# ---------------------------------------------------------------------------
def signature_required(has_vpp_plugin: bool) -> bool:
    """Whether this upload must carry a valid package signature.

    THIS FUNCTION IS THE ONLY PLACE THE POLICY LIVES. Changing the answer for
    a whole class of uploads is a one-line edit here; do not spread the
    decision into callers.

    Current policy, and the trade-off it buys:

    * An upload that carries a ``vpp_plugin/`` directory MUST be signed. That
      directory is the attack surface -- it is the only content the runtime
      compiles with a Makefile that came from the upload itself.
    * A plain PLC program (no ``vpp_plugin/``) is untouched. Every existing
      user, and every editor built before the sidecar existed, keeps working.

    What this deliberately does NOT do: require a signature for all uploads.
    That would be strictly stronger (it would also cover the ``core/generated/
    *.cpp`` path, which is arbitrary native code compiled as root) but it
    would refuse every upload from every editor in the field today, and the
    C++ path is the product -- ``c_blocks_code.cpp`` is the user's own C slot,
    which nobody can sign for them. Raising the bar there is a privilege
    question (run the compiler unprivileged), not a signature question.

    The residual gap while this stays as it is: an attacker who wants native
    code as root does not need ``vpp_plugin/`` at all. This gate closes the
    LICENSING bypass, not the RCE.
    """
    return has_vpp_plugin


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of the upload gate. ``ok`` False means: refuse the upload."""

    ok: bool
    #: Operator-facing reason; empty when ``ok``. Written verbatim into the
    #: upload response and the build log, so it must say what to DO.
    error: str = ""
    #: Set when a signature was actually verified (None for a plain program).
    package_id: Optional[str] = None
    #: sha256 over the verified vpp_plugin/ tree; the seal compile.sh checks.
    tree_digest: Optional[str] = None


def canonicalize(value: object) -> str:
    """Recursive, key-sorted JSON with no extra whitespace.

    Must produce the same bytes as ``canonicalize`` in
    openplc-packages/scripts/lib/package-signing.ts:54. Two details are not
    incidental:

    * ``ensure_ascii=False`` -- JS ``JSON.stringify`` emits non-ASCII
      characters raw; Python escapes them by default, which would change the
      signed bytes for any package with a non-ASCII path.
    * Numbers are the one shape where the two languages can still disagree
      (JS renders ``1.0`` as ``1``). Every field of a real payload is a
      string, so this never fires in practice, and if it ever did the
      mismatch fails CLOSED (bad signature) rather than open.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonicalize(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value.keys())
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{canonicalize(value[k])}" for k in keys) + "}"
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


def _verify_ed25519(message: bytes, signature: bytes, public_key_pem: str) -> bool:
    """Ed25519 verify. Any failure -- including a missing dependency -- raises.

    ``cryptography`` is listed in requirements.txt. It is imported lazily and
    inside the request path only, so a runtime that has not re-run install.sh
    still boots and still serves plain programs; only VPP uploads are refused,
    with a message that names the missing package.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "python package 'cryptography' is not installed, so the VPP package "
            "signature cannot be verified; re-run install.sh (or "
            "pip install -r requirements.txt) on this runtime"
        ) from exc

    key = load_pem_public_key(public_key_pem.encode("utf-8"))
    verify = getattr(key, "verify", None)
    if verify is None:
        raise RuntimeError("trusted key is not an Ed25519 public key")
    try:
        verify(signature, message)
    except InvalidSignature:
        return False
    return True


def sha256_file(path: str) -> str:
    """sha256 hex of a file's raw bytes, read in chunks (objects are MBs)."""
    digest = sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_tree_files(root: str) -> list[str]:
    """Every regular file under ``root`` as sorted POSIX-relative paths.

    Symlinks are reported as-is rather than followed: the caller rejects them,
    because a link is not a regular file and hashing its target would let an
    upload pass verification on bytes that are not in the upload.
    """
    out: list[str] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(current, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append(rel)
    return sorted(out)


def tree_digest(root: str) -> str:
    """Digest over a whole directory: sha256 of "<filehash>  <relpath>\\n" lines.

    Same shape scripts/compile.sh recomputes when it checks the verification
    seal, so the two cannot drift: sorted POSIX-relative paths, two spaces,
    trailing newline, then sha256 of that listing.
    """
    outer = sha256()
    for rel in list_tree_files(root):
        outer.update(f"{sha256_file(os.path.join(root, rel))}  {rel}\n".encode("utf-8"))
    return outer.hexdigest()


def _parse_sidecar(path: str) -> tuple[dict, str, str]:
    """Read the editor's sidecar. Returns (signature_file, plugin_dir, error)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        return {}, "", f"{SIGNATURE_SIDECAR_NAME} is missing or unreadable ({exc})"

    if not isinstance(raw, dict):
        return {}, "", f"{SIGNATURE_SIDECAR_NAME} is malformed (not an object)"

    package = raw.get("package")
    plugin_dir = raw.get("pluginDir")
    if not isinstance(package, dict) or not isinstance(plugin_dir, str) or not plugin_dir:
        return {}, "", f"{SIGNATURE_SIDECAR_NAME} is malformed (missing 'package' or 'pluginDir')"

    # pluginDir only selects which signed subtree to compare against, but it is
    # still string-concatenated into signed paths -- keep it a plain relative
    # POSIX path so it cannot be used to walk anywhere unexpected.
    normalized = plugin_dir.strip("/")
    if not normalized or normalized.startswith("/") or ".." in normalized.split("/") or "\\" in normalized:
        return {}, "", f"{SIGNATURE_SIDECAR_NAME} has an invalid pluginDir: {plugin_dir!r}"

    return package, normalized, ""


def _verify_signature_file(signature_file: dict) -> tuple[dict, str]:
    """Check the detached Ed25519 signature. Returns (files_map, error)."""
    signature_b64 = signature_file.get("signature")
    if not isinstance(signature_b64, str) or not signature_b64:
        return {}, "package signature.json is malformed (no signature)"

    # The payload is EVERYTHING except `signature` -- including keys we do not
    # know about. That is what the signing side hashed, and it is why an
    # attacker cannot smuggle extra fields past the check.
    payload = {k: v for k, v in signature_file.items() if k != "signature"}

    for field in ("formatVersion", "alg", "keyId", "packageId", "version", "signedAt"):
        if not isinstance(payload.get(field), str):
            return {}, f"package signature.json is malformed (bad '{field}')"
    files = payload.get("files")
    if not isinstance(files, dict) or any(not isinstance(v, str) for v in files.values()):
        return {}, "package signature.json is malformed (bad 'files' map)"

    if payload["alg"] != "ed25519":
        return {}, f"unsupported package signature algorithm: {payload['alg']}"

    public_key_pem = TRUSTED_PACKAGE_KEYS.get(payload["keyId"])
    if not public_key_pem:
        return {}, f"package is signed by an untrusted key: {payload['keyId']}"

    from base64 import b64decode

    try:
        signature = b64decode(signature_b64, validate=True)
    except ValueError:
        return {}, "package signature is not valid base64"

    message = canonicalize(payload).encode("utf-8")
    try:
        valid = _verify_ed25519(message, signature, public_key_pem)
    except RuntimeError as exc:
        return {}, str(exc)
    if not valid:
        return {}, "package signature does not verify (tampered package or wrong key)"

    return files, ""


def verify_uploaded_vpp_plugin(generated_dir: str) -> VerificationResult:
    """The upload gate. Call BEFORE anything is copied out of ``generated_dir``.

    Refusing here (rather than in compile.sh) is deliberate: nothing from the
    upload -- not vpp_plugins.conf, not the per-plugin JSON, not the license
    blob -- reaches the runtime root until this returns ok.
    """
    plugin_dir = os.path.join(generated_dir, VPP_PLUGIN_DIR_NAME)
    has_vpp_plugin = os.path.isdir(plugin_dir)

    if not signature_required(has_vpp_plugin):
        return VerificationResult(ok=True)

    sidecar_path = os.path.join(generated_dir, SIGNATURE_SIDECAR_NAME)
    signature_file, signed_prefix, error = _parse_sidecar(sidecar_path)
    if error:
        return VerificationResult(
            ok=False,
            error=(
                f"this upload contains a VPP plugin but no usable package signature: {error}. "
                "Re-install the VPP package from a signed .vpp and upload again from an "
                "editor that forwards the package signature."
            ),
        )

    signed_files, error = _verify_signature_file(signature_file)
    if error:
        return VerificationResult(ok=False, error=f"VPP package signature rejected: {error}")

    prefix = signed_prefix + "/"

    # 1) Everything that travelled must be signed, with the signed bytes.
    #    This is the half that stops the audit's attack: an injected
    #    license_gate.c has no signed hash, and an edited Makefile has the
    #    wrong one.
    try:
        present = list_tree_files(plugin_dir)
    except OSError as exc:
        return VerificationResult(ok=False, error=f"VPP plugin directory is unreadable: {exc}")

    for rel in present:
        full = os.path.join(plugin_dir, rel)
        if os.path.islink(full) or not os.path.isfile(full):
            return VerificationResult(
                ok=False,
                error=f"VPP plugin directory contains a non-regular file: vpp_plugin/{rel}",
            )
        if rel in EDITOR_GENERATED_FILES:
            continue
        expected = signed_files.get(prefix + rel)
        if expected is None:
            return VerificationResult(
                ok=False,
                error=(
                    f"VPP plugin file is not covered by the package signature: vpp_plugin/{rel}. "
                    "The uploaded plugin does not match the signed .vpp package."
                ),
            )
        if sha256_file(full) != expected:
            return VerificationResult(
                ok=False,
                error=(
                    f"VPP plugin file does not match the package signature: vpp_plugin/{rel}. "
                    "The uploaded plugin was modified after the package was signed."
                ),
            )

    # 2) Nothing signed may be missing either. Without this, dropping an
    #    enforcement object would still be refused only indirectly (the signed
    #    Makefile would fail to link it), which depends on that Makefile
    #    naming the object explicitly. Not every package's will.
    present_set = set(present)
    for signed_path in signed_files:
        if not signed_path.startswith(prefix):
            continue
        rel = signed_path[len(prefix) :]
        if os.path.basename(rel) in EDITOR_EXCLUDED_BASENAMES:
            continue  # the editor drops these on purpose; see the constant
        if rel not in present_set:
            return VerificationResult(
                ok=False,
                error=(
                    f"VPP plugin file is missing from the upload: vpp_plugin/{rel}. "
                    "The uploaded plugin is not the signed .vpp package."
                ),
            )

    return VerificationResult(
        ok=True,
        package_id=str(signature_file.get("packageId", "")),
        tree_digest=tree_digest(plugin_dir),
    )


def write_verification_seal(generated_dir: str, result: VerificationResult) -> None:
    """Record that the gate ran and over which bytes.

    scripts/compile.sh refuses to run the uploaded Makefile without this seal
    AND without recomputing the same tree digest, so invoking compile.sh
    directly cannot skip the gate, and a file swapped between the gate and
    ``make`` is caught. The seal is an interlock, not a trust anchor: it is
    unkeyed, so anyone who can already write into core/generated/ can forge
    it -- but anyone who can do that has a shell, and the upload path (the
    thing being defended) cannot.
    """
    if result.tree_digest is None:
        return
    seal_path = os.path.join(generated_dir, VERIFICATION_SEAL_NAME)
    lines = [
        "# OpenPLC VPP plugin verification seal.",
        "# Written by the webserver upload gate (webserver/vpp_package_signature.py)",
        "# after the package Ed25519 signature verified against the bytes below.",
        "# Consumed by scripts/compile.sh. Not a signature -- see the docstring.",
        f"packageId {result.package_id or ''}",
        f"treeDigest {result.tree_digest}",
        "",
    ]
    with open(seal_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
