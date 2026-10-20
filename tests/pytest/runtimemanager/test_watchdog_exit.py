# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""RuntimeManager restarts plc_main in safe mode after a watchdog fault exit."""

# pylint: disable=protected-access,redefined-outer-name

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from webserver import runtimemanager as rm


class _ExitedProcess(subprocess.Popen):
    """Popen stand-in for a process that already exited with a given code."""

    def __init__(self, code: int | None) -> None:  # pylint: disable=super-init-not-called
        self._code = code
        self._child_created = False
        self.returncode = code

    def poll(self) -> int | None:
        return self._code


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> rm.RuntimeManager:
    mgr = rm.RuntimeManager("/bin/false", "/tmp/x.sock", "/tmp/y.sock")
    monkeypatch.setattr(mgr, "_safe_stop_log_server", MagicMock())
    monkeypatch.setattr(mgr, "_safe_close_runtime_socket", MagicMock())
    monkeypatch.setattr(mgr, "_start_runtime_process", MagicMock())
    return mgr


def test_exit_code_constant_matches_runtime() -> None:
    header_path = Path(__file__).resolve().parents[3] / "core/src/plc_app/task_policy.h"
    with open(header_path, encoding="utf-8") as header_file:
        header = header_file.read()
    assert f"#define PLC_EXIT_WATCHDOG_FAULT {rm.RUNTIME_EXIT_WATCHDOG_FAULT}" in header


def test_watchdog_fault_restarts_in_safe_mode_on_first_exit(manager: rm.RuntimeManager) -> None:
    manager.process = _ExitedProcess(rm.RUNTIME_EXIT_WATCHDOG_FAULT)
    manager._handle_runtime_exit()
    manager._start_runtime_process.assert_called_once_with(safe_mode=True, after_fault=True)
    assert manager._safe_mode is True
    assert manager._crash_times == []


def test_other_exit_restarts_normally(manager: rm.RuntimeManager) -> None:
    manager.process = _ExitedProcess(1)
    manager._handle_runtime_exit()
    manager._start_runtime_process.assert_called_once_with(safe_mode=False)
    assert manager._safe_mode is False


def test_safe_mode_sticks_after_a_later_crash(manager: rm.RuntimeManager) -> None:
    manager.process = _ExitedProcess(rm.RUNTIME_EXIT_WATCHDOG_FAULT)
    manager._handle_runtime_exit()
    manager.process = _ExitedProcess(-9)
    manager._handle_runtime_exit()
    manager._start_runtime_process.assert_called_with(safe_mode=True)


def test_upload_clears_safe_mode(manager: rm.RuntimeManager) -> None:
    manager.process = _ExitedProcess(rm.RUNTIME_EXIT_WATCHDOG_FAULT)
    manager._handle_runtime_exit()
    manager.reset_crash_tracking()
    manager.process = _ExitedProcess(1)
    manager._handle_runtime_exit()
    manager._start_runtime_process.assert_called_with(safe_mode=False)


def test_rapid_crashes_still_enter_safe_mode_without_fault_flag(manager: rm.RuntimeManager) -> None:
    for _ in range(rm.MAX_RAPID_CRASHES):
        manager.process = _ExitedProcess(-11)
        manager._handle_runtime_exit()
    manager._start_runtime_process.assert_called_with(safe_mode=True)
    assert manager._safe_mode is True


def test_start_runtime_process_passes_fault_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = rm.RuntimeManager("/opt/plc_main", "/tmp/x.sock", "/tmp/y.sock")
    monkeypatch.setattr(mgr, "_safe_start_log_server", MagicMock())
    monkeypatch.setattr(mgr, "_safe_connect_runtime_socket", MagicMock())
    monkeypatch.setattr(rm.time, "sleep", lambda _s: None)
    popen = MagicMock()
    monkeypatch.setattr(rm.subprocess, "Popen", popen)

    mgr._start_runtime_process(safe_mode=True, after_fault=True)
    assert popen.call_args.args[0] == ["/opt/plc_main", "--safe-mode", "--fault"]

    mgr._start_runtime_process(safe_mode=True)
    assert popen.call_args.args[0] == ["/opt/plc_main", "--safe-mode"]
