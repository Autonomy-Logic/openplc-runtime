# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Uploads carrying the pre-split EtherCAT configuration are refused before anything changes."""

import io
import json
import zipfile

import pytest


def _program_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _upload(blob: bytes) -> dict:
    from webserver import app as app_module

    data = {"file": (io.BytesIO(blob), "program.zip")}
    with app_module.app.test_request_context(
        "/api/upload-file", method="POST", data=data, content_type="multipart/form-data"
    ):
        return app_module.handle_upload_file({})


@pytest.fixture
def busconfig_calls(monkeypatch):
    from webserver import app as app_module

    calls: list = []
    monkeypatch.setattr(app_module.etherdog_manager, "apply_busconfig", calls.append)
    return calls


def test_legacy_ethercat_upload_is_refused_with_versions(busconfig_calls) -> None:
    legacy = json.dumps([{"name": "m0", "protocol": "ETHERCAT", "config": {}}])
    result = _upload(
        _program_zip({"program.st": "PROGRAM p END_PROGRAM", "conf/ethercat.json": legacy})
    )

    message = result["UploadFileFail"]
    assert "conf/ethercat.json" in message
    assert "4.3.0" in message and "OpenPLC Editor 4.3.2" in message
    assert result["CompilationStatus"] == "FAILED"
    assert busconfig_calls == []


def test_empty_legacy_file_is_not_refused(busconfig_calls, monkeypatch, quiet_upload) -> None:
    """Editors before the split always wrote an empty ethercat.json; that alone is fine."""
    from webserver import app as app_module

    runtime = _FakeRuntime(stops_on_request=True)
    runtime.state = "STOPPED"
    monkeypatch.setattr(app_module, "runtime_manager", runtime)
    result = _upload(
        _program_zip({"program.st": "PROGRAM p END_PROGRAM", "conf/ethercat.json": ""})
    )

    assert "conf/ethercat.json" not in str(result.get("UploadFileFail", ""))


class _FakeRuntime:
    """Answers STATUS from a script; records the commands it receives."""

    def __init__(self, stops_on_request: bool) -> None:
        self.state = "RUNNING"
        self.stops_on_request = stops_on_request
        self.calls: list[str] = []

    def status_plc(self) -> str:
        return f"STATUS:{self.state}\n"

    def stop_plc(self) -> str:
        self.calls.append("stop")
        if self.stops_on_request:
            self.state = "STOPPED"
        return "OK\n"


@pytest.fixture
def quiet_upload(monkeypatch):
    """Everything past the stop check that touches the device is a no-op."""
    from webserver import app as app_module

    for name in (
        "safe_extract",
        "apply_vpp_plugin_conf",
        "apply_retain_conf",
        "update_plugin_configurations",
        "run_compile",
    ):
        monkeypatch.setattr(app_module, name, lambda *a, **k: None)
    monkeypatch.setattr(app_module, "PLC_STOP_TIMEOUT_S", 0.3)
    monkeypatch.setattr(app_module.build_state, "status", app_module.BuildStatus.SUCCESS)


def test_upload_stops_a_running_plc_before_staging(monkeypatch, quiet_upload) -> None:
    from webserver import app as app_module

    runtime = _FakeRuntime(stops_on_request=True)
    monkeypatch.setattr(app_module, "runtime_manager", runtime)
    monkeypatch.setattr(
        app_module.etherdog_manager,
        "apply_busconfig",
        lambda _path: runtime.calls.append("busconfig"),
    )

    result = _upload(_program_zip({"program.st": "PROGRAM p END_PROGRAM"}))

    assert result["UploadFileFail"] == ""
    assert runtime.calls == ["stop", "busconfig"]


def test_upload_is_cancelled_when_the_plc_does_not_stop(
    monkeypatch, quiet_upload, busconfig_calls
) -> None:
    from webserver import app as app_module

    runtime = _FakeRuntime(stops_on_request=False)
    monkeypatch.setattr(app_module, "runtime_manager", runtime)

    result = _upload(_program_zip({"program.st": "PROGRAM p END_PROGRAM"}))

    assert "could not be stopped" in result["UploadFileFail"]
    assert result["CompilationStatus"] == "FAILED"
    assert busconfig_calls == []


def test_upload_with_the_plc_stopped_sends_no_stop(
    monkeypatch, quiet_upload, busconfig_calls
) -> None:
    from webserver import app as app_module

    runtime = _FakeRuntime(stops_on_request=True)
    runtime.state = "STOPPED"
    monkeypatch.setattr(app_module, "runtime_manager", runtime)

    result = _upload(_program_zip({"program.st": "PROGRAM p END_PROGRAM"}))

    assert result["UploadFileFail"] == ""
    assert runtime.calls == []
