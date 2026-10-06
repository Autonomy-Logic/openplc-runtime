# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""A refused program bundle says why in the build log.

analyze_zip sent its reasons only to the server logger, so the editor and
the CLI saw the build fail with empty logs and "Compilation failed". Each
refusal now also goes to the build log the status endpoint returns.
"""

import zipfile

from webserver import plcapp_management as pm


def _zip(tmp_path, name, data):
    path = tmp_path / "program.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, data)
    return str(path)


def test_file_over_the_limit_is_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MAX_FILE_SIZE", 100)
    pm.build_state.logs.clear()
    ok, _ = pm.analyze_zip(_zip(tmp_path, "debug-map.json", "x" * 101))
    assert not ok
    assert any("debug-map.json" in line and "limit" in line for line in pm.build_state.logs)


def test_unsafe_path_is_logged(tmp_path):
    pm.build_state.logs.clear()
    ok, _ = pm.analyze_zip(_zip(tmp_path, "../evil.c", "int x;"))
    assert not ok
    assert any("Unsafe path" in line for line in pm.build_state.logs)


def test_a_normal_bundle_logs_no_error(tmp_path):
    pm.build_state.logs.clear()
    ok, _ = pm.analyze_zip(_zip(tmp_path, "program.st", "PROGRAM P END_PROGRAM"))
    assert ok
    assert not any("[ERROR]" in line for line in pm.build_state.logs)
