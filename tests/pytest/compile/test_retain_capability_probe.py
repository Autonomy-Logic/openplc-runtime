"""The retain capability probe, and the gate it drives.

Why this file exists
--------------------
Runtime v4.2.0 shipped a shim (``core/strucpp_runtime/runtime_v4_entry.cpp``)
that calls ``strucpp::retain`` and ``strucpp::debug::retain_layout_hash``. That
file is the ONE runtime source compiled against STruC++ headers the EDITOR
shipped inside ``program.zip``, and the retain API landed in STruC++ v0.6.5 --
newer than the STruC++ pinned by every OpenPLC Editor released to date. So the
release could not build a program from any editor in the field, and nothing in
the suite noticed, because nothing here compiles the shim against a header set
older than the developer's own.

``retain_probe.cpp`` plus ``SHIM_HAS_RETAIN`` in ``scripts/Makefile.strucpp``
are the fix: measure the header set per upload, and compile the retain block out
when it cannot supply the API. These tests pin the three ways that gate can rot.

1. **The probe discriminates.** It must fail against a pre-v0.6.5 header shape
   and pass against a v0.6.5+ one. The legacy stub below is modelled on the real
   v0.6.2 ``iec_retain.hpp``: the file EXISTS and declares things in
   ``namespace strucpp``, which is exactly why a "does the header exist" check or
   a grep for the word ``retain`` would both have passed it.
2. **The shim's retain code stays behind the gate.** A single new
   ``strucpp::retain`` call added outside the ``#ifdef`` reintroduces the
   original bug in full.
3. **The probe covers what the shim uses.** A probe that checks less than the
   shim calls would pass for a header set the shim cannot compile against --
   turning a clear build error into a confusing one.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SHIM = _REPO / "core" / "strucpp_runtime" / "runtime_v4_entry.cpp"
_PROBE = _REPO / "core" / "strucpp_runtime" / "retain_probe.cpp"
_MAKEFILE = _REPO / "scripts" / "Makefile.strucpp"

_CXX = shutil.which("g++") or shutil.which("c++") or shutil.which("clang++")

# Every name in the shim that only a v0.6.5+ header set can satisfy. Kept as a
# list rather than a regex so a failure names the missing one.
_RETAIN_NAMES = (
    "strucpp::retain::blob_size",
    "strucpp::retain::pack",
    "strucpp::retain::unpack",
    "strucpp::debug::retain_layout_hash",
)

# --- header stubs ----------------------------------------------------------
#
# Deliberately minimal. The probe includes exactly two headers, so these are all
# it can see, and hand-written stubs let the test state the SHAPE that matters
# without vendoring a copy of STruC++ into the runtime repo.

_DEBUG_TABLE_COMMON = """
#pragma once
#include <cstdint>
namespace strucpp {
namespace debug {
extern const uint16_t retain_var_count;
}  // namespace debug
}  // namespace strucpp
"""

# v0.6.5+ added the layout-hash declaration alongside the retain table.
_DEBUG_TABLE_CAPABLE = (
    _DEBUG_TABLE_COMMON
    + """
namespace strucpp {
namespace debug {
extern const uint32_t retain_layout_hash;
}  // namespace debug
}  // namespace strucpp
"""
)

# Modelled on the real v0.6.1 - v0.6.4 header: present, in namespace strucpp,
# talks about retain variables, and offers the shim nothing it can call.
_IEC_RETAIN_LEGACY = """
#pragma once
#include <cstddef>
#include <cstdint>
namespace strucpp {
// Metadata for a retain variable. A future namespace retain will own the walk.
struct RetainVarDescriptor {
    const char* name;
    void*       storage;
    uint16_t    size;
};
}  // namespace strucpp
"""

# The v0.6.5 shape: same three entry points the real header exposes, same
# argument order, templated on the leaf accessors.
_IEC_RETAIN_CAPABLE = """
#pragma once
#include <cstddef>
#include <cstdint>
#include "debug_table.hpp"
namespace strucpp {
namespace retain {
enum class LoadResult : uint8_t { Ok = 0, Empty = 1, StaleLayout = 2 };

template <typename SizeLeaf>
inline size_t blob_size(SizeLeaf size_of) noexcept { (void)size_of; return 0; }

template <typename ReadLeaf, typename SizeLeaf>
inline size_t pack(uint8_t* out, size_t cap, ReadLeaf read_leaf, SizeLeaf size_of) noexcept {
    (void)out; (void)cap; (void)read_leaf; (void)size_of; return 0;
}

template <typename WriteLeaf, typename SizeLeaf>
inline LoadResult unpack(const uint8_t* blob, size_t len,
                         WriteLeaf write_leaf, SizeLeaf size_of) noexcept {
    (void)blob; (void)len; (void)write_leaf; (void)size_of; return LoadResult::Ok;
}
}  // namespace retain
}  // namespace strucpp
"""


def _header_set(root: Path, *, capable: bool) -> Path:
    """Write a stub `strucpp_runtime/include` and return it."""
    include = root / "strucpp_runtime" / "include"
    include.mkdir(parents=True)
    (include / "debug_table.hpp").write_text(
        _DEBUG_TABLE_CAPABLE if capable else _DEBUG_TABLE_COMMON, encoding="utf-8"
    )
    (include / "iec_retain.hpp").write_text(
        _IEC_RETAIN_CAPABLE if capable else _IEC_RETAIN_LEGACY, encoding="utf-8"
    )
    return include


def _run_probe(include: Path) -> subprocess.CompletedProcess:
    """The probe exactly as Makefile.strucpp runs it."""
    return subprocess.run(
        [
            _CXX,
            "-std=c++17",
            "-DSTRUCPP_THREADED",
            "-I",
            str(include),
            "-fsyntax-only",
            str(_PROBE),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# 1. The probe discriminates
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_CXX is None, reason="no C++ compiler on PATH")
def test_the_probe_fails_against_pre_v065_headers(tmp_path):
    """The v4.2.0 regression, reproduced at the level that catches it.

    If this ever passes, the gate has stopped gating and every editor in the
    field is one release away from being unable to upload again.
    """
    result = _run_probe(_header_set(tmp_path, capable=False))

    assert result.returncode != 0, "probe accepted a header set with no retain API"
    # And it fails for the RIGHT reason -- not a typo or a missing include path.
    assert "retain" in result.stderr


@pytest.mark.skipif(_CXX is None, reason="no C++ compiler on PATH")
def test_the_probe_passes_against_v065_headers(tmp_path):
    result = _run_probe(_header_set(tmp_path, capable=True))

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(_CXX is None, reason="no C++ compiler on PATH")
def test_a_missing_iec_retain_header_is_not_a_build_failure(tmp_path):
    """Older still: a header set without the file at all.

    The Makefile's wildcard guard is what handles this, but the probe must fail
    cleanly rather than do something surprising if it is ever run anyway.
    """
    include = tmp_path / "strucpp_runtime" / "include"
    include.mkdir(parents=True)
    (include / "debug_table.hpp").write_text(_DEBUG_TABLE_COMMON, encoding="utf-8")

    assert _run_probe(include).returncode != 0


# ---------------------------------------------------------------------------
# 2. The shim's retain code stays behind the gate
# ---------------------------------------------------------------------------


def _gated_regions(source: str) -> list[tuple[int, int]]:
    """Line spans covered by `#ifdef STRUCPP_SHIM_HAS_RETAIN` ... `#endif`.

    Counts nesting so an inner #if inside the block does not close it early.
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    depth = 0
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if start is None:
            if re.match(r"#\s*if(def)?\s+.*STRUCPP_SHIM_HAS_RETAIN", stripped):
                start, depth = lineno, 1
            continue
        if re.match(r"#\s*if", stripped):
            depth += 1
        elif re.match(r"#\s*endif", stripped):
            depth -= 1
            if depth == 0:
                spans.append((start, lineno))
                start = None
    assert start is None, "unterminated STRUCPP_SHIM_HAS_RETAIN block in the shim"
    return spans


@pytest.mark.parametrize("name", _RETAIN_NAMES)
def test_every_retain_reference_in_the_shim_is_gated(name):
    """One ungated call is the whole v4.2.0 bug back again."""
    source = _SHIM.read_text(encoding="utf-8")
    spans = _gated_regions(source)
    assert spans, "the shim has no STRUCPP_SHIM_HAS_RETAIN block at all"

    for lineno, line in enumerate(source.splitlines(), start=1):
        code = line.split("//", 1)[0]
        if name not in code:
            continue
        assert any(lo <= lineno <= hi for lo, hi in spans), (
            f"{_SHIM.name}:{lineno} uses {name} outside "
            f"#ifdef STRUCPP_SHIM_HAS_RETAIN -- an upload from an editor older "
            f"than v4.2.12 will fail to build"
        )


def test_the_iec_retain_include_is_gated():
    """Belt and braces for a header set that lacks the file entirely."""
    source = _SHIM.read_text(encoding="utf-8")
    spans = _gated_regions(source)

    for lineno, line in enumerate(source.splitlines(), start=1):
        if not re.match(r'\s*#\s*include\s+"iec_retain\.hpp"', line):
            continue
        assert any(lo <= lineno <= hi for lo, hi in spans), (
            f"{_SHIM.name}:{lineno} includes iec_retain.hpp unconditionally"
        )


# ---------------------------------------------------------------------------
# 3. The probe covers what the shim uses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _RETAIN_NAMES)
def test_the_probe_exercises_every_name_the_shim_needs(name):
    """Keeps the two files honest about each other.

    A probe that touches fewer names than the shim calls reports "capable" for a
    header set the shim cannot compile against -- the build then fails with the
    original confusing error, and the gate looks innocent.
    """
    probe = _PROBE.read_text(encoding="utf-8")
    code = "\n".join(line.split("//", 1)[0] for line in probe.splitlines())

    assert name in code, f"retain_probe.cpp never references {name}"


def test_the_makefile_only_defines_the_gate_from_the_probe():
    """The wiring: verdict -> define -> the shim's own flag set.

    Checked as text because the alternative is running `make` against a
    generated tree that does not exist outside a real upload.
    """
    makefile = _MAKEFILE.read_text(encoding="utf-8")

    assert "-fsyntax-only $(RETAIN_PROBE)" in makefile
    assert "SHIM_HAS_RETAIN :=" in makefile
    # The define must be reachable ONLY through the probe's verdict.
    for line in makefile.splitlines():
        if "-DSTRUCPP_SHIM_HAS_RETAIN" in line:
            assert "$(if $(SHIM_HAS_RETAIN)" in line, line
    # ...and the shim recipe must use the gated flags, not the common ones.
    assert "$(CXX) $(SHIM_CXXFLAGS) -c $< -o $@" in makefile


# ---------------------------------------------------------------------------
# 4. The wiring: probe verdict -> compiler flag
# ---------------------------------------------------------------------------
#
# Everything above can pass while the build still does the wrong thing. It did:
# the first cut of the Makefile wrote the verdict with a line continuation, and
# GNU make counts the lone space that leaves behind as a NON-EMPTY $(if)
# condition -- so a legacy header set produced " " instead of "", the gate turned
# on for exactly the uploads it exists to protect, and the original error came
# straight back. Only asking make itself catches that class of bug.

_MAKE = shutil.which("make") or shutil.which("gmake")


def _shim_recipe(generated_dir: Path, build_dir: Path) -> str:
    """What make WOULD run to compile the shim, without running it.

    GENERATED_DIR and BUILD_DIR are both overridden: the first points the probe
    at the stub header set, the second keeps a stale object in the developer's
    real build/ from making the target look up to date (which would print
    nothing to assert on).
    """
    proc = subprocess.run(
        [
            _MAKE,
            "-f",
            "scripts/Makefile.strucpp",
            f"GENERATED_DIR={generated_dir}",
            f"BUILD_DIR={build_dir}",
            "-n",
            str(build_dir / "runtime_v4_entry.o"),
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _generated_dir(tmp_path: Path, *, capable: bool) -> Path:
    """A stub `core/generated` with the two prerequisites the shim target needs."""
    generated = tmp_path / "generated"
    generated.mkdir()
    _header_set(generated, capable=capable)
    (generated / "generated.hpp").write_text("#pragma once\n", encoding="utf-8")
    (generated / "defines.h").write_text(
        '#pragma once\n#define PROGRAM_MD5 "x"\n', encoding="utf-8"
    )
    return generated


@pytest.mark.skipif(_MAKE is None or _CXX is None, reason="needs make and a C++ compiler")
def test_make_omits_the_define_for_a_legacy_header_set(tmp_path):
    """The regression that shipped, and then very nearly shipped twice."""
    recipe = _shim_recipe(_generated_dir(tmp_path, capable=False), tmp_path / "build")

    assert "runtime_v4_entry.cpp" in recipe
    assert "-DSTRUCPP_SHIM_HAS_RETAIN" not in recipe


@pytest.mark.skipif(_MAKE is None or _CXX is None, reason="needs make and a C++ compiler")
def test_make_adds_the_define_for_a_capable_header_set(tmp_path):
    recipe = _shim_recipe(_generated_dir(tmp_path, capable=True), tmp_path / "build")

    assert "-DSTRUCPP_SHIM_HAS_RETAIN" in recipe


@pytest.mark.skipif(_MAKE is None or _CXX is None, reason="needs make and a C++ compiler")
def test_the_legacy_path_says_so_in_the_build_log(tmp_path):
    """The user is watching this log in the editor's console.

    A program whose RETAIN variables silently do nothing is worse than one that
    fails, so the build has to name the reason and the version that fixes it.
    """
    recipe = _shim_recipe(_generated_dir(tmp_path, capable=False), tmp_path / "build")

    assert "RETAIN:" in recipe
    assert "v0.6.5" in recipe, "the notice must name the STruC++ version that adds support"
    assert "v4.2.12" in recipe, "the notice must name the editor version that adds support"


@pytest.mark.skipif(_MAKE is None or _CXX is None, reason="needs make and a C++ compiler")
def test_the_capable_path_stays_quiet(tmp_path):
    recipe = _shim_recipe(_generated_dir(tmp_path, capable=True), tmp_path / "build")

    assert "RETAIN:" not in recipe
