# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Runtime licences: shape, storage, the report GET /api/runtime-license returns,
and the upload endpoint. The runtime checks no signature."""

import base64
import io
import json
import pathlib

import pytest
from conftest import auth, create_user, token_for

from webserver import hardware_id, runtime_license

ID = "hw2:" + "a" * 64
OTHER_ID = "hw2:" + "b" * 64
RECORD = f"{ID}\nserial {'1' * 64}\n"
# Well formed but signed by nobody: the runtime must store and report it unchanged.
UNSIGNED = base64.b64encode(b"\x01" * 64).decode("ascii")


def make_license(
    hardware: str = ID,
    order_id: str = "ord_1",
    key_id: int = 0,
    seats: int = 2,
) -> dict[str, object]:
    return {
        "fmt": 1,
        "product": runtime_license.PRODUCT,
        "hardware_id": hardware,
        "seats": seats,
        "order_id": order_id,
        "issued_at": "2026-10-28T12:00:00.000Z",
        "key_id": key_id,
        "sig": UNSIGNED,
    }


def as_bytes(data: dict[str, object]) -> bytes:
    return json.dumps(data).encode()


@pytest.fixture()
def env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, pathlib.Path]:
    paths = {
        "stored": tmp_path / "data" / "hardware-id",
        "injected": tmp_path / "etc" / "hardware-id",
        "seat": tmp_path / "etc" / "runtime.license",
        "device_dir": tmp_path / "data" / "licenses",
        "vplc_dir": tmp_path / "var" / "licenses",
    }
    for key in ("stored", "injected"):
        paths[key].parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hardware_id, "STORED_PATH", paths["stored"])
    monkeypatch.setattr(hardware_id, "INJECTED_PATH", paths["injected"])
    monkeypatch.setattr(runtime_license, "SEAT_FILE_PATH", paths["seat"])
    monkeypatch.setattr(runtime_license, "DEVICE_LICENSES_DIR", paths["device_dir"])
    monkeypatch.setattr(runtime_license, "VPLC_LICENSES_DIR", paths["vplc_dir"])
    return paths


def on_device(paths: dict[str, pathlib.Path]) -> None:
    paths["stored"].write_text(RECORD)


def in_vplc(paths: dict[str, pathlib.Path]) -> None:
    paths["injected"].write_text(RECORD)


def seat(paths: dict[str, pathlib.Path], content: bytes) -> None:
    paths["seat"].write_bytes(content)


def seat_file(**kwargs: object) -> bytes:
    return json.dumps({"fmt": 1, "vplc": "vplc-1", "license": make_license(**kwargs)}).encode()


# --- shape ----------------------------------------------------------------


def test_parse_accepts_an_edge_licence() -> None:
    lic = runtime_license.parse_license(make_license())
    assert lic is not None
    assert (lic.hardware_id, lic.order_id, lic.key_id) == (ID, "ord_1", 0)
    assert len(lic.signature) == 64


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"fmt": 2},
        {"product": "com.autonomy.openplc-runtime-baremetal"},
        {"hardware_id": "hw2:abc"},
        {"seats": 0},
        {"seats": True},
        {"seats": 1001},
        {"order_id": "../escape"},
        {"order_id": 7},
        {"issued_at": ""},
        {"key_id": -1},
        {"key_id": 256},
        {"key_id": False},
        {"fmt": True},
        {"fmt": 1.0},
        {"sig": "short"},
    ],
)
def test_parse_refuses_other_shapes(change) -> None:
    assert runtime_license.parse_license({**make_license(), **change}) is None


@pytest.mark.parametrize("data", [None, [], "x"])
def test_parse_refuses_non_objects(data) -> None:
    assert runtime_license.parse_license(data) is None


@pytest.mark.parametrize("raw", [b"\xff", b"{", b"[]"])
def test_parse_bytes_refuses_non_json(raw) -> None:
    assert runtime_license.parse_license_bytes(raw) is None


def test_to_bytes_round_trips() -> None:
    data = make_license()
    assert json.loads(runtime_license.parse_license(data).to_bytes()) == data


def test_no_verifier_is_left() -> None:
    for name in ("TRUSTED_KEYS", "DEV_KEY_ENV", "verify_signature", "verifies_for", "status"):
        assert not hasattr(runtime_license, name)


# --- storage ----------------------------------------------------------------


def test_store_keeps_a_device_licence_unverified(env) -> None:
    on_device(env)
    data = make_license()
    result = runtime_license.store(as_bytes(data))
    assert result == runtime_license.StoreResult(True, order_id="ord_1")
    assert json.loads((env["device_dir"] / "ord_1.license").read_bytes()) == data


def test_store_keeps_a_licence_for_another_hardware_id(env) -> None:
    on_device(env)
    assert runtime_license.store(as_bytes(make_license(hardware=OTHER_ID))).ok
    assert (env["device_dir"] / "ord_1.license").exists()


def test_store_in_a_vplc_uses_the_agent_folder(env) -> None:
    in_vplc(env)
    assert runtime_license.store(as_bytes(make_license())).ok
    assert (env["vplc_dir"] / "ord_1.license").exists()
    assert not env["device_dir"].exists()
    assert runtime_license.licenses_dir() == env["vplc_dir"]


def test_licenses_dir_on_a_device(env) -> None:
    on_device(env)
    assert runtime_license.licenses_dir() == env["device_dir"]


@pytest.mark.parametrize(
    ("setup", "raw", "error"),
    [
        (on_device, b"x" * 4097, "larger than 4096"),
        (on_device, b"{}", "Not an OpenPLC Runtime licence"),
        (lambda p: None, as_bytes(make_license()), "no hardware ID"),
    ],
)
def test_store_refuses(env, setup, raw, error) -> None:
    setup(env)
    result = runtime_license.store(raw)
    assert not result.ok
    assert error in result.error
    assert not result.internal
    assert not env["device_dir"].exists()


def test_store_reports_a_write_failure(env, monkeypatch) -> None:
    on_device(env)

    def fail(_path: str, _blob: bytes) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(runtime_license, "_write_license_atomically", fail)
    result = runtime_license.store(as_bytes(make_license()))
    assert not result.ok and result.internal


# --- report -----------------------------------------------------------------


def test_report_without_a_hardware_id(env) -> None:
    assert runtime_license.report() == {"hardwareId": None, "source": None, "licenses": []}


def test_report_on_a_device_lists_every_stored_licence(env) -> None:
    on_device(env)
    first = make_license(order_id="ord_1")
    foreign = make_license(order_id="ord_2", hardware=OTHER_ID)
    assert runtime_license.store(as_bytes(first)).ok
    assert runtime_license.store(as_bytes(foreign)).ok
    assert runtime_license.report() == {
        "hardwareId": ID,
        "source": "device",
        "licenses": [first, foreign],
    }


def test_report_skips_unusable_files(env) -> None:
    on_device(env)
    env["device_dir"].mkdir(parents=True)
    (env["device_dir"] / "notes.txt").write_bytes(as_bytes(make_license()))
    (env["device_dir"] / "bad.license").write_bytes(b"{")
    (env["device_dir"] / "big.license").write_bytes(b" " * 5000)
    assert runtime_license.report()["licenses"] == []


def test_report_lists_this_device_first_within_the_cap(env, monkeypatch) -> None:
    on_device(env)
    mine = make_license(order_id="zzz_mine")
    assert runtime_license.store(as_bytes(mine)).ok
    for i in range(3):
        assert runtime_license.store(as_bytes(make_license(order_id=f"a{i}", hardware=OTHER_ID))).ok
    monkeypatch.setattr(runtime_license, "MAX_STORED_LICENSES", 2)
    listed = runtime_license.report()["licenses"]
    assert len(listed) == 2
    assert listed[0] == mine


def test_report_reads_the_hardware_id_once(env, monkeypatch) -> None:
    on_device(env)
    calls: list[int] = []
    real = hardware_id.current

    def counted() -> hardware_id.HardwareId | None:
        calls.append(1)
        return real()

    monkeypatch.setattr(hardware_id, "current", counted)
    runtime_license.report()
    assert len(calls) == 1


def test_report_in_a_vplc_lists_only_the_seat_licence(env) -> None:
    in_vplc(env)
    assert runtime_license.store(as_bytes(make_license(order_id="host_only"))).ok
    assert runtime_license.report() == {"hardwareId": ID, "source": "orchestrator", "licenses": []}
    seat(env, seat_file(order_id="ord_seat"))
    assert runtime_license.report()["licenses"] == [make_license(order_id="ord_seat")]


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b"\xff",
        json.dumps({"fmt": 2, "vplc": "v", "license": make_license()}).encode(),
        json.dumps({"fmt": 1, "vplc": "", "license": make_license()}).encode(),
        json.dumps({"fmt": 1, "vplc": "v", "license": {}}).encode(),
        json.dumps([1]).encode(),
        pytest.param(b" " * 9000, id="too-large"),
    ],
)
def test_report_in_a_vplc_skips_a_malformed_seat_file(env, content) -> None:
    in_vplc(env)
    seat(env, content)
    assert runtime_license.report()["licenses"] == []


# --- endpoints ----------------------------------------------------------------


@pytest.fixture()
def user_token(client) -> str:
    assert create_user(client, "admin", "admin-pass-123").status_code == 201
    return token_for(client, "admin", "admin-pass-123")


def test_endpoints_need_a_login(client, env) -> None:
    assert client.post("/api/runtime-license", data=b"{}").status_code == 401
    assert client.get("/api/runtime-license").status_code == 401


def test_upload_then_read_back(client, env, user_token) -> None:
    on_device(env)
    data = make_license()
    resp = client.post(
        "/api/runtime-license",
        data=as_bytes(data),
        headers=auth(user_token),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == {"orderId": "ord_1"}
    resp = client.get("/api/runtime-license", headers=auth(user_token))
    assert resp.status_code == 200
    assert resp.get_json() == {"hardwareId": ID, "source": "device", "licenses": [data]}


def test_upload_refuses_another_shape(client, env, user_token) -> None:
    on_device(env)
    resp = client.post(
        "/api/runtime-license",
        data=as_bytes({**make_license(), "seats": 0}),
        headers=auth(user_token),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "Not an OpenPLC Runtime licence" in resp.get_json()["error"]


def test_upload_refuses_a_large_body(client, env, user_token) -> None:
    resp = client.post("/api/runtime-license", data=b"x" * 5000, headers=auth(user_token))
    assert resp.status_code == 413


def test_upload_refuses_a_large_streamed_body(client, env, user_token) -> None:
    resp = client.post(
        "/api/runtime-license",
        input_stream=io.BytesIO(b"x" * 5000),
        headers={**auth(user_token), "Transfer-Encoding": "chunked"},
        environ_overrides={"wsgi.input_terminated": True},
    )
    assert resp.status_code == 413


def test_upload_reports_a_write_failure(client, env, user_token, monkeypatch) -> None:
    on_device(env)

    def fail(_path: str, _blob: bytes) -> None:
        raise OSError("full")

    monkeypatch.setattr(runtime_license, "_write_license_atomically", fail)
    resp = client.post(
        "/api/runtime-license", data=as_bytes(make_license()), headers=auth(user_token)
    )
    assert resp.status_code == 500


# --- /api/status ---------------------------------------------------------------


class FakeRuntime:
    def __init__(self, status: str | None) -> None:
        self._status = status

    def status_plc(self) -> str | None:
        return self._status

    def switch_plc(self) -> None:
        return None


@pytest.mark.parametrize("plc_status", ["STATUS:RUNNING", None])
def test_status_no_longer_reports_a_licence(env, monkeypatch, plc_status) -> None:
    from webserver import app as app_module

    monkeypatch.setattr(app_module, "runtime_manager", FakeRuntime(plc_status))
    on_device(env)
    assert runtime_license.store(as_bytes(make_license())).ok
    assert "license" not in app_module.handle_status({})
