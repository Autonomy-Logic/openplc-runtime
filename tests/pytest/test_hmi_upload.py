# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The hmi/ folder of an upload: manifest, whitelists, keep and remove (webserver/hmi_upload.py)."""

import io
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from webserver import hmi_upload
from webserver.hmi_upload import HmiAction, HmiPlan, HmiUploadError, classify
from webserver.plcapp_management import DISALLOWED_EXT, analyze_zip, extracted_names

INSTALL = json.dumps({"format": 2}).encode()
REMOVE = json.dumps({"format": 2, "remove": True}).encode()
SRC = ["hmi/src/hmi_access.h", "hmi/src/hmi_access.c"]


def _zip(files: dict[str, bytes | str]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    buf.seek(0)
    return buf


# ---- manifest -----------------------------------------------------------------------------


def test_install_manifest() -> None:
    plan = classify(["program.cpp", "hmi/manifest.json", *SRC, "hmi/client/index.html"], INSTALL)
    assert plan == HmiPlan(HmiAction.INSTALL, src_files=2, client_files=1)


def test_remove_manifest_alone() -> None:
    assert classify(["program.cpp", "hmi/manifest.json"], REMOVE).action is HmiAction.REMOVE


def test_no_hmi_keeps() -> None:
    assert classify(["program.cpp", "conf/x.json"], None).action is HmiAction.KEEP


def test_directory_entries_are_ignored() -> None:
    assert classify(["hmi/", "hmi/src/"], None).action is HmiAction.KEEP


@pytest.mark.parametrize(
    "manifest",
    [
        b'{"format": 1}',
        b'{"format": 3}',
        b'{"format": "2"}',
        b'{"format": true}',
        b'{"format": 2.0}',
        b'{"format": 2, "remove": false}',
        b'{"format": 2, "remove": 1}',
        b'{"format": 2, "extra": 0}',
        b'{"remove": true}',
        b"[1]",
        b"not json",
        b"\xff\xfe",
        b'{"format": 2' + b" " * 2000 + b"}",
    ],
)
def test_bad_manifest_is_refused(manifest: bytes) -> None:
    with pytest.raises(HmiUploadError):
        classify(["hmi/manifest.json", *SRC], manifest)


def test_an_hmi_built_for_screens_run_on_the_device_is_refused_by_name() -> None:
    with pytest.raises(HmiUploadError, match="format must be 2"):
        classify(["hmi/manifest.json", *SRC], b'{"format": 1}')


def test_hmi_without_manifest_is_refused() -> None:
    with pytest.raises(HmiUploadError, match="manifest"):
        classify(SRC, None)


def test_remove_with_other_files_is_refused() -> None:
    with pytest.raises(HmiUploadError, match="removal"):
        classify(["hmi/manifest.json", "hmi/client/index.html"], REMOVE)


def test_install_without_sources_is_refused() -> None:
    with pytest.raises(HmiUploadError, match="no sources"):
        classify(["hmi/manifest.json", "hmi/client/index.html"], INSTALL)


# ---- whitelists ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "hmi/client/index.html",
        "hmi/client/hmi.js",
        "hmi/client/hmi.wasm",
        "hmi/client/style.css",
        "hmi/client/LICENSE.txt",
        "hmi/client/OpenPLCSans-Regular.ttf",
        "hmi/client/a-b_c.min.js",
        "hmi/client/" + "x" * 61 + ".js",
    ],
)
def test_allowed_client_files(name: str) -> None:
    assert classify(["hmi/manifest.json", *SRC, name], INSTALL).client_files == 1


@pytest.mark.parametrize(
    "name",
    [
        "hmi/client/sub/app.js",
        "hmi/client/app.exe",
        "hmi/client/app.php",
        "hmi/client/app.svg",
        "hmi/client/my app.js",
        "hmi/client/" + "x" * 62 + ".js",
        "hmi/client/café.js",
        "hmi/client/app.JS",
        "hmi/client/Index.HTML",
        "hmi/client/style.Css",
        "hmi/src/notes.md",
        "hmi/src/unit.cpp",
        "hmi/src/unit.hpp",
        "hmi/src/core/hmi_core.c",
        "hmi/src/ACCESS.C",
        "hmi/src/build.py",
        "hmi/src/sub dir/a.c",
        "hmi/src/core/../a.c",
        "hmi/src/app.js",
        "hmi/other.txt",
        "hmi/manifest.json.bak",
        "hmi",
    ],
)
def test_refused_hmi_entries(name: str) -> None:
    with pytest.raises(HmiUploadError):
        classify(["hmi/manifest.json", *SRC, name], INSTALL)


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("hmi/client/app.js", True),
        ("hmi/client/index.html", True),
        ("hmi/client/hmi.wasm", True),
        ("hmi/client/app.JS", False),
        ("app.js", False),
        ("web/index.html", False),
        ("lib/x.wasm", False),
        ("hmi/src/app.js", False),
        ("hmi/client/sub/app.js", False),
        ("hmi/client/setup.sh", False),
        ("conf/setup.sh", False),
        ("program.cpp", True),
        ("conf/modbus.json", True),
    ],
)
def test_extension_rule(name: str, allowed: bool) -> None:
    assert hmi_upload.extension_allowed(name, DISALLOWED_EXT) is allowed


# ---- analyze_zip and extracted names ------------------------------------------------------


def test_analyze_zip_accepts_client_scripts_only_in_hmi_client() -> None:
    files = {
        "program.cpp": "",
        "hmi/manifest.json": INSTALL,
        "hmi/src/u.c": "",
        "hmi/client/hmi.js": "",
        "hmi/client/hmi.wasm": b"\0asm",
        "hmi/client/index.html": "",
    }
    safe, _ = analyze_zip(_zip(files))
    assert safe


@pytest.mark.parametrize("name", ["tools/app.js", "page.html", "x/mod.wasm"])
def test_analyze_zip_refuses_client_types_elsewhere(name: str) -> None:
    safe, _ = analyze_zip(_zip({"program.cpp": "", name: ""}))
    assert not safe


def test_single_root_folder_is_stripped_before_the_rules() -> None:
    blob = _zip(
        {
            "proj/program.cpp": "",
            "proj/hmi/manifest.json": INSTALL,
            "proj/hmi/src/u.c": "",
            "proj/hmi/client/hmi.js": "",
        }
    )
    safe, valid = analyze_zip(blob)
    assert safe
    names = [n for _, n in extracted_names(valid)]
    assert "hmi/client/hmi.js" in names
    assert hmi_upload.plan_from_zip(blob, extracted_names(valid)).action is HmiAction.INSTALL


def test_plan_from_zip_reads_the_manifest() -> None:
    blob = _zip({"program.cpp": "", "hmi/manifest.json": REMOVE})
    _, valid = analyze_zip(blob)
    assert hmi_upload.plan_from_zip(blob, extracted_names(valid)).action is HmiAction.REMOVE


# ---- keep and remove ----------------------------------------------------------------------


def _install(generated: Path, marker: str) -> None:
    (generated / "hmi" / "src").mkdir(parents=True)
    (generated / "hmi" / "src" / "u.c").write_text(marker)


def _replace_generated(generated: Path, with_hmi: str | None = None) -> None:
    """What the upload does inside keep_previous: wipe and extract."""
    shutil.rmtree(generated)
    generated.mkdir()
    (generated / "program.cpp").write_text("new")
    if with_hmi is not None:
        _install(generated, with_hmi)


def test_upload_without_hmi_keeps_the_previous_one(tmp_path: Path) -> None:
    generated = tmp_path / "core" / "generated"
    generated.mkdir(parents=True)
    _install(generated, "old")
    with hmi_upload.keep_previous(str(generated), HmiPlan(HmiAction.KEEP)):
        _replace_generated(generated)
    assert (generated / "hmi" / "src" / "u.c").read_text() == "old"
    assert (generated / "program.cpp").read_text() == "new"
    assert not (tmp_path / "core" / hmi_upload.STASH_NAME).exists()


def test_upload_with_hmi_replaces_the_previous_one(tmp_path: Path) -> None:
    generated = tmp_path / "core" / "generated"
    generated.mkdir(parents=True)
    _install(generated, "old")
    plan = HmiPlan(HmiAction.INSTALL, src_files=1)
    with hmi_upload.keep_previous(str(generated), plan):
        _replace_generated(generated, with_hmi="new")
    hmi_upload.apply(str(generated), plan, lambda _m: None)
    assert (generated / "hmi" / "src" / "u.c").read_text() == "new"


def test_remove_form_deletes_the_hmi(tmp_path: Path) -> None:
    generated = tmp_path / "core" / "generated"
    generated.mkdir(parents=True)
    _install(generated, "old")
    plan = HmiPlan(HmiAction.REMOVE)
    with hmi_upload.keep_previous(str(generated), plan):
        _replace_generated(generated)
        (generated / "hmi").mkdir()
        (generated / "hmi" / "manifest.json").write_bytes(REMOVE)
    logs: list[str] = []
    hmi_upload.apply(str(generated), plan, logs.append)
    assert not (generated / "hmi").exists()
    assert any("removed" in m for m in logs)


def test_previous_hmi_survives_a_failed_extract(tmp_path: Path) -> None:
    generated = tmp_path / "core" / "generated"
    generated.mkdir(parents=True)
    _install(generated, "old")
    with (
        pytest.raises(OSError),
        hmi_upload.keep_previous(str(generated), HmiPlan(HmiAction.KEEP)),
    ):
        shutil.rmtree(generated)
        raise OSError("disk full")
    assert (generated / "hmi" / "src" / "u.c").read_text() == "old"


def test_stale_stash_is_discarded(tmp_path: Path) -> None:
    generated = tmp_path / "core" / "generated"
    generated.mkdir(parents=True)
    stale = tmp_path / "core" / hmi_upload.STASH_NAME
    stale.mkdir()
    (stale / "junk").write_text("x")
    with hmi_upload.keep_previous(str(generated), HmiPlan(HmiAction.KEEP)):
        _replace_generated(generated)
    assert not stale.exists()
    assert not (generated / "hmi").exists()
    assert os.listdir(generated) == ["program.cpp"]
