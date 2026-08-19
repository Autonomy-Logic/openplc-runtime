"""Cross-language contract test for the licensing ANCHOR normalization.

Why this exists
---------------
``webserver/vpp_license_debug.py`` normalizes the raw hardware anchor before
putting it on the wire for FC 0x48, and the editor hashes exactly those bytes
into the ``deviceId`` a license is signed for. The closed core's
``license_platform.c`` (``__linux__`` branch) normalizes the SAME file, on the
SAME board, and hands its result to ``license_core``, which is the side that
decides whether the license verifies. The C is therefore canonical.

That C used to live in the open plugin (``rpi_plugin.c::license_gate_bringup``)
and moved into the closed core verbatim under ADR-0003 -- the packages repo
marks the logic "A CONTRACT, NOT AN IMPLEMENTATION DETAIL" for exactly the
reason this test exists: both sides once carried a comment claiming
byte-identity with the other, and both were wrong. The Python stripped a
trailing TAB and the C never did, so an anchor ending in 0x09 derived a
different ``deviceId`` on each side --
``sha256("openplc-dev-v1|" + "8625807b0a83ae7d\\t")[:16]`` is
``ac07623afa23c771...`` where the C computes ``7146518f9842adac...``. Nothing
would log, nothing would fail: the customer pays and the license simply never
works. The C also reads into a ``LIC_ANCHOR_MAX``-byte buffer and silently
truncates, while the Python read the whole file and framed up to 255 bytes on
the wire.

How this test stays honest
--------------------------
It does NOT re-implement the C normalization in Python and compare that against
itself -- that would only prove this file agrees with its own assumptions. There
is already one test in this suite that does the weaker thing on purpose and says
so (``test_vpp_license_delivery.py``'s hand transcription of
``derive_license_path``). This file instead:

1. extracts the REAL strip set out of ``license_platform.c`` and the REAL
   buffer ceiling out of ``license_platform.h`` by source text, and asserts the
   Python constants equal them; and
2. compiles ``license_platform.c`` ITSELF -- whole and unmodified, through the
   ``LIC_LINUX_ANCHOR_PATH`` override seam the file provides for its own host
   tests -- and compares the bytes the real C produces with the bytes
   ``_read_anchor()`` produces, over the same files.

If either side's source changes, this test runs the NEW text, not a stale copy.

Where the C lives
-----------------
``license-core/src/license_platform.{c,h}`` in the sibling ``openplc-packages``
repository, not in this one, so this test resolves it and SKIPS when it cannot
be found. Point ``OPENPLC_PACKAGES_DIR`` at a checkout to run it from
elsewhere. This is a real limitation -- on a runtime-only CI checkout this file
skips, exactly like the symlink case in ``test_vpp_license_debug.py`` -- and
the mirror of this test belongs in ``openplc-packages``, where the C source is
always present.
"""

import os
import re
import shutil
import subprocess
import tempfile

import pytest

lic = pytest.importorskip(
    "webserver.vpp_license_debug",
    reason="runtime webserver package not importable (no venv)",
)

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_PLATFORM_C_REL = os.path.join("license-core", "src", "license_platform.c")
_PLATFORM_H_REL = os.path.join("license-core", "src", "license_platform.h")


def _find_packages_root():
    """The openplc-packages checkout holding license_platform.{c,h}, or None.

    Checked in order: an explicit OPENPLC_PACKAGES_DIR, then a sibling
    ``openplc-packages`` checkout next to this repository.
    """
    roots = []
    env_root = os.environ.get("OPENPLC_PACKAGES_DIR")
    if env_root:
        roots.append(env_root)
    roots.append(os.path.join(os.path.dirname(_REPO_ROOT), "openplc-packages"))
    for root in roots:
        if os.path.isfile(os.path.join(root, _PLATFORM_C_REL)) and os.path.isfile(
            os.path.join(root, _PLATFORM_H_REL)
        ):
            return root
    return None


_PACKAGES_ROOT = _find_packages_root()
_CC = next((c for c in ("cc", "gcc", "clang") if shutil.which(c)), None)

pytestmark = pytest.mark.skipif(
    _PACKAGES_ROOT is None,
    reason=(
        "license_platform.{c,h} not found: this test needs the sibling "
        "openplc-packages checkout (or OPENPLC_PACKAGES_DIR) because the "
        "canonical anchor normalization lives there, not in this repo"
    ),
)


def _read_source(rel_path: str) -> str:
    # newline=None gives universal-newline translation; strip any stray \r on
    # top of it, because a Windows checkout can hand us CRLF where the
    # extraction patterns expect bare \n (tasks #50/#58). Normalizing line
    # endings for matching does not change what the C does.
    path = os.path.join(_PACKAGES_ROOT, rel_path)
    with open(path, "r", encoding="utf-8", newline=None) as handle:
        return handle.read().replace("\r", "")


# ---------------------------------------------------------------------------
# 1. The constants, read out of the real C source. No compiler needed.
# ---------------------------------------------------------------------------


def test_python_strips_exactly_the_bytes_the_c_strips():
    """The strip set is the whole contract: one extra byte on either side moves
    the deviceId and the purchased license stops matching the hardware."""
    source = _read_source(_PLATFORM_C_REL)
    # Every character literal compared against out[n - 1u] in the real
    # normalization loop (the __linux__ branch is the only place that shape
    # appears in the file).
    comparisons = re.findall(r"out\[n - 1u\] == '((?:\\.|[^'\\])+)'", source)
    assert comparisons, (
        f"no strip comparisons found in {_PLATFORM_C_REL} -- the extraction "
        "pattern no longer matches the real source, which means this test is "
        "not exercising real code"
    )

    escapes = {"\\0": b"\x00", "\\n": b"\n", "\\r": b"\r", "\\t": b"\t", " ": b" "}
    c_strip_set = set()
    for literal in comparisons:
        assert literal in escapes, f"unhandled C character literal {literal!r}"
        c_strip_set.add(escapes[literal])

    assert c_strip_set == {bytes([b]) for b in lic.ANCHOR_STRIP_BYTES}, (
        f"C strips {sorted(c_strip_set)}, Python strips "
        f"{sorted(bytes([b]) for b in lic.ANCHOR_STRIP_BYTES)}. These MUST be "
        "the same four bytes -- any difference changes the derived deviceId and "
        "the license signed for this board stops verifying, silently."
    )


def test_python_anchor_ceiling_matches_the_c_buffer():
    header = _read_source(_PLATFORM_H_REL)
    match = re.search(r"#define LIC_ANCHOR_MAX (\d+)u?\b", header)
    assert match, (
        f"LIC_ANCHOR_MAX not found in {_PLATFORM_H_REL} -- the ceiling moved "
        "and this test must follow it"
    )
    size = int(match.group(1))
    assert size == lic.ANCHOR_MAX_BYTES, (
        f"the C reads the anchor into a {size}-byte buffer (LIC_ANCHOR_MAX) but "
        f"this runtime caps at {lic.ANCHOR_MAX_BYTES}. The C never sees more "
        "than its buffer, so anything above the cap must be refused here, not "
        "truncated."
    )


# ---------------------------------------------------------------------------
# 2. The behaviour, by executing the real C. Needs a compiler.
#
# license_platform.c ships its own test seam: LIC_LINUX_ANCHOR_PATH overrides
# the /proc/device-tree path at compile time (the packages host tests use the
# same seam). So the whole file compiles UNMODIFIED, pointed at a temp file,
# and a three-line main() prints what license_platform_anchor() returned --
# no extraction, no transcription, the real translation unit end to end.
# ---------------------------------------------------------------------------

_HARNESS_MAIN = """\
#include <stdio.h>

#include "license_platform.h"

int main(void)
{
    uint8_t buf[LIC_ANCHOR_MAX];
    size_t n = license_platform_anchor(buf, sizeof buf);
    for (size_t i = 0; i < n; i++) printf("%02x", buf[i]);
    printf("\\n");
    return 0;
}
"""


def _build_c_normalizer(workdir: str) -> tuple:
    """Compile the REAL license_platform.c against a temp anchor file.

    Returns (exe_path, anchor_file): the binary reads `anchor_file` on every
    run, so each parity case just rewrites that file and re-invokes.
    """
    anchor_file = os.path.join(workdir, "serial-number")
    main_path = os.path.join(workdir, "anchor_harness_main.c")
    with open(main_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_HARNESS_MAIN)
    src_dir = os.path.join(_PACKAGES_ROOT, "license-core", "src")
    platform_c = os.path.join(_PACKAGES_ROOT, _PLATFORM_C_REL)
    exe_path = os.path.join(workdir, "anchor_harness")
    result = subprocess.run(
        [
            _CC,
            "-std=c11",
            "-Wall",
            "-I",
            src_dir,
            f'-DLIC_LINUX_ANCHOR_PATH="{anchor_file}"',
            platform_c,
            main_path,
            "-o",
            exe_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "the real license_platform.c did not compile against the harness -- "
        f"either the seam or the header contract changed:\n{result.stdout}\n{result.stderr}"
    )
    return exe_path, anchor_file


def _c_normalize(exe_path: str, anchor_file: str, raw: bytes) -> bytes:
    with open(anchor_file, "wb") as handle:
        handle.write(raw)
    result = subprocess.run([exe_path], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"C harness exited {result.returncode}: {result.stderr}"
    return bytes.fromhex(result.stdout.strip())


def _python_normalize(monkeypatch, workdir: str, raw: bytes) -> bytes:
    anchor_file = os.path.join(workdir, "serial-number-py")
    with open(anchor_file, "wb") as handle:
        handle.write(raw)
    monkeypatch.setattr(lic, "ANCHOR_PATH", anchor_file)
    return lic._read_anchor()


# Raw anchor files, and why each one is here.
PARITY_CASES = {
    # The Pi 5 shape actually measured on hardware: ASCII hex + trailing NUL.
    "pi5_serial_with_nul": b"8625807b0a83ae7d\x00",
    # The case that was PROVEN divergent: a trailing TAB the C keeps.
    "trailing_tab": b"8625807b0a83ae7d\t",
    "trailing_tab_then_nul": b"8625807b0a83ae7d\t\x00",
    # A TAB behind a space: the loop must stop at the TAB, so the space stays.
    "space_then_tab": b"8625807b0a83ae7d \t",
    # All four strippable bytes, in a mixed tail.
    "mixed_trailing_whitespace": b"8625807b0a83ae7d \r\n\x00",
    # Interior NULs must survive; only the tail is stripped.
    "interior_nul": b"abc\x00def\x00",
    # Degenerate tails.
    "all_nul": b"\x00\x00\x00\x00",
    "empty": b"",
    "single_byte": b"Z",
    # Exactly at the C buffer size, with and without padding.
    "exactly_at_ceiling": b"a" * 64,
    "at_ceiling_plus_padding": b"b" * 20 + b"\x00" * 60,
    "padding_only_past_ceiling": b"c" * 63 + b"\x00" * 200,
    # Non-ASCII bytes: neither side may interpret or transcode them.
    "high_bytes": bytes([0x00, 0xB1, 0x8C, 0xED, 0x00]),
}


@pytest.mark.skipif(_CC is None, reason="no C compiler (cc/gcc/clang) on PATH")
@pytest.mark.parametrize("case", sorted(PARITY_CASES))
def test_python_and_c_normalize_the_anchor_identically(case, monkeypatch):
    raw = PARITY_CASES[case]
    workdir = tempfile.mkdtemp(prefix="vpp-anchor-cross-lang-")
    try:
        exe, anchor_file = _build_c_normalizer(workdir)
        from_c = _c_normalize(exe, anchor_file, raw)
        from_python = _python_normalize(monkeypatch, workdir, raw)
        assert from_python == from_c, (
            f"case {case!r}: python -> {from_python!r}, C -> {from_c!r}. "
            "These MUST be identical: the editor hashes the python bytes into "
            "the deviceId a license is signed for, and license_core hashes the "
            "C bytes to decide whether that license is valid."
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.mark.skipif(_CC is None, reason="no C compiler (cc/gcc/clang) on PATH")
def test_an_anchor_longer_than_the_c_buffer_is_refused_rather_than_diverging(monkeypatch):
    """The one case where the two sides CANNOT agree, and what we do about it.

    The C reads LIC_ANCHOR_MAX bytes and hashes those; a longer anchor would
    have this side hash more, so the two deviceIds differ by construction.
    Rather than serve bytes that derive an identity the verifier can never
    reproduce, 0x48 refuses with TOO_LARGE (0x81) -- an error the editor
    surfaces, instead of a license bought against a deviceId that will never
    validate.
    """
    raw = b"d" * 100
    workdir = tempfile.mkdtemp(prefix="vpp-anchor-cross-lang-big-")
    try:
        exe, anchor_file = _build_c_normalizer(workdir)
        from_c = _c_normalize(exe, anchor_file, raw)
        assert len(from_c) == lic.ANCHOR_MAX_BYTES  # the C silently truncates

        py_anchor_file = os.path.join(workdir, "serial-number-py")
        with open(py_anchor_file, "wb") as handle:
            handle.write(raw)
        monkeypatch.setattr(lic, "ANCHOR_PATH", py_anchor_file)

        # The bytes really do diverge...
        assert lic._read_anchor() != from_c
        # ...so nothing is put on the wire.
        assert lic.handle_license_command("48") == "48 81"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
