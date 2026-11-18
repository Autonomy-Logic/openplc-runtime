# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""/api/upload-file with an hmi/ folder: installed, kept, removed, or refused before any change."""

import io
import json
import zipfile
from pathlib import Path

import pytest

INSTALL = json.dumps({"format": 2})
REMOVE = json.dumps({"format": 2, "remove": True})


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _upload(blob: bytes) -> dict:
    from webserver import app as app_module

    # run_compile is stubbed, so the previous upload left the status at COMPILING.
    app_module.build_state.status = app_module.BuildStatus.SUCCESS
    data = {"file": (io.BytesIO(blob), "program.zip")}
    with app_module.app.test_request_context(
        "/api/upload-file", method="POST", data=data, content_type="multipart/form-data"
    ):
        return app_module.handle_upload_file({})


class _StoppedRuntime:
    def status_plc(self) -> str:
        return "STATUS:STOPPED\n"

    def stop_plc(self) -> str:
        return "OK\n"


@pytest.fixture
def runtime_root(tmp_path: Path, monkeypatch) -> Path:
    """A scratch runtime root: real extraction, everything that builds or configures stubbed."""
    from webserver import app as app_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_module, "runtime_manager", _StoppedRuntime())
    for name in (
        "apply_vpp_plugin_conf",
        "apply_retain_conf",
        "apply_image_conf",
        "update_plugin_configurations",
        "run_compile",
        "stage_project_snapshot",
    ):
        monkeypatch.setattr(app_module, name, lambda *a, **k: None)
    monkeypatch.setattr(app_module.etherdog_manager, "apply_busconfig", lambda _p: None)
    monkeypatch.setattr(app_module.project_snapshot, "clear", lambda: None)
    monkeypatch.setattr(app_module.build_state, "status", app_module.BuildStatus.SUCCESS)
    return tmp_path


def _program(extra: dict[str, str] | None = None) -> bytes:
    files = {"program.cpp": "// program", "generated.hpp": ""}
    files.update(extra or {})
    return _zip(files)


HMI = {
    "hmi/manifest.json": INSTALL,
    "hmi/src/hmi_access.h": "",
    "hmi/src/hmi_access.c": "// unit v1",
    "hmi/client/index.html": "<html>",
    "hmi/client/hmi.js": "export {};",
}


def test_install_keep_and_remove(runtime_root: Path) -> None:
    hmi = runtime_root / "core" / "generated" / "hmi"

    assert _upload(_program(HMI))["UploadFileFail"] == ""
    assert (hmi / "src" / "hmi_access.c").read_text() == "// unit v1"
    assert (hmi / "client" / "hmi.js").exists()

    assert _upload(_program())["UploadFileFail"] == ""
    assert (hmi / "src" / "hmi_access.c").read_text() == "// unit v1", "kept without hmi/"
    assert (runtime_root / "core" / "generated" / "program.cpp").exists()

    v2 = dict(HMI, **{"hmi/src/hmi_access.c": "// unit v2"})
    del v2["hmi/client/hmi.js"]
    assert _upload(_program(v2))["UploadFileFail"] == ""
    assert (hmi / "src" / "hmi_access.c").read_text() == "// unit v2", "replaced"
    assert not (hmi / "client" / "hmi.js").exists(), "nothing of the old HMI survives"

    assert _upload(_program({"hmi/manifest.json": REMOVE}))["UploadFileFail"] == ""
    assert not hmi.exists(), "removed"


@pytest.mark.parametrize(
    "files",
    [
        {"hmi/manifest.json": json.dumps({"format": 1}), "hmi/src/u.c": ""},
        {"hmi/manifest.json": INSTALL, "hmi/src/unit.cpp": ""},
        {"hmi/src/u.c": ""},
        {"hmi/manifest.json": INSTALL, "hmi/src/u.c": "", "hmi/client/sub/x.js": ""},
        {"hmi/manifest.json": REMOVE, "hmi/src/u.c": ""},
    ],
)
def test_bad_hmi_is_refused_before_anything_changes(runtime_root: Path, files: dict) -> None:
    assert _upload(_program(HMI))["UploadFileFail"] == ""
    result = _upload(_program(files))
    assert (
        result["UploadFileFail"].startswith("HMI: ") or "safety checks" in result["UploadFileFail"]
    )
    assert result["CompilationStatus"] == "FAILED"
    unit = runtime_root / "core" / "generated" / "hmi" / "src" / "hmi_access.c"
    assert unit.read_text() == "// unit v1", "the installed HMI is untouched"


def test_script_outside_the_client_is_refused(runtime_root: Path) -> None:
    result = _upload(_program({"tools/run.js": ""}))
    assert "safety checks" in result["UploadFileFail"]
