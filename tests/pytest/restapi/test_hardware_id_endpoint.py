# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The hardware ID record: which source the runtime advertises, the endpoint, and
when the runtime computes the record itself."""

import pathlib
import subprocess

import pytest
from conftest import auth, create_user, token_for

from webserver import hardware_id

ID = "hw2:" + "a" * 64
OTHER_ID = "hw2:" + "b" * 64
RECORD = f"{ID}\nserial {'1' * 64}\nmac {'2' * 64}\ncpu {'3' * 64}\n"


@pytest.fixture()
def paths(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, pathlib.Path]:
    stored = tmp_path / "data" / "hardware-id"
    injected = tmp_path / "etc" / "hardware-id"
    stored.parent.mkdir()
    injected.parent.mkdir()
    monkeypatch.setattr(hardware_id, "STORED_PATH", stored)
    monkeypatch.setattr(hardware_id, "INJECTED_PATH", injected)
    return {"stored": stored, "injected": injected}


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
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(args)
        pathlib.Path(args[-1]).write_text(RECORD)
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(hardware_id.subprocess, "run", rec)
    monkeypatch.setattr(hardware_id.platform, "system", lambda: "Linux")
    return rec


def _host(monkeypatch: pytest.MonkeyPatch, container: bool, bootloader: bool) -> None:
    monkeypatch.setattr(hardware_id, "is_running_in_container", lambda: container)
    if bootloader:
        monkeypatch.setenv("OPENPLC_PERSISTENT_DATA_DIR", "/var/lib/openplc-runtime")
    else:
        monkeypatch.delenv("OPENPLC_PERSISTENT_DATA_DIR", raising=False)


def test_native_computes_when_missing(paths, recorder, monkeypatch) -> None:
    _host(monkeypatch, container=False, bootloader=False)
    hardware_id.ensure_stored()
    assert recorder.calls == [
        ["bash", str(hardware_id.SCRIPT_PATH), "--store", str(paths["stored"])]
    ]
    assert hardware_id.current().value == ID


def test_bootloader_container_computes_when_missing(paths, recorder, monkeypatch) -> None:
    _host(monkeypatch, container=True, bootloader=True)
    hardware_id.ensure_stored()
    assert len(recorder.calls) == 1


def test_vplc_never_computes(paths, recorder, monkeypatch) -> None:
    _host(monkeypatch, container=True, bootloader=False)
    hardware_id.ensure_stored()
    assert recorder.calls == []


def test_stored_record_is_never_recomputed(paths, recorder, monkeypatch) -> None:
    _host(monkeypatch, container=False, bootloader=False)
    paths["stored"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert recorder.calls == []


def test_injected_record_skips_computation(paths, recorder, monkeypatch) -> None:
    _host(monkeypatch, container=True, bootloader=True)
    paths["injected"].write_text(RECORD)
    hardware_id.ensure_stored()
    assert recorder.calls == []


def test_non_linux_never_computes(paths, recorder, monkeypatch) -> None:
    _host(monkeypatch, container=False, bootloader=False)
    monkeypatch.setattr(hardware_id.platform, "system", lambda: "Windows")
    hardware_id.ensure_stored()
    assert recorder.calls == []
