# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "compile-clean.sh"

_LIBPLC = re.compile(r"^libplc_\d+\.so$")
_OLD_LIBRARY = "libplc_1700000000.so"
_NEW_LIBRARY_BYTES = b"\x7fELF new program built by this upload"

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="compile-clean.sh names the library with GNU date %N, which BSD date lacks",
)


def _install_sync_shim(root: Path) -> Path:
    """Write a `sync` stand-in that logs its arguments and a build/ listing, then exits 0.

    Each call is one line: the argument count, a tab, the arguments joined by a unit
    separator (0x1f), a tab, and the build/ entries joined by spaces.
    """
    shim_dir = root / "shim"
    shim_dir.mkdir()
    log = root / "sync.log"
    build = root / "build"
    shim = shim_dir / "sync"
    shim.write_text(
        "#!/bin/bash\n"
        "args=$(IFS=$'\\x1f'; printf '%s' \"$*\")\n"
        f"listing=$(ls -1A '{build}' | tr '\\n' ' ')\n"
        f"printf '%s\\t%s\\t%s\\n' \"$#\" \"$args\" \"$listing\" >> '{log}'\n"
        "exit 0\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim_dir


def _install_uname_shim(shim_dir: Path, kernel_name: str, os_name: str) -> None:
    """Write a `uname` stand-in into `shim_dir`.

    `uname` and `uname -s` print `kernel_name`, `uname -o` prints `os_name`, and any other
    invocation exits 1.
    """
    shim = shim_dir / "uname"
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "$#" -eq 0 ] || { [ "$#" -eq 1 ] && [ "$1" = "-s" ]; }; then\n'
        f"  printf '%s\\n' '{kernel_name}'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$#" -eq 1 ] && [ "$1" = "-o" ]; then\n'
        f"  printf '%s\\n' '{os_name}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_compile_clean(root: Path, shim_dir: Path | None) -> subprocess.CompletedProcess:
    """Run scripts/compile-clean.sh from `root`, with the `sync` shim first on PATH when given."""
    env = dict(os.environ)
    if shim_dir is not None:
        env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _sync_calls(root: Path) -> list[tuple[list[str], list[str]]]:
    """Return each logged `sync` call as (arguments, build/ entries)."""
    log = root / "sync.log"
    if not log.exists():
        return []
    calls = []
    for line in log.read_text().splitlines():
        argc, joined, listing = line.split("\t", 2)
        arguments = joined.split("\x1f") if int(argc) else []
        calls.append((arguments, listing.split()))
    return calls


def _the_installed_library(build: Path) -> str:
    """Return the one libplc_<n>.so in build/, asserting its new bytes and no new_libplc.so left."""
    libraries = sorted(p.name for p in build.iterdir() if _LIBPLC.match(p.name))
    assert len(libraries) == 1, sorted(p.name for p in build.iterdir())
    assert (build / libraries[0]).read_bytes() == _NEW_LIBRARY_BYTES
    assert not (build / "new_libplc.so").exists()
    return libraries[0]


@pytest.fixture
def build_root(tmp_path: Path) -> Path:
    build = tmp_path / "build"
    build.mkdir()
    (build / _OLD_LIBRARY).write_bytes(b"\x7fELF previous program")
    (build / "x.o").write_bytes(b"object file")
    return tmp_path


def test_a_successful_build_flushes_the_renamed_library_to_disk_as_its_last_action(build_root):
    build = build_root / "build"
    (build / "new_libplc.so").write_bytes(_NEW_LIBRARY_BYTES)
    shim_dir = _install_sync_shim(build_root)

    result = _run_compile_clean(build_root, shim_dir)

    assert result.returncode == 0, result.stderr
    libraries = sorted(p.name for p in build.iterdir() if _LIBPLC.match(p.name))
    assert len(libraries) == 1, sorted(p.name for p in build.iterdir())
    assert (build / libraries[0]).read_bytes() == _NEW_LIBRARY_BYTES
    assert not (build / "new_libplc.so").exists()
    assert not list(build.glob("*.o"))

    calls = _sync_calls(build_root)
    assert calls, "compile-clean.sh never called sync after installing the new library"
    last_arguments, last_listing = calls[-1]
    assert libraries[0] in last_listing, last_listing
    assert "new_libplc.so" not in last_listing, last_listing
    expected = ["--", f"build/{libraries[0]}", "build"]
    assert last_arguments == expected, "sync must flush only the new library and build/"


def test_a_failed_build_still_exits_non_zero_and_leaves_no_library(build_root):
    build = build_root / "build"
    shim_dir = _install_sync_shim(build_root)

    result = _run_compile_clean(build_root, shim_dir)

    assert result.returncode != 0
    assert not [p.name for p in build.iterdir() if p.name.startswith("libplc_")]
    assert _sync_calls(build_root) == []


@pytest.mark.skipif(shutil.which("sync") is None, reason="no sync command on this system")
def test_the_real_sync_accepts_the_library_and_the_build_directory(build_root):
    build = build_root / "build"
    (build / "new_libplc.so").write_bytes(_NEW_LIBRARY_BYTES)

    result = _run_compile_clean(build_root, None)

    assert result.returncode == 0, result.stderr
    libraries = sorted(p.name for p in build.iterdir() if _LIBPLC.match(p.name))
    assert len(libraries) == 1, sorted(p.name for p in build.iterdir())
    assert (build / libraries[0]).read_bytes() == _NEW_LIBRARY_BYTES


@pytest.mark.parametrize(
    ("kernel_name", "os_name"),
    [
        ("MSYS_NT-10.0-19045", "Msys"),
        ("MINGW64_NT-10.0-19045", "Msys"),
        ("CYGWIN_NT-10.0-19045", "Cygwin"),
    ],
    ids=["MSYS", "MinGW", "Cygwin"],
)
def test_on_msys2_and_cygwin_the_build_ends_with_a_plain_sync(
    build_root: Path, kernel_name: str, os_name: str
) -> None:
    build = build_root / "build"
    (build / "new_libplc.so").write_bytes(_NEW_LIBRARY_BYTES)
    shim_dir = _install_sync_shim(build_root)
    _install_uname_shim(shim_dir, kernel_name, os_name)

    result = _run_compile_clean(build_root, shim_dir)

    assert result.returncode == 0, result.stderr
    library = _the_installed_library(build)
    calls = _sync_calls(build_root)
    assert calls, "compile-clean.sh never called sync after installing the new library"
    last_arguments, last_listing = calls[-1]
    assert library in last_listing, last_listing
    assert last_arguments == [], "on MSYS2, MinGW and Cygwin the last sync must take no arguments"


def test_on_linux_the_build_ends_with_the_targeted_sync(build_root: Path) -> None:
    build = build_root / "build"
    (build / "new_libplc.so").write_bytes(_NEW_LIBRARY_BYTES)
    shim_dir = _install_sync_shim(build_root)
    _install_uname_shim(shim_dir, "Linux", "GNU/Linux")

    result = _run_compile_clean(build_root, shim_dir)

    assert result.returncode == 0, result.stderr
    library = _the_installed_library(build)
    calls = _sync_calls(build_root)
    assert calls, "compile-clean.sh never called sync after installing the new library"
    last_arguments, _ = calls[-1]
    expected = ["--", f"build/{library}", "build"]
    assert last_arguments == expected, "on Linux sync must flush only the new library and build/"
