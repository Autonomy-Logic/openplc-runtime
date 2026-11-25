# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""run_compile: a failed build leaves the running program, its .so and the installed HMI alone."""

from pathlib import Path

import pytest

from webserver import plcapp_management as pm


class _Runtime:
    def __init__(self) -> None:
        self.stops = 0

    def stop_plc(self) -> str:
        self.stops += 1
        return "OK\n"

    def status_plc(self) -> str:
        return "STATUS:STOPPED\n"

    def reset_crash_tracking(self) -> None:
        pass


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "build" / "hmi").mkdir(parents=True)
    (tmp_path / "build" / "libplc_1.so").write_text("running program")
    (tmp_path / "build" / "hmi" / "hmi.seal").write_text("installed hmi")
    (tmp_path / "scripts" / "compile-clean.sh").write_text(
        "touch build/cleaned\nmv build/new_libplc.so build/libplc_2.so\n"
    )
    monkeypatch.setattr(pm, "update_plugin_configurations", lambda _cwd: None)
    monkeypatch.setattr(pm, "_wait_for_plc_idle", lambda *_a, **_k: True)
    monkeypatch.setattr(pm.project_snapshot, "promote", lambda: True)
    monkeypatch.setattr(pm.project_snapshot, "discard_staged", lambda: None)
    pm.build_state.clear()
    return tmp_path


def _compile_script(root: Path, body: str) -> None:
    (root / "scripts" / "compile.sh").write_text("echo built > build/new_libplc.so\n" + body)


def test_a_failed_build_stops_nothing_and_swaps_nothing(root: Path) -> None:
    _compile_script(root, "exit 4\n")
    runtime = _Runtime()
    pm.run_compile(runtime)
    assert pm.build_state.status is pm.BuildStatus.FAILED
    assert pm.build_state.exit_code == 1
    assert runtime.stops == 0
    assert not (root / "build" / "cleaned").exists()
    assert not (root / "build" / "new_libplc.so").exists()
    assert (root / "build" / "libplc_1.so").read_text() == "running program"
    assert (root / "build" / "hmi" / "hmi.seal").read_text() == "installed hmi"
    assert any("has not been updated" in line for line in pm.build_state.logs)


def test_a_successful_build_swaps_the_program(root: Path) -> None:
    _compile_script(root, "exit 0\n")
    runtime = _Runtime()
    pm.run_compile(runtime)
    assert pm.build_state.status is pm.BuildStatus.SUCCESS
    assert runtime.stops == 1
    assert (root / "build" / "libplc_2.so").read_text() == "built\n"
