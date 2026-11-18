# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The hardware ID record: which source the runtime advertises, the endpoint, and
when the runtime computes the record itself."""

import json
import os
import pathlib
import subprocess

import pytest
from conftest import auth, create_user, token_for

from webserver import hardware_id

ID = "hw2:" + "a" * 64
OTHER_ID = "hw2:" + "b" * 64
VECTORS_PATH = pathlib.Path(__file__).resolve().parents[1] / "hardware_id" / "vectors.json"
RECORD = f"{ID}\nserial {'1' * 64}\nmac {'2' * 64}\ncpu {'3' * 64}\n"


FULL_CAPS = "000001ffffffffff"
DOCKER_DEFAULT_CAPS = "00000000a80425fb"


@pytest.fixture()
def paths(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, pathlib.Path]:
    """Record paths plus fake /proc and /sys; the default view is native Linux."""
    stored = tmp_path / "data" / "hardware-id"
    injected = tmp_path / "etc" / "hardware-id"
    proc = tmp_path / "proc"
    sys_dir = tmp_path / "sys"
    for directory in (stored.parent, injected.parent, proc, sys_dir):
        directory.mkdir()
    monkeypatch.setattr(hardware_id, "STORED_PATH", stored)
    monkeypatch.setattr(hardware_id, "INJECTED_PATH", injected)
    monkeypatch.setattr(hardware_id, "PROC_DIR", proc)
    monkeypatch.setattr(hardware_id, "SYS_DIR", sys_dir)
    monkeypatch.setattr(hardware_id.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware_id, "is_running_in_container", lambda: False)
    return {"stored": stored, "injected": injected, "proc": proc, "sys": sys_dir}


def _container(
    monkeypatch: pytest.MonkeyPatch,
    paths: dict[str, pathlib.Path],
    caps: str = FULL_CAPS,
    physical_nic: bool = True,
    bootloader: bool = True,
) -> None:
    """A container with bounding set ``caps`` that sees eth0, device-backed or not, started by the bootloader or not."""
    monkeypatch.setattr(hardware_id, "is_running_in_container", lambda: True)
    if bootloader:
        monkeypatch.setenv(hardware_id.BOOTLOADER_CONTAINER_ENV, "/var/lib/openplc-runtime")
    else:
        monkeypatch.delenv(hardware_id.BOOTLOADER_CONTAINER_ENV, raising=False)
    kernel = paths["proc"] / "sys" / "kernel"
    kernel.mkdir(parents=True)
    (kernel / "cap_last_cap").write_text("40\n")
    (paths["proc"] / "self").mkdir()
    (paths["proc"] / "self" / "status").write_text(f"Name:\tpython3\nCapBnd:\t{caps}\n")
    nic = paths["sys"] / "class" / "net" / "eth0"
    nic.mkdir(parents=True)
    if physical_nic:
        (nic / "device").mkdir()


def test_parse_record() -> None:
    record = hardware_id.parse_record(RECORD, "device")
    assert record is not None
    assert record.value == ID
    assert [e.kind for e in record.elements] == ["serial", "mac", "cpu"]


@pytest.mark.parametrize(
    "text",
    ["", "hw2:abc\n", ID.upper() + "\n", f"{ID}\nwifi {'1' * 64}\n", f"{ID}\nmac 12\n"],
)
def test_malformed_records_are_ignored(text: str) -> None:
    assert hardware_id.parse_record(text, "device") is None


def test_stored_record_is_advertised(paths: dict[str, pathlib.Path]) -> None:
    paths["stored"].write_text(RECORD)
    record = hardware_id.current()
    assert record is not None and record.value == ID and record.source == "device"


def test_injected_record_wins(paths: dict[str, pathlib.Path]) -> None:
    paths["stored"].write_text(RECORD)
    paths["injected"].write_text(RECORD.replace(ID, OTHER_ID))
    record = hardware_id.current()
    assert record is not None and record.value == OTHER_ID and record.source == "orchestrator"


def test_no_record_means_none(paths: dict[str, pathlib.Path]) -> None:
    assert hardware_id.current() is None


def test_endpoint_requires_login(client, paths) -> None:
    assert client.get("/api/hardware-id").status_code == 401


def test_endpoint_returns_the_record(client, admin_token, paths) -> None:
    paths["stored"].write_text(RECORD)
    body = client.get("/api/hardware-id", headers=auth(admin_token)).get_json()
    assert body == {
        "hardwareId": ID,
        "source": "device",
        "elements": [
            {"kind": "serial", "hash": "1" * 64},
            {"kind": "mac", "hash": "2" * 64},
            {"kind": "cpu", "hash": "3" * 64},
        ],
    }


def test_endpoint_open_to_non_admin_users(client, admin_token, paths) -> None:
    paths["stored"].write_text(RECORD)
    assert (
        create_user(client, "operator", "operator-pass", admin_token, role="user").status_code
        == 201
    )
    token = token_for(client, "operator", "operator-pass")
    assert client.get("/api/hardware-id", headers=auth(token)).get_json()["hardwareId"] == ID


def test_endpoint_without_an_id(client, admin_token, paths) -> None:
    body = client.get("/api/hardware-id", headers=auth(admin_token)).get_json()
    assert body == {"hardwareId": None, "source": None, "elements": []}


class _Recorder:
    def __init__(self, record: str = RECORD, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.record = record
        self.code = code

    def __call__(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if self.code == 0:
            pathlib.Path(args[-1]).write_text(self.record)
        return subprocess.CompletedProcess(args, self.code, "", "")


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(hardware_id.subprocess, "run", rec)
    return rec


def test_native_computes_when_missing(paths, recorder) -> None:
    hardware_id.ensure_stored()
    assert recorder.calls == [
        ["bash", str(hardware_id.SCRIPT_PATH), "--store", str(paths["stored"])]
    ]
    assert hardware_id.current().value == ID


def test_stored_record_is_recomputed_on_every_start(paths, recorder) -> None:
    paths["stored"].write_text(RECORD)
    hardware_id.ensure_stored()
    hardware_id.ensure_stored()
    assert len(recorder.calls) == 2
    assert hardware_id.current().value == ID


def test_cloned_record_is_replaced(paths, recorder) -> None:
    paths["stored"].write_text(RECORD.replace(ID, OTHER_ID))
    hardware_id.ensure_stored()
    assert hardware_id.current().value == ID


@pytest.mark.parametrize("caps", [FULL_CAPS, "0000003fffffffff"], ids=["all", "up-to-audit-read"])
def test_privileged_host_network_container_computes(paths, recorder, monkeypatch, caps) -> None:
    _container(monkeypatch, paths, caps=caps)
    hardware_id.ensure_stored()
    assert len(recorder.calls) == 1
    assert hardware_id.current().value == ID


@pytest.mark.parametrize(
    "caps,physical_nic",
    [(DOCKER_DEFAULT_CAPS, True), (FULL_CAPS, False), (DOCKER_DEFAULT_CAPS, False)],
    ids=["unprivileged-host-network", "privileged-bridged", "unprivileged-bridged"],
)
def test_container_without_host_view_has_no_id(
    paths, recorder, monkeypatch, caps: str, physical_nic: bool
) -> None:
    _container(monkeypatch, paths, caps=caps, physical_nic=physical_nic)
    paths["stored"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert recorder.calls == []
    assert hardware_id.current() is None


def test_privileged_host_network_container_not_started_by_the_bootloader_has_no_id(
    paths, recorder, monkeypatch
) -> None:
    _container(monkeypatch, paths, bootloader=False)
    paths["stored"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert recorder.calls == []
    assert hardware_id.current() is None


def test_container_without_capability_data_has_no_id(paths, recorder, monkeypatch) -> None:
    monkeypatch.setattr(hardware_id, "is_running_in_container", lambda: True)
    paths["stored"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert recorder.calls == []
    assert hardware_id.current() is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")
def test_unreadable_dmi_means_no_id(paths, recorder) -> None:
    dmi = paths["sys"] / "class" / "dmi" / "id"
    dmi.mkdir(parents=True)
    (dmi / "product_uuid").write_text("uuid\n")
    (dmi / "product_uuid").chmod(0o000)
    paths["stored"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert recorder.calls == []
    assert hardware_id.current() is None


def test_vplc_never_computes(paths, recorder, monkeypatch) -> None:
    _container(monkeypatch, paths, caps=DOCKER_DEFAULT_CAPS, physical_nic=False)
    paths["injected"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert recorder.calls == []
    assert hardware_id.current().source == "orchestrator"


def test_injected_record_skips_computation(paths, recorder, monkeypatch) -> None:
    _container(monkeypatch, paths)
    paths["injected"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert recorder.calls == []


def test_windows_has_no_id(paths, recorder, monkeypatch) -> None:
    monkeypatch.setattr(hardware_id.platform, "system", lambda: "Windows")
    paths["stored"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert recorder.calls == []
    assert hardware_id.current() is None


def test_windows_endpoint_reports_no_id(client, admin_token, paths, monkeypatch) -> None:
    monkeypatch.setattr(hardware_id.platform, "system", lambda: "MINGW64_NT-10.0")
    body = client.get("/api/hardware-id", headers=auth(admin_token)).get_json()
    assert body == {"hardwareId": None, "source": None, "elements": []}


def _vector_root(root: pathlib.Path, name: str) -> pathlib.Path:
    """Builds the fake hardware tree of vector ``name`` under ``root``."""
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    vec = next(v for v in vectors if v["name"] == name)
    for rel, content in vec.get("files", {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for rel, target in vec.get("links", {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    return root


def _vector_id(name: str) -> str:
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    return next(v["id"] for v in vectors if v["name"] == name)


def test_reader_replaces_a_cloned_record(paths, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENPLC_HWID_ROOT", str(_vector_root(tmp_path / "hw", "raspberry-pi")))
    paths["stored"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert hardware_id.current().value == _vector_id("raspberry-pi")


def test_reader_rewrites_a_malformed_record(paths, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENPLC_HWID_ROOT", str(_vector_root(tmp_path / "hw", "raspberry-pi")))
    paths["stored"].write_text("not a record\n")
    hardware_id.ensure_stored()
    assert hardware_id.current().value == _vector_id("raspberry-pi")


def test_reader_keeps_the_record_when_nothing_is_computable(paths, tmp_path, monkeypatch) -> None:
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("OPENPLC_HWID_ROOT", str(tmp_path / "empty"))
    paths["stored"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert paths["stored"].read_text() == RECORD
    assert hardware_id.current().value == ID


def test_reader_without_id_or_record_stores_nothing(paths, tmp_path, monkeypatch) -> None:
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("OPENPLC_HWID_ROOT", str(tmp_path / "empty"))
    paths["stored"].write_text("garbage\n")
    hardware_id.ensure_stored()
    assert hardware_id.current() is None


@pytest.mark.parametrize(
    "text",
    [
        RECORD,
        f"  {ID} \r\n\n mac {'2' * 64}\n",
        f"{ID}\n",
        "",
        "\n\n",
        "hw2:abc\n",
        ID.upper() + "\n",
        f"{ID}\nwifi {'1' * 64}\n",
        f"{ID}\nmac 12\n",
        f"mac {'2' * 64}\n{ID}\n",
    ],
)
def test_shell_and_python_agree_on_well_formed_records(tmp_path: pathlib.Path, text: str) -> None:
    target = tmp_path / "hardware-id"
    target.write_text(text)
    script = f'set -euo pipefail; source "{hardware_id.SCRIPT_PATH}"; hw2_record_ok "{target}"'
    shell_ok = subprocess.run(["bash", "-c", script], check=False).returncode == 0
    assert shell_ok == (hardware_id.parse_record(text, "device") is not None)
