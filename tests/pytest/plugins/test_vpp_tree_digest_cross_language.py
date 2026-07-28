"""Cross-language contract test for the VPP plugin tree digest.

Why this exists
---------------
The upload gate (``webserver/vpp_package_signature.py``) computes a sha256
tree digest over the verified ``vpp_plugin/`` directory and writes it into
the verification seal. ``scripts/compile.sh`` recomputes that SAME digest,
in bash, right before it lets the uploaded Makefile run
(``check_vpp_verification_seal`` -> ``vpp_tree_digest``), and refuses the
build if the two disagree.

Nobody fixed by test that the two computations actually agree. They were
compared by hand once, for one tree, during the work that added the
signature gate (a nested, mixed-case, hyphen/underscore tree; both sides
produced ``64772944...6bf5c``). Every line of either function is free to
drift after that -- a change to the sort locale, the hash line format, the
path separator, anything -- and the FIRST symptom would be every legitimate
VPP upload refusing to compile in the field. That is an availability
failure, not a security one, but it is just as silent: nothing today would
catch it before a user hits it.

How this test stays honest
---------------------------
It does NOT re-implement ``vpp_tree_digest`` in Python and compare that
reimplementation to itself -- that would only prove this file agrees with
its own assumptions about what compile.sh does. Instead it extracts the
REAL ``sha256_hex`` and ``vpp_tree_digest`` function bodies out of
``scripts/compile.sh`` by source text and executes them, unmodified, under
bash. If either function's source changes, this test runs the new text,
not a stale copy.

``scripts/compile.sh`` cannot be ``source``d directly: it is a full build
script (``set -euo pipefail``, then immediately runs ``check_required_files``
and the real compiler invocation) with no include-guard around the two
digest functions. Extracting just those two function definitions is the
only way to exercise the real bash code without also running a build.

Windows-checkout note: this repo has no ``.gitattributes``, so
``scripts/compile.sh`` is CRLF on a Windows checkout while every ``.py``
file is LF (see tasks #50/#58). The extracted function text has its ``\r``
stripped before being handed to bash for exactly that reason -- this is
normalizing line endings for execution, not changing what the function
does.
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile

import pytest

_sig = pytest.importorskip(
    "webserver.vpp_package_signature",
    reason="runtime webserver package not importable (no venv)",
)

_BASH = shutil.which("bash")
_COMPILE_SH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "compile.sh")
)


def _extract_bash_function(source: str, name: str) -> str:
    """Pull ``name() { ... }`` out of ``source`` by exact text, closing brace
    on its own line at column 0 (true of both functions in compile.sh today).

    Raises rather than silently returning an empty string: a test that
    "passes" because it extracted nothing and ran nothing would be worse
    than not existing.
    """
    pattern = re.compile(
        r"^" + re.escape(name) + r"\(\)\s*\{$.*?^\}$",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(source)
    if not match:
        raise AssertionError(
            f"could not find function {name!r} in {_COMPILE_SH} -- the "
            "extraction pattern in this test no longer matches the real "
            "source, which means this test is not exercising real code"
        )
    return match.group(0)


def _bash_tree_digest(directory: str) -> str:
    """Run the REAL ``vpp_tree_digest`` (plus its ``sha256_hex`` helper)
    extracted from scripts/compile.sh, over ``directory``."""
    with open(_COMPILE_SH, "r", encoding="utf-8", newline=None) as handle:
        source = handle.read()
    # newline=None already gives universal-newline translation, but be
    # explicit about the CRLF checkout: strip any stray \r before this text
    # is interpreted by bash, which would otherwise choke on it or (worse)
    # silently fold it into a path/hash string.
    sha256_hex_fn = _extract_bash_function(source, "sha256_hex").replace("\r", "")
    vpp_tree_digest_fn = _extract_bash_function(source, "vpp_tree_digest").replace("\r", "")

    script = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            sha256_hex_fn,
            vpp_tree_digest_fn,
            'vpp_tree_digest "$1"',
            "",
        ]
    )

    fd, script_path = tempfile.mkstemp(prefix="vpp-tree-digest-", suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)
        result = subprocess.run(
            [_BASH, script_path, directory],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        os.remove(script_path)

    assert result.returncode == 0, (
        f"vpp_tree_digest (bash) exited {result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    digest = result.stdout.strip()
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), (
        f"vpp_tree_digest (bash) did not print a sha256 hex digest: {result.stdout!r}"
    )
    return digest


def _write_tree(root: str, files: dict) -> None:
    for rel, data in files.items():
        out = os.path.join(root, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as handle:
            handle.write(data)


pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not found on PATH")


# ---------------------------------------------------------------------------
# The edge cases: everything the task called out as a place the two
# implementations' sort/hash could visibly diverge if either side drifted.
# ---------------------------------------------------------------------------
EDGE_CASE_TREE = {
    # Nested directories, depth > 1.
    "hal/runtime-v4/plugin/license_core.o": b"\x7fELF license core\n",
    "hal/runtime-v4/plugin/nested/deep/leaf.o": b"deep nested leaf\n",
    # Mixed case: same name, different case, must sort as distinct, separate
    # entries (uppercase ASCII sorts before lowercase in a byte-order sort).
    "Config.json": b'{"case":"upper"}\n',
    "config.json": b'{"case":"lower"}\n',
    # Hyphen vs underscore vs neither, sharing a long common prefix. Byte
    # values: '-' (0x2D) < '.' (0x2E) < '/' (0x2F) < '_' (0x5F) < letters.
    "license-core-extra.o": b"hyphen variant\n",
    "license_core_extra.o": b"underscore variant\n",
    "licensecoreextra.o": b"no separator variant\n",
    # A bare file whose name is a directory-name prefix of another entry's
    # directory, so a flat full-path sort and a component-wise sort could, in
    # principle, disagree about where the bare file lands relative to the
    # directory's contents.
    "foo.txt": b"bare file named foo.txt\n",
    "foo/y.txt": b"file inside foo/\n",
    "foo-bar/x.txt": b"file inside foo-bar/\n",
    # ASCII-vs-natural-order collision: byte/ASCII sort puts file1 < file10 <
    # file2 (compares the '1' before the '0'), which is NOT numeric order.
    # Both implementations must agree on the SAME (ASCII) order, not on what
    # a human would consider "natural".
    "series/file1.txt": b"one\n",
    "series/file2.txt": b"two\n",
    "series/file10.txt": b"ten\n",
    # Case-mixed directory alongside a same-named-but-cased sibling.
    "Assets/logo.png": b"asset upper dir\n",
    "assets/logo.png": b"asset lower dir\n",
}


def test_python_and_bash_tree_digest_agree_on_edge_case_tree():
    """The contract this whole file exists to pin: sha256_hex(python) ==
    sha256_hex(bash) over the exact same tree, covering nested depth,
    mixed case, hyphen/underscore, and ASCII-vs-natural sort collisions."""
    root = tempfile.mkdtemp(prefix="vpp-tree-digest-cross-lang-")
    try:
        _write_tree(root, EDGE_CASE_TREE)
        python_digest = _sig.tree_digest(root)
        bash_digest = _bash_tree_digest(root)
        assert python_digest == bash_digest, (
            f"tree_digest (python) = {python_digest}\n"
            f"vpp_tree_digest (bash) = {bash_digest}\n"
            "These MUST agree byte for byte -- scripts/compile.sh recomputes "
            "this digest and refuses to build any upload where it disagrees "
            "with the seal python wrote, so a real divergence here means "
            "every legitimate VPP upload stops compiling."
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_python_and_bash_tree_digest_agree_on_empty_tree():
    """Zero files is the degenerate case: both sides must hash the same
    (empty) byte stream rather than, say, one of them erroring out."""
    root = tempfile.mkdtemp(prefix="vpp-tree-digest-cross-lang-empty-")
    try:
        assert _sig.tree_digest(root) == _bash_tree_digest(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_python_and_bash_tree_digest_agree_on_single_deep_file():
    """A minimal nested case in isolation, so a failure here narrows the
    problem down to path handling rather than sort order among many files."""
    root = tempfile.mkdtemp(prefix="vpp-tree-digest-cross-lang-deep-")
    try:
        _write_tree(root, {"a/b/c/d/leaf-file_name.o": b"single deep file\n"})
        assert _sig.tree_digest(root) == _bash_tree_digest(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_python_and_bash_tree_digest_both_change_on_rename():
    """Companion to test_tree_digest_tracks_content_and_names in
    test_vpp_plugin_signature.py, but checked in BOTH languages: a rename
    that leaves content untouched must still move the digest on the bash
    side too, or a swap between the gate and `make` could rename a file
    around a still-matching seal."""
    root = tempfile.mkdtemp(prefix="vpp-tree-digest-cross-lang-rename-")
    try:
        _write_tree(root, {"d/a": b"same bytes\n"})
        before_py, before_sh = _sig.tree_digest(root), _bash_tree_digest(root)
        os.rename(os.path.join(root, "d", "a"), os.path.join(root, "d", "b"))
        after_py, after_sh = _sig.tree_digest(root), _bash_tree_digest(root)
        assert before_py != after_py
        assert before_sh != after_sh
        assert after_py == after_sh
    finally:
        shutil.rmtree(root, ignore_errors=True)
