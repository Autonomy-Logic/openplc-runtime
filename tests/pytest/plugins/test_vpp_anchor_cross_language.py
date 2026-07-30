"""Cross-language contract test for the licensing ANCHOR normalization.

Why this exists
---------------
``webserver/vpp_license_debug.py`` normalizes the raw hardware anchor before
putting it on the wire for FC 0x48, and the editor hashes exactly those bytes
into the ``deviceId`` a license is signed for. The plugin's C
(``rpi_plugin.c::license_gate_bringup``) normalizes the SAME file, on the SAME
board, and hands its result to the closed ``license_core``, which is the side
that decides whether the license verifies. The C is therefore canonical.

Both sides carried a comment claiming byte-identity with the other, and both were
wrong: the Python stripped a trailing TAB and the C never did, so an anchor
ending in 0x09 derived a different ``deviceId`` on each side --
``sha256("openplc-dev-v1|" + "8625807b0a83ae7d\\t")[:16]`` is
``ac07623afa23c771...`` where the C computes ``7146518f9842adac...``. Nothing
would log, nothing would fail: the customer pays and the license simply never
works. The C also reads into ``uint8_t anchor[64]`` and silently truncates,
while the Python read the whole file and framed up to 255 bytes on the wire.

How this test stays honest
--------------------------
It does NOT re-implement the C normalization in Python and compare that against
itself -- that would only prove this file agrees with its own assumptions. There
is already one test in this suite that does the weaker thing on purpose and says
so (``test_vpp_license_delivery.py``'s hand transcription of
``derive_license_path``). This file instead:

1. extracts the REAL strip set and the REAL buffer size out of the C source by
   source text, and asserts the Python constants equal them; and
2. extracts the REAL ``read_file_bytes`` function and the REAL anchor block
   (declaration, read, and normalization loop), compiles them unmodified, and
   compares the bytes the C produces with the bytes ``_read_anchor()`` produces,
   over the same files.

If either side's source changes, this test runs the NEW text, not a stale copy.

Where the C lives
-----------------
The plugin C source is in the sibling ``openplc-packages`` repository, not in
this one, so this test resolves it and SKIPS when it cannot be found. Point
``OPENPLC_PACKAGES_DIR`` at a checkout to run it from elsewhere. This is a real
limitation -- on a runtime-only CI checkout this file skips, exactly like the
symlink case in ``test_vpp_license_debug.py`` -- and the mirror of this test
belongs in ``openplc-packages``, where the C source is always present.
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
_RPI_PLUGIN_REL = os.path.join(
    "packages",
    "com.openplc.raspberry-pi-licensed",
    "hal",
    "runtime-v4",
    "source",
    "rpi_plugin.c",
)


def _find_rpi_plugin_source():
    """The licensed Pi plugin's C source, or None.

    Checked in order: an explicit OPENPLC_PACKAGES_DIR, then a sibling
    ``openplc-packages`` checkout next to this repository.
    """
    roots = []
    env_root = os.environ.get("OPENPLC_PACKAGES_DIR")
    if env_root:
        roots.append(env_root)
    roots.append(os.path.join(os.path.dirname(_REPO_ROOT), "openplc-packages"))
    for root in roots:
        candidate = os.path.join(root, _RPI_PLUGIN_REL)
        if os.path.isfile(candidate):
            return candidate
    return None


_RPI_PLUGIN_C = _find_rpi_plugin_source()
_CC = next((c for c in ("cc", "gcc", "clang") if shutil.which(c)), None)

pytestmark = pytest.mark.skipif(
    _RPI_PLUGIN_C is None,
    reason=(
        "rpi_plugin.c not found: this test needs the sibling openplc-packages "
        "checkout (or OPENPLC_PACKAGES_DIR) because the canonical anchor "
        "normalization lives there, not in this repo"
    ),
)


def _c_source() -> str:
    # newline=None gives universal-newline translation; strip any stray \r on
    # top of it, because this repo has no .gitattributes and a Windows checkout
    # can hand us CRLF where the extraction patterns expect bare \n (tasks
    # #50/#58). Normalizing line endings for matching does not change what the
    # C does.
    with open(_RPI_PLUGIN_C, "r", encoding="utf-8", newline=None) as handle:
        return handle.read().replace("\r", "")


def _extract(pattern: str, what: str) -> str:
    """Pull a chunk of real C out of the source by exact text.

    Raises rather than returning an empty string: a test that "passes" because
    it extracted nothing and compared nothing would be worse than no test.
    """
    match = re.compile(pattern, re.MULTILINE | re.DOTALL).search(_c_source())
    if not match:
        raise AssertionError(
            f"could not locate {what} in {_RPI_PLUGIN_C} -- the extraction "
            "pattern in this test no longer matches the real source, which "
            "means this test is not exercising real code"
        )
    return match.group(0)


_ANCHOR_BLOCK_PATTERN = r"^    uint8_t anchor\[\d+\];$.*?^    \}$"
_READ_FILE_BYTES_PATTERN = r"^static long read_file_bytes\(.*?^\}$"


# ---------------------------------------------------------------------------
# 1. The constants, read out of the real C source. No compiler needed.
# ---------------------------------------------------------------------------


def test_python_strips_exactly_the_bytes_the_c_strips():
    """The strip set is the whole contract: one extra byte on either side moves
    the deviceId and the purchased license stops matching the hardware."""
    block = _extract(_ANCHOR_BLOCK_PATTERN, "the anchor normalization block")
    # Every character literal compared against anchor[alen - 1] in the real loop.
    comparisons = re.findall(r"anchor\[alen - 1\] == '((?:\\.|[^'\\])+)'", block)
    assert comparisons, f"no strip comparisons found in the extracted C:\n{block}"

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
    block = _extract(_ANCHOR_BLOCK_PATTERN, "the anchor normalization block")
    size = int(re.search(r"uint8_t anchor\[(\d+)\];", block).group(1))
    assert size == lic.ANCHOR_MAX_BYTES, (
        f"the C reads the anchor into uint8_t anchor[{size}] but this runtime "
        f"caps at {lic.ANCHOR_MAX_BYTES}. The C never sees more than its buffer, "
        "so anything above the cap must be refused here, not truncated."
    )


# ---------------------------------------------------------------------------
# 2. The behaviour, by executing the real C. Needs a compiler.
# ---------------------------------------------------------------------------

_HARNESS = """\
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* The anchor block below reads RPI_ANCHOR_PATH; point it at argv[1] so the same
   real code can be run against a temp file instead of /proc. */
#define RPI_ANCHOR_PATH argv[1]

%(read_file_bytes)s

int main(int argc, char **argv)
{
    if (argc < 2) return 2;
%(anchor_block)s
    for (long i = 0; i < alen; i++) printf("%%02x", anchor[i]);
    printf("\\n");
    return 0;
}
"""


def _build_c_normalizer(workdir: str) -> str:
    source = _HARNESS % {
        "read_file_bytes": _extract(_READ_FILE_BYTES_PATTERN, "read_file_bytes()"),
        "anchor_block": _extract(_ANCHOR_BLOCK_PATTERN, "the anchor normalization block"),
    }
    src_path = os.path.join(workdir, "anchor_harness.c")
    with open(src_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(source)
    exe_path = os.path.join(workdir, "anchor_harness")
    result = subprocess.run(
        [_CC, "-std=c99", "-Wall", "-o", exe_path, src_path],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "the extracted C did not compile -- the extraction is picking up text "
        f"that is not the real function/block:\n{result.stdout}\n{result.stderr}\n"
        f"--- generated source ---\n{source}"
    )
    return exe_path


def _c_normalize(exe_path: str, workdir: str, raw: bytes) -> bytes:
    anchor_file = os.path.join(workdir, "serial-number")
    with open(anchor_file, "wb") as handle:
        handle.write(raw)
    result = subprocess.run([exe_path, anchor_file], capture_output=True, text=True, check=False)
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
        exe = _build_c_normalizer(workdir)
        from_c = _c_normalize(exe, workdir, raw)
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

    The C reads 64 bytes and hashes those; a longer anchor would have this side
    hash more, so the two deviceIds differ by construction. Rather than serve
    bytes that derive an identity the verifier can never reproduce, 0x48 refuses
    with TOO_LARGE (0x81) -- an error the editor surfaces, instead of a license
    bought against a deviceId that will never validate.
    """
    raw = b"d" * 100
    workdir = tempfile.mkdtemp(prefix="vpp-anchor-cross-lang-big-")
    try:
        exe = _build_c_normalizer(workdir)
        from_c = _c_normalize(exe, workdir, raw)
        assert len(from_c) == lic.ANCHOR_MAX_BYTES  # the C silently truncates

        anchor_file = os.path.join(workdir, "serial-number-py")
        with open(anchor_file, "wb") as handle:
            handle.write(raw)
        monkeypatch.setattr(lic, "ANCHOR_PATH", anchor_file)

        # The bytes really do diverge...
        assert lic._read_anchor() != from_c
        # ...so nothing is put on the wire.
        assert lic.handle_license_command("48") == "48 81"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
