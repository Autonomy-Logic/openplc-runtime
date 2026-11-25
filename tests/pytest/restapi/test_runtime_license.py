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
        "license": tmp_path / "data" / "runtime.license",
    }
    paths["stored"].parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hardware_id, "STORED_PATH", paths["stored"])
    monkeypatch.setattr(runtime_license, "LICENSE_PATH", paths["license"])
    return paths


def on_device(paths: dict[str, pathlib.Path]) -> None:
    paths["stored"].write_text(RECORD)


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


def test_module_exposes_no_signature_verification() -> None:
    for name in ("TRUSTED_KEYS", "DEV_KEY_ENV", "verify_signature", "verifies_for", "status"):
        assert not hasattr(runtime_license, name)


# --- storage ----------------------------------------------------------------


def test_store_keeps_a_licence_unverified(env) -> None:
    on_device(env)
    data = make_license()
    result = runtime_license.store(as_bytes(data))
    assert result == runtime_license.StoreResult(True, order_id="ord_1")
    assert json.loads(env["license"].read_bytes()) == data


def test_store_keeps_a_licence_for_another_hardware_id(env) -> None:
    on_device(env)
    assert runtime_license.store(as_bytes(make_license(hardware=OTHER_ID))).ok
    assert env["license"].exists()


def test_store_works_without_a_hardware_id(env) -> None:
    assert runtime_license.store(as_bytes(make_license())).ok
    assert env["license"].exists()


@pytest.mark.parametrize(
    ("raw", "error"),
    [(b"x" * 4097, "larger than 4096"), (b"{}", "Not an OpenPLC Runtime licence")],
)
def test_store_refuses(env, raw, error) -> None:
    on_device(env)
    result = runtime_license.store(raw)
    assert not result.ok
    assert error in result.error
    assert not result.internal
    assert not env["license"].exists()


def test_store_reports_a_write_failure(env, monkeypatch) -> None:
    on_device(env)

    def fail(_path: str | pathlib.Path, _blob: bytes) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(runtime_license, "write_file_atomically", fail)
    result = runtime_license.store(as_bytes(make_license()))
    assert not result.ok and result.internal


def test_store_replaces_the_previous_licence(env) -> None:
    on_device(env)
    assert runtime_license.store(as_bytes(make_license(order_id="ord_1"))).ok
    newer = make_license(order_id="ord_2", hardware=OTHER_ID)
    assert runtime_license.store(as_bytes(newer)).ok
    assert runtime_license.report()["licenses"] == [newer]
    assert sorted(p.name for p in env["license"].parent.iterdir()) == [
        "hardware-id",
        "runtime.license",
    ]


def test_remove(env) -> None:
    on_device(env)
    assert runtime_license.remove() is False
    assert runtime_license.store(as_bytes(make_license())).ok
    assert runtime_license.remove() is True
    assert not env["license"].exists()
    assert runtime_license.report()["licenses"] == []


@pytest.mark.parametrize(
    "content", [b"", b"{", b" " * 5000], ids=["empty", "malformed", "too-large"]
)
def test_remove_an_unusable_slot_reports_nothing_removed(env, content) -> None:
    env["license"].parent.mkdir(parents=True, exist_ok=True)
    env["license"].write_bytes(content)
    assert runtime_license.remove() is False
    assert not env["license"].exists()


# --- report -----------------------------------------------------------------


def test_report_without_a_hardware_id(env) -> None:
    assert runtime_license.report() == {"hardwareId": None, "licenses": []}


def test_report_lists_the_stored_licence(env) -> None:
    on_device(env)
    data = make_license()
    assert runtime_license.store(as_bytes(data)).ok
    assert runtime_license.report() == {"hardwareId": ID, "licenses": [data]}


@pytest.mark.parametrize(
    "content", [b"", b"{", b"\xff", b" " * 5000], ids=["empty", "malformed", "binary", "too-large"]
)
def test_report_skips_an_unusable_stored_file(env, content) -> None:
    on_device(env)
    env["license"].write_bytes(content)
    assert runtime_license.report()["licenses"] == []


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


# --- endpoints ----------------------------------------------------------------


@pytest.fixture()
def user_token(client) -> str:
    assert create_user(client, "admin", "admin-pass-123").status_code == 201
    return token_for(client, "admin", "admin-pass-123")


def test_write_endpoints_need_a_login(client, env) -> None:
    assert client.post("/api/runtime-license", data=b"{}").status_code == 401
    assert client.delete("/api/runtime-license").status_code == 401


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_write_endpoints_refuse_operators_and_viewers(client, env, user_token, role) -> None:
    on_device(env)
    assert create_user(client, role, f"{role}-pass", user_token, role=role).status_code == 201
    token = token_for(client, role, f"{role}-pass")
    upload = client.post(
        "/api/runtime-license",
        data=as_bytes(make_license()),
        headers=auth(token),
        content_type="application/json",
    )
    assert upload.status_code == 403
    assert client.delete("/api/runtime-license", headers=auth(token)).status_code == 403
    assert client.get("/api/runtime-license").get_json()["licenses"] == []


def test_write_endpoints_accept_an_engineer(client, env, user_token) -> None:
    on_device(env)
    assert create_user(client, "eng", "eng-pass", user_token, role="engineer").status_code == 201
    token = token_for(client, "eng", "eng-pass")
    resp = client.post(
        "/api/runtime-license",
        data=as_bytes(make_license()),
        headers=auth(token),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.get_json()


def test_read_endpoint_is_public(client, env) -> None:
    on_device(env)
    assert runtime_license.store(as_bytes(make_license())).ok
    resp = client.get("/api/runtime-license")
    assert resp.status_code == 200
    assert resp.get_json() == {"hardwareId": ID, "licenses": [make_license()]}


def test_delete_endpoint(client, env, user_token) -> None:
    on_device(env)
    assert runtime_license.store(as_bytes(make_license())).ok
    resp = client.delete("/api/runtime-license", headers=auth(user_token))
    assert resp.status_code == 200 and resp.get_json() == {"removed": True}
    resp = client.delete("/api/runtime-license", headers=auth(user_token))
    assert resp.status_code == 200 and resp.get_json() == {"removed": False}
    assert client.get("/api/runtime-license").get_json()["licenses"] == []


def test_delete_endpoint_on_an_emptied_slot(client, env, user_token) -> None:
    env["license"].parent.mkdir(parents=True, exist_ok=True)
    env["license"].write_bytes(b"")
    resp = client.delete("/api/runtime-license", headers=auth(user_token))
    assert resp.status_code == 200 and resp.get_json() == {"removed": False}
    assert not env["license"].exists()


def test_delete_endpoint_reports_a_failure(client, env, user_token, monkeypatch) -> None:
    def fail() -> bool:
        raise OSError("read-only")

    monkeypatch.setattr(runtime_license, "remove", fail)
    assert client.delete("/api/runtime-license", headers=auth(user_token)).status_code == 500


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
    assert resp.get_json() == {"hardwareId": ID, "licenses": [data]}


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


@pytest.mark.parametrize("field", ["hardware_id", "order_id", "sig"])
def test_upload_refuses_a_trailing_newline(client, env, user_token, field) -> None:
    on_device(env)
    data = make_license()
    resp = client.post(
        "/api/runtime-license",
        data=as_bytes({**data, field: f"{data[field]}\n"}),
        headers=auth(user_token),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert not env["license"].exists()


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

    def fail(_path: str | pathlib.Path, _blob: bytes) -> None:
        raise OSError("full")

    monkeypatch.setattr(runtime_license, "write_file_atomically", fail)
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
def test_status_omits_the_licence(env, monkeypatch, plc_status) -> None:
    from webserver import app as app_module

    monkeypatch.setattr(app_module, "runtime_manager", FakeRuntime(plc_status))
    on_device(env)
    assert runtime_license.store(as_bytes(make_license())).ok
    assert "license" not in app_module.handle_status({})
