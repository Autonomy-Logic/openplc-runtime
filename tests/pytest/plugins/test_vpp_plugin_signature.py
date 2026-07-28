"""Tests for the VPP package-signature gate on the upload path.

Two kinds of test, on purpose:

1. **A known-answer vector taken from a REAL signed package.** ``REAL_SIGNATURE``
   below is the verbatim ``signature.json`` of
   ``com.openplc.raspberry-pi-licensed-1.0.0.vpp``, produced by the
   openplc-packages signing pipeline, and the key it names is the one shipped in
   webserver/vpp_package_signature.py. It exists because the single real risk in
   this design is that the runtime's canonicalization does not reproduce the
   signer's byte for byte -- and a payload I sign myself with my own
   canonicalization would agree with my own mistake and stay green. This vector
   cannot: the bytes were signed by code in another repo, in another language.

2. **Plumbing tests, signed with a throwaway key.** Everything downstream of the
   signature check -- which files must be present, which must not, how the
   uploaded ``vpp_plugin/`` maps onto the signed package paths -- needs payloads
   this test controls, so it generates a key and trusts it for the duration.
   These are safe to self-sign precisely because test 1 pins the format.
"""
import base64
import hashlib
import json
import os
import shutil
import tempfile

import pytest

_sig = pytest.importorskip(
    "webserver.vpp_package_signature",
    reason="runtime webserver package not importable (no venv)",
)

# Skipped rather than failed where the dependency is absent: the module is
# designed to refuse VPP uploads with an actionable message in that state, and
# that behaviour is asserted separately below without needing the library.
_crypto = pytest.importorskip("cryptography", reason="cryptography not installed")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

# ---------------------------------------------------------------------------
# 1. Known-answer vector from a real .vpp
# ---------------------------------------------------------------------------
# Copied byte for byte out of dist/com.openplc.raspberry-pi-licensed-1.0.0.vpp.
# Do not "tidy" it: the point is that these exact values, canonicalized by this
# repo's implementation, are what the openplc-packages private key signed.
REAL_SIGNATURE = json.loads(
    """
{
  "formatVersion": "1.0",
  "alg": "ed25519",
  "keyId": "openplc-2026",
  "packageId": "com.openplc.raspberry-pi-licensed",
  "version": "1.0.0",
  "signedAt": "2026-07-22T10:59:20.069Z",
  "files": {
    "assets/boards/raspberry-pi.png": "e94cd058c3ae18c92931d024e558702805d6435044201befb21bda08bad00464",
    "assets/logo.png": "e94cd058c3ae18c92931d024e558702805d6435044201befb21bda08bad00464",
    "hal/runtime-v4/plugin/config_template.json": "574e353a5b75185f0777bbb5f722fc6c6cc7058fca0b08fa072fe06a3dda6112",
    "hal/runtime-v4/plugin/license_core.o": "edbf26abe1e83b445dda08687398cf2bdcd7bf43318cb57057a905a70aac30c8",
    "hal/runtime-v4/plugin/license_gate.o": "6054c4d4116db7e73c391e82cc02baabd1eb054a8a806e74d23ade4023cbb759",
    "hal/runtime-v4/plugin/Makefile": "057d85d4e27743b76767dae2071b920357da3f6a9848fd4e9d899563925f49c3",
    "hal/runtime-v4/plugin/rpi_config.o": "6de869b44f25ede6bb9dfb446a788394a65f2c231d141c051bad8d0e4ab66538",
    "hal/runtime-v4/plugin/rpi_gpio.o": "aff4d91f9fd1507d343d71edffd38f08ba3c667c67fa5f89b2df7dae1326ab18",
    "hal/runtime-v4/plugin/rpi_plugin.o": "a978cd597f7c54ae3c2dbbc4b2349ed0abfb62f811bb9cea6d04b5ac4f4c2d16",
    "hal/runtime-v4/plugin/sha256.o": "f39be9f90d088106e848112c5706bbcd0f30ede54c0b35439c057e6610d6b786",
    "hal/runtime-v4/plugin/uECC.o": "db574e6d107d4acccb22ed124fd9d05f8ba9ec752cee29c071782643c0c30ec1",
    "manifest.json": "8db0dad34bdd356a2eaac67221a93e2eec2c2ba99559b3f44c37b4ba5bdbb947"
  },
  "signature": "/GsBcebrbwUpRdPHzkU0lhYyiHYsOtccqcqoqju+27jJN+VZ+0T1QzeArJOOuTb+oBUjxa26FPEpLei+qm7WAQ=="
}
"""
)


def test_real_package_signature_verifies():
    """The shipped trusted key verifies a signature made by the real pipeline.

    If canonicalize() ever drifts from openplc-packages/scripts/lib/
    package-signing.ts:54 -- key ordering, separators, escaping, anything --
    this fails, and it is the only test that can catch that.
    """
    files, error = _sig._verify_signature_file(REAL_SIGNATURE)
    assert error == "", error
    assert files["hal/runtime-v4/plugin/license_gate.o"] == (
        "6054c4d4116db7e73c391e82cc02baabd1eb054a8a806e74d23ade4023cbb759"
    )


def test_real_package_signature_rejects_a_single_flipped_hash():
    """Same vector with one hash changed: the signature must no longer verify.

    Guards against the failure mode where the signature step is accidentally
    turned into a no-op and everything passes.
    """
    doctored = json.loads(json.dumps(REAL_SIGNATURE))
    doctored["files"]["hal/runtime-v4/plugin/license_gate.o"] = "00" * 32
    _files, error = _sig._verify_signature_file(doctored)
    assert "does not verify" in error


def test_real_package_signature_rejects_an_injected_payload_field():
    """An extra key is canonicalized INTO the payload, so it breaks the
    signature -- it is not silently ignored."""
    doctored = json.loads(json.dumps(REAL_SIGNATURE))
    doctored["notes"] = "harmless"
    _files, error = _sig._verify_signature_file(doctored)
    assert "does not verify" in error


def test_untrusted_key_is_refused():
    doctored = json.loads(json.dumps(REAL_SIGNATURE))
    doctored["keyId"] = "attacker-2026"
    _files, error = _sig._verify_signature_file(doctored)
    assert "untrusted key" in error


# ---------------------------------------------------------------------------
# 2. Plumbing, with a throwaway signing key
# ---------------------------------------------------------------------------
PLUGIN_DIR_REL = "hal/runtime-v4/plugin"

# The plugin payload of a licensed VPP, in miniature: the enforcement objects,
# the link-only Makefile that names them, and the config template the editor
# strips before upload.
PACKAGE_FILES = {
    "manifest.json": b'{"id":"com.test.board"}\n',
    f"{PLUGIN_DIR_REL}/Makefile": b"VENDOR_OBJECTS := plugin.o license_core.o license_gate.o\n",
    f"{PLUGIN_DIR_REL}/plugin.o": b"\x7fELF plugin object\n",
    f"{PLUGIN_DIR_REL}/license_core.o": b"\x7fELF license core\n",
    f"{PLUGIN_DIR_REL}/license_gate.o": b"\x7fELF license gate\n",
    f"{PLUGIN_DIR_REL}/config_template.json": b'{"plugin_name":"testplug"}\n',
    f"{PLUGIN_DIR_REL}/nested/extra.o": b"\x7fELF nested\n",
}


class _Signer:
    """A throwaway Ed25519 key, trusted only inside a `with` block."""

    def __init__(self, key_id="test-key"):
        self.key_id = key_id
        self._key = Ed25519PrivateKey.generate()

    def __enter__(self):
        self._pem = (
            self._key.public_key()
            .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
            .decode("utf-8")
        )
        _sig.TRUSTED_PACKAGE_KEYS[self.key_id] = self._pem
        return self

    def __exit__(self, *_exc):
        _sig.TRUSTED_PACKAGE_KEYS.pop(self.key_id, None)
        return False

    def sign_package(self, files: dict[str, bytes]) -> dict:
        payload = {
            "formatVersion": "1.0",
            "alg": "ed25519",
            "keyId": self.key_id,
            "packageId": "com.test.board",
            "version": "1.0.0",
            "signedAt": "2026-07-28T00:00:00.000Z",
            "files": {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()},
        }
        signature = self._key.sign(_sig.canonicalize(payload).encode("utf-8"))
        return {**payload, "signature": base64.b64encode(signature).decode("ascii")}


def _build_upload(root: str, signer: _Signer, files=None, plugin_dir=PLUGIN_DIR_REL) -> str:
    """Reproduce what the editor puts in the upload: the plugin directory's
    contents (minus the files it excludes), its checksum.sha256, and the
    forwarded package signature."""
    files = PACKAGE_FILES if files is None else files
    os.makedirs(root, exist_ok=True)
    plugin_out = os.path.join(root, "vpp_plugin")
    prefix = plugin_dir + "/"
    for rel, data in files.items():
        if not rel.startswith(prefix):
            continue
        inner = rel[len(prefix) :]
        if os.path.basename(inner) in _sig.EDITOR_EXCLUDED_BASENAMES:
            continue
        out = os.path.join(plugin_out, inner)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as handle:
            handle.write(data)
    # The editor's cache key. Content is irrelevant here; presence is what the
    # gate has to tolerate, since it cannot be covered by the signature.
    with open(os.path.join(plugin_out, "checksum.sha256"), "w") as handle:
        handle.write("deadbeef\n")
    with open(os.path.join(root, "vpp_signature.json"), "w", encoding="utf-8") as handle:
        json.dump({"pluginDir": plugin_dir, "package": signer.sign_package(files)}, handle)
    return root


def _tmp():
    return tempfile.mkdtemp(prefix="vpp-sig-test-")


def test_signed_untouched_upload_is_accepted():
    """The inverse of every test below, and the one that keeps this gate from
    quietly becoming a blanket refusal nobody notices."""
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
            result = _sig.verify_uploaded_vpp_plugin(root)
        assert result.ok, result.error
        assert result.package_id == "com.test.board"
        assert result.tree_digest and len(result.tree_digest) == 64
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_plain_program_without_vpp_plugin_is_untouched():
    """Policy: only uploads carrying vpp_plugin/ need a signature. Every
    existing user and every older editor keeps working."""
    root = _tmp()
    try:
        with open(os.path.join(root, "generated.hpp"), "w") as handle:
            handle.write("// program\n")
        result = _sig.verify_uploaded_vpp_plugin(root)
        assert result.ok
        assert result.tree_digest is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_injected_license_gate_source_is_refused():
    """The audit's attack, first half: drop in a license_gate.c that always
    answers "licensed". It has no signed hash, so the upload dies here."""
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
            with open(os.path.join(root, "vpp_plugin", "license_gate.c"), "w") as handle:
                handle.write("int license_gate_actuation_allowed(void) { return 1; }\n")
            result = _sig.verify_uploaded_vpp_plugin(root)
        assert not result.ok
        assert "not covered by the package signature" in result.error
        assert "license_gate.c" in result.error
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_edited_makefile_is_refused():
    """The audit's attack, second half: point VENDOR_OBJECTS at the stub."""
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
            makefile = os.path.join(root, "vpp_plugin", "Makefile")
            with open(makefile, "ab") as handle:
                handle.write(b"license_gate.o: license_gate.c\n\t$(CC) -fPIC -c -o $@ $<\n")
            result = _sig.verify_uploaded_vpp_plugin(root)
        assert not result.ok
        assert "does not match the package signature" in result.error
        assert "Makefile" in result.error
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_swapped_prebuilt_object_is_refused():
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
            with open(os.path.join(root, "vpp_plugin", "license_gate.o"), "wb") as handle:
                handle.write(b"\x7fELF attacker gate\n")
            result = _sig.verify_uploaded_vpp_plugin(root)
        assert not result.ok
        assert "license_gate.o" in result.error
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_dropped_enforcement_object_is_refused():
    """Deleting a signed object must fail HERE, not merely at link time: not
    every package's Makefile names its objects explicitly."""
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
            os.remove(os.path.join(root, "vpp_plugin", "license_core.o"))
            result = _sig.verify_uploaded_vpp_plugin(root)
        assert not result.ok
        assert "missing from the upload" in result.error
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_editor_excluded_files_may_be_absent():
    """config_template.json is signed in the package but the editor drops it,
    so its absence must not be read as a dropped object."""
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
            assert not os.path.exists(os.path.join(root, "vpp_plugin", "config_template.json"))
            result = _sig.verify_uploaded_vpp_plugin(root)
        assert result.ok, result.error
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_unsigned_upload_with_vpp_plugin_is_refused():
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
        os.remove(os.path.join(root, "vpp_signature.json"))
        result = _sig.verify_uploaded_vpp_plugin(root)
        assert not result.ok
        assert "no usable package signature" in result.error
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_signature_from_a_different_package_is_refused():
    """Reusing a legitimate signature.json from another package does not help:
    the hashes do not match the objects that travelled."""
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
            other = {**PACKAGE_FILES, f"{PLUGIN_DIR_REL}/license_gate.o": b"different bytes\n"}
            with open(os.path.join(root, "vpp_signature.json"), "w", encoding="utf-8") as handle:
                json.dump({"pluginDir": PLUGIN_DIR_REL, "package": signer.sign_package(other)}, handle)
            result = _sig.verify_uploaded_vpp_plugin(root)
        assert not result.ok
        assert "license_gate.o" in result.error
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_plugin_dir_cannot_traverse():
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
            with open(os.path.join(root, "vpp_signature.json"), "w", encoding="utf-8") as handle:
                json.dump(
                    {"pluginDir": "hal/../../etc", "package": signer.sign_package(PACKAGE_FILES)},
                    handle,
                )
            result = _sig.verify_uploaded_vpp_plugin(root)
        assert not result.ok
        assert "invalid pluginDir" in result.error
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_wrong_plugin_dir_cannot_launder_files():
    """pluginDir is unsigned routing information, so prove it cannot be used to
    make unsigned bytes verify: pointing it at a subtree the uploaded files do
    not belong to fails, rather than matching some other signed hash."""
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
            with open(os.path.join(root, "vpp_signature.json"), "w", encoding="utf-8") as handle:
                json.dump(
                    {"pluginDir": "hal/runtime-v4", "package": signer.sign_package(PACKAGE_FILES)},
                    handle,
                )
            result = _sig.verify_uploaded_vpp_plugin(root)
        assert not result.ok
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_seal_is_written_only_for_a_verified_plugin():
    """scripts/compile.sh keys off this file, so an accepted plain program must
    not produce one and an accepted plugin must."""
    root = _tmp()
    try:
        with _Signer() as signer:
            _build_upload(root, signer)
            result = _sig.verify_uploaded_vpp_plugin(root)
            _sig.write_verification_seal(root, result)
        seal = os.path.join(root, _sig.VERIFICATION_SEAL_NAME)
        assert os.path.exists(seal)
        with open(seal, encoding="utf-8") as handle:
            body = handle.read()
        assert f"treeDigest {result.tree_digest}" in body

        plain = _tmp()
        try:
            _sig.write_verification_seal(plain, _sig.verify_uploaded_vpp_plugin(plain))
            assert not os.path.exists(os.path.join(plain, _sig.VERIFICATION_SEAL_NAME))
        finally:
            shutil.rmtree(plain, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_tree_digest_tracks_content_and_names():
    """The digest compile.sh recomputes must change for a content edit AND for
    a rename, or a swap between the gate and `make` would slip through."""
    root = _tmp()
    try:
        os.makedirs(os.path.join(root, "d"))
        with open(os.path.join(root, "d", "a"), "wb") as handle:
            handle.write(b"one")
        first = _sig.tree_digest(root)
        with open(os.path.join(root, "d", "a"), "wb") as handle:
            handle.write(b"two")
        assert _sig.tree_digest(root) != first
        os.rename(os.path.join(root, "d", "a"), os.path.join(root, "d", "b"))
        renamed = _sig.tree_digest(root)
        with open(os.path.join(root, "d", "b"), "wb") as handle:
            handle.write(b"two")
        assert _sig.tree_digest(root) == renamed
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_signature_policy_is_one_decision():
    """The unsigned-upload policy lives in exactly one function. If someone
    changes it, this is the test that has to be updated with it."""
    assert _sig.signature_required(has_vpp_plugin=True) is True
    assert _sig.signature_required(has_vpp_plugin=False) is False


# ---------------------------------------------------------------------------
# 3. Containment of the loaded path (the .so the runtime actually dlopens)
# ---------------------------------------------------------------------------
def test_vpp_plugins_conf_containment():
    """A verified plugin tree still means nothing if vpp_plugins.conf can point
    `path` at any .so on the box. Every entry must resolve inside build/vpp/."""
    mgmt = pytest.importorskip(
        "webserver.plcapp_management",
        reason="runtime webserver package not importable (no venv)",
    )
    root = _tmp()
    try:
        os.makedirs(os.path.join(root, "build", "vpp"))
        cases = [
            ("good,./build/vpp/libx_plugin.so,1,1,./build/vpp/x.json,\n", True),
            ("evil,/tmp/evil.so,1,1,./build/vpp/x.json,\n", False),
            ("evil,../../tmp/evil.so,1,1,./build/vpp/x.json,\n", False),
            # Inside the runtime root but outside build/vpp/: still unverified.
            ("evil,./build/libplc.so,1,1,./build/vpp/x.json,\n", False),
            ("evil,./core/generated/vpp_plugin/x.so,1,1,./build/vpp/x.json,\n", False),
            # Escaping config_path, which the pre-existing per-file guard also
            # caught -- now the whole conf is refused instead of one line.
            ("evil,./build/vpp/libx_plugin.so,1,1,/etc/cron.d/runme,\n", False),
        ]
        for line, expect_ok in cases:
            conf = os.path.join(root, "vpp_plugins.conf")
            with open(conf, "w") as handle:
                handle.write(line)
            ok, reason = mgmt.validate_vpp_plugins_conf(conf, root, "build/vpp")
            assert ok is expect_ok, f"{line.strip()} -> ok={ok} reason={reason}"
    finally:
        shutil.rmtree(root, ignore_errors=True)
