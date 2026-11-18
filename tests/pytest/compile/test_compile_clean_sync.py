# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

import os
import re
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
    """Write a `sync` stand-in that logs its argument count and a build/ listing, then exits 0."""
    shim_dir = root / "shim"
    shim_dir.mkdir()
    log = root / "sync.log"
    build = root / "build"
    shim = shim_dir / "sync"
    shim.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\t%s\\n' \"$#\" \"$(ls -1A '{build}' | tr '\\n' ' ')\" >> '{log}'\n"
        "exit 0\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim_dir


def _run_compile_clean(root: Path, shim_dir: Path) -> subprocess.CompletedProcess:
    """Run scripts/compile-clean.sh from `root` with the `sync` shim first on PATH."""
    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _sync_calls(root: Path) -> list[tuple[int, list[str]]]:
    """Return each logged `sync` call as (argument count, build/ entries)."""
    log = root / "sync.log"
    if not log.exists():
        return []
    calls = []
    for line in log.read_text().splitlines():
        argc, listing = line.split("\t", 1)
        calls.append((int(argc), listing.split()))
    return calls


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
    last_argc, last_listing = calls[-1]
    assert libraries[0] in last_listing, last_listing
    assert "new_libplc.so" not in last_listing, last_listing
    assert last_argc == 0, f"sync got {last_argc} argument(s); it must flush every filesystem"


def test_a_failed_build_still_exits_non_zero_and_leaves_no_library(build_root):
    build = build_root / "build"
    shim_dir = _install_sync_shim(build_root)

    result = _run_compile_clean(build_root, shim_dir)

    assert result.returncode != 0
    assert not [p.name for p in build.iterdir() if p.name.startswith("libplc_")]
    assert _sync_calls(build_root) == []
