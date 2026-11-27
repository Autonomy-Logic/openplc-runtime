# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Size limits of a program upload and the reasons a refused upload reports (analyze_zip)."""

import io
import os
import zipfile

import pytest

from webserver import plcapp_management
from webserver.plcapp_management import analyze_zip, build_state

MB = 1024 * 1024


def _zip(files: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    buf.seek(0)
    return buf


@pytest.fixture(autouse=True)
def _fresh_build_state() -> None:
    build_state.clear()


def test_limits_fit_the_debug_files_of_a_large_project() -> None:
    assert (plcapp_management.MAX_FILE_SIZE, plcapp_management.MAX_TOTAL_SIZE) == (
        128 * MB,
        512 * MB,
    )


def test_a_debug_map_over_ten_megabytes_is_accepted() -> None:
    safe, _ = analyze_zip(_zip({"program.cpp": b"", "debug-map.json": os.urandom(12 * MB)}))
    assert (safe, build_state.refusals) == (True, [])


def test_a_file_over_the_limit_is_refused_with_its_name_and_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plcapp_management, "MAX_FILE_SIZE", 1 * MB)
    safe, _ = analyze_zip(_zip({"program.cpp": b"", "debug-map.json": os.urandom(2 * MB)}))
    assert (safe, build_state.refusals) == (
        False,
        ["debug-map.json is 2.0 MB, over the 1.0 MB limit per file"],
    )


def test_a_program_over_the_total_limit_is_refused_with_its_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plcapp_management, "MAX_TOTAL_SIZE", 3 * MB)
    files = {f"part{i}.cpp": os.urandom(1 * MB) for i in range(4)}
    safe, _ = analyze_zip(_zip(files))
    assert (safe, build_state.refusals) == (
        False,
        ["the program unpacks to 4.0 MB, over the 3.0 MB limit"],
    )


def test_a_disallowed_file_type_is_named() -> None:
    safe, _ = analyze_zip(_zip({"program.cpp": b"", "tools/run.sh": b""}))
    assert (safe, build_state.refusals) == (
        False,
        ["tools/run.sh: file type not allowed in a program"],
    )


def test_clear_forgets_the_refusals() -> None:
    analyze_zip(_zip({"run.sh": b""}))
    build_state.clear()
    assert build_state.refusals == []
