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
    result = _upload(_program_zip({"program.st": "PROGRAM p END_PROGRAM", "conf/ethercat.json": legacy}))

    message = result["UploadFileFail"]
    assert "conf/ethercat.json" in message
    assert "4.3.0" in message and "OpenPLC Editor 4.3.2" in message
    assert result["CompilationStatus"] == "FAILED"
    assert busconfig_calls == []


def test_empty_legacy_file_is_not_refused(busconfig_calls, monkeypatch) -> None:
    """Editors before the split always wrote an empty ethercat.json; that alone is fine."""
    from webserver import app as app_module

    monkeypatch.setattr(app_module, "safe_extract", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "apply_vpp_plugin_conf", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "apply_retain_conf", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "update_plugin_configurations", lambda *a, **k: None)
    result = _upload(_program_zip({"program.st": "PROGRAM p END_PROGRAM", "conf/ethercat.json": ""}))

    assert "conf/ethercat.json" not in str(result.get("UploadFileFail", ""))
