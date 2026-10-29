# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Runtime licences: shape, signature check, storage, the status /api/status reports,
and the upload endpoint."""

import base64
import io
import json
import pathlib
import subprocess
from dataclasses import dataclass

import pytest
from conftest import auth, create_user, token_for

from webserver import hardware_id, runtime_license

ID = "hw2:" + "a" * 64
OTHER_ID = "hw2:" + "b" * 64
RECORD = f"{ID}\nserial {'1' * 64}\n"


@dataclass(frozen=True)
class Signer:
    key_path: pathlib.Path
    raw_public_hex: str


def _der_to_raw(der: bytes) -> bytes:
    """Raw r||s from an ECDSA-Sig-Value DER signature."""
    assert der[0] == 0x30
    pos = 2
    parts = []
    for _ in range(2):
        assert der[pos] == 0x02
        length = der[pos + 1]
        value = der[pos + 2 : pos + 2 + length].lstrip(b"\x00")
        parts.append(value.rjust(32, b"\x00"))
        pos += 2 + length
    return parts[0] + parts[1]


def _make_signer(directory: pathlib.Path) -> Signer:
    key = directory / "key.pem"
    subprocess.run(
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)],
        check=True,
        capture_output=True,
    )
    der = subprocess.run(
        ["openssl", "ec", "-in", str(key), "-pubout", "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    return Signer(key_path=key, raw_public_hex=der[-64:].hex())


def _sign(signer: Signer, message: bytes, work: pathlib.Path) -> str:
    msg = work / "msg"
    msg.write_bytes(message)
    der = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(signer.key_path), str(msg)],
        check=True,
        capture_output=True,
    ).stdout
    return base64.b64encode(_der_to_raw(der)).decode("ascii")


@pytest.fixture(scope="module")
def signer(tmp_path_factory: pytest.TempPathFactory) -> Signer:
    return _make_signer(tmp_path_factory.mktemp("signer"))


@pytest.fixture(scope="module")
def other_signer(tmp_path_factory: pytest.TempPathFactory) -> Signer:
    return _make_signer(tmp_path_factory.mktemp("other-signer"))


def make_license(
    signer: Signer,
    work: pathlib.Path,
    hardware: str = ID,
    order_id: str = "ord_1",
    key_id: int = 0,
    seats: int = 2,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "fmt": 1,
        "product": runtime_license.PRODUCT,
        "hardware_id": hardware,
        "seats": seats,
        "order_id": order_id,
        "issued_at": "2026-10-28T12:00:00.000Z",
        "key_id": key_id,
    }
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "sig": _sign(signer, message, work)}


def as_bytes(data: dict[str, object]) -> bytes:
    return json.dumps(data).encode()


@pytest.fixture()
def env(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, signer: Signer
) -> dict[str, pathlib.Path]:
    paths = {
        "stored": tmp_path / "data" / "hardware-id",
        "injected": tmp_path / "etc" / "hardware-id",
        "seat": tmp_path / "etc" / "runtime.license",
        "device_dir": tmp_path / "data" / "licenses",
        "vplc_dir": tmp_path / "var" / "licenses",
        "work": tmp_path / "work",
    }
    for key in ("stored", "injected", "work"):
        (paths[key] if key == "work" else paths[key].parent).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hardware_id, "STORED_PATH", paths["stored"])
    monkeypatch.setattr(hardware_id, "INJECTED_PATH", paths["injected"])
    monkeypatch.setattr(runtime_license, "SEAT_FILE_PATH", paths["seat"])
    monkeypatch.setattr(runtime_license, "DEVICE_LICENSES_DIR", paths["device_dir"])
    monkeypatch.setattr(runtime_license, "VPLC_LICENSES_DIR", paths["vplc_dir"])
    monkeypatch.setattr(runtime_license, "TRUSTED_KEYS", {})
    monkeypatch.setenv(runtime_license.DEV_KEY_ENV, f"0:{signer.raw_public_hex}")
    runtime_license.reset_cache()
    yield paths
    runtime_license.reset_cache()


def on_device(paths: dict[str, pathlib.Path]) -> None:
    paths["stored"].write_text(RECORD)


def in_vplc(paths: dict[str, pathlib.Path]) -> None:
    paths["injected"].write_text(RECORD)


# --- shape ----------------------------------------------------------------


def test_parse_accepts_an_edge_licence(env, signer) -> None:
    data = make_license(signer, env["work"])
    lic = runtime_license.parse_license(data)
    assert lic is not None
    assert (lic.hardware_id, lic.order_id, lic.key_id) == (ID, "ord_1", 0)
    assert len(lic.signature) == 64


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"fmt": 2},
        {"product": "com.autonomy.other"},
        {"hardware_id": "hw2:abc"},
        {"seats": 0},
        {"seats": True},
        {"seats": 1001},
        {"order_id": "../escape"},
        {"order_id": 7},
        {"issued_at": ""},
        {"key_id": -1},
        {"key_id": False},
        {"sig": "short"},
    ],
)
def test_parse_refuses_other_shapes(env, signer, change) -> None:
    data = {**make_license(signer, env["work"]), **change}
    assert runtime_license.parse_license(data) is None


@pytest.mark.parametrize("data", [None, [], "x"])
def test_parse_refuses_non_objects(data) -> None:
    assert runtime_license.parse_license(data) is None


@pytest.mark.parametrize("raw", [b"\xff", b"{", b"[]"])
def test_parse_bytes_refuses_non_json(raw) -> None:
    assert runtime_license.parse_license_bytes(raw) is None


def test_canonical_payload_matches_edge_bytes(env, signer) -> None:
    lic = runtime_license.parse_license(make_license(signer, env["work"]))
    assert runtime_license.canonical_payload(lic) == (
        b'{"fmt":1,"hardware_id":"' + ID.encode() + b'","issued_at":"2026-10-28T12:00:00.000Z",'
        b'"key_id":0,"order_id":"ord_1","product":"com.autonomy.openplc-runtime","seats":2}'
    )


def test_to_bytes_round_trips(env, signer) -> None:
    data = make_license(signer, env["work"])
    lic = runtime_license.parse_license(data)
    assert json.loads(lic.to_bytes()) == data


# --- signature --------------------------------------------------------------


def test_verifies_with_the_development_key(env, signer) -> None:
    lic = runtime_license.parse_license(make_license(signer, env["work"]))
    assert runtime_license.verifies_for(lic, ID)


def test_development_key_needs_its_variable(env, signer, monkeypatch) -> None:
    monkeypatch.delenv(runtime_license.DEV_KEY_ENV)
    lic = runtime_license.parse_license(make_license(signer, env["work"]))
    assert not runtime_license.verifies_for(lic, ID)


def test_verifies_with_a_built_in_key(env, signer, monkeypatch) -> None:
    monkeypatch.delenv(runtime_license.DEV_KEY_ENV)
    monkeypatch.setattr(runtime_license, "TRUSTED_KEYS", {3: signer.raw_public_hex})
    lic = runtime_license.parse_license(make_license(signer, env["work"], key_id=3))
    assert runtime_license.verifies_for(lic, ID)


def test_refuses_another_hardware_id(env, signer) -> None:
    lic = runtime_license.parse_license(make_license(signer, env["work"]))
    assert not runtime_license.verifies_for(lic, OTHER_ID)


def test_refuses_an_unknown_key_id(env, signer) -> None:
    lic = runtime_license.parse_license(make_license(signer, env["work"], key_id=5))
    assert not runtime_license.verifies_for(lic, ID)


def test_refuses_another_signer(env, other_signer) -> None:
    lic = runtime_license.parse_license(make_license(other_signer, env["work"]))
    assert not runtime_license.verifies_for(lic, ID)


def test_refuses_a_changed_field(env, signer) -> None:
    data = {**make_license(signer, env["work"]), "seats": 50}
    lic = runtime_license.parse_license(data)
    assert not runtime_license.verifies_for(lic, ID)


@pytest.mark.parametrize("value", ["junk", "x:" + "a" * 128, "0:abc", "0:" + "g" * 128])
def test_malformed_development_key_is_ignored(env, monkeypatch, value) -> None:
    monkeypatch.setenv(runtime_license.DEV_KEY_ENV, value)
    assert runtime_license.trusted_keys(0) == []


def test_development_key_only_for_its_key_id(env, signer, monkeypatch) -> None:
    monkeypatch.setattr(runtime_license, "TRUSTED_KEYS", {0: "c" * 128})
    assert runtime_license.trusted_keys(0) == ["c" * 128, signer.raw_public_hex]
    assert runtime_license.trusted_keys(1) == []


def test_verify_signature_refuses_bad_inputs(signer) -> None:
    assert not runtime_license.verify_signature(b"m", b"\x00" * 63, signer.raw_public_hex)
    assert not runtime_license.verify_signature(b"m", b"\x00" * 64, "zz")


def test_verify_signature_without_openssl(env, signer, monkeypatch) -> None:
    def missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("openssl")

    lic = runtime_license.parse_license(make_license(signer, env["work"]))
    monkeypatch.setattr(runtime_license.subprocess, "run", missing)
    assert not runtime_license.verifies_for(lic, ID)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"\x00" * 31 + b"\x01", b"\x02\x01\x01"),
        (b"\x80" + b"\x00" * 31, b"\x02\x21\x00\x80" + b"\x00" * 31),
        (b"\x00" * 32, b"\x02\x01\x00"),
    ],
)
def test_der_integer(raw, expected) -> None:
    assert runtime_license._der_integer(raw) == expected


# --- storage ----------------------------------------------------------------


def test_store_keeps_a_device_licence(env, signer) -> None:
    on_device(env)
    data = make_license(signer, env["work"])
    result = runtime_license.store(as_bytes(data))
    assert result == runtime_license.StoreResult(True, order_id="ord_1")
    stored = env["device_dir"] / "ord_1.license"
    assert json.loads(stored.read_bytes()) == data


def test_store_in_a_vplc_uses_the_agent_folder(env, signer) -> None:
    in_vplc(env)
    assert runtime_license.store(as_bytes(make_license(signer, env["work"]))).ok
    assert (env["vplc_dir"] / "ord_1.license").exists()
    assert not env["device_dir"].exists()
    assert runtime_license.licenses_dir() == env["vplc_dir"]


def test_licenses_dir_on_a_device(env) -> None:
    on_device(env)
    assert runtime_license.licenses_dir() == env["device_dir"]


@pytest.mark.parametrize(
    ("setup", "make", "error"),
    [
        (on_device, lambda s, w: b"x" * 4097, "larger than 4096"),
        (on_device, lambda s, w: b"{}", "Not an OpenPLC Runtime licence"),
        (lambda p: None, lambda s, w: as_bytes(make_license(s, w)), "no hardware ID"),
        (
            on_device,
            lambda s, w: as_bytes(make_license(s, w, hardware=OTHER_ID)),
            "another hardware ID",
        ),
        (on_device, lambda s, w: as_bytes(make_license(s, w, key_id=9)), "does not verify"),
    ],
)
def test_store_refuses(env, signer, setup, make, error) -> None:
    setup(env)
    result = runtime_license.store(make(signer, env["work"]))
    assert not result.ok
    assert error in result.error
    assert not result.internal
    assert not env["device_dir"].exists()


def test_store_reports_a_write_failure(env, signer, monkeypatch) -> None:
    on_device(env)

    def fail(_path: str, _blob: bytes) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(runtime_license, "_write_license_atomically", fail)
    result = runtime_license.store(as_bytes(make_license(signer, env["work"])))
    assert not result.ok and result.internal


# --- status -----------------------------------------------------------------


def test_status_without_a_hardware_id_is_gpl(env) -> None:
    assert runtime_license.compute_status() == runtime_license.GPL


def test_status_on_a_device(env, signer) -> None:
    on_device(env)
    assert runtime_license.status() == runtime_license.GPL
    assert runtime_license.store(as_bytes(make_license(signer, env["work"]))).ok
    assert runtime_license.status() == runtime_license.COMMERCIAL


def test_status_ignores_unusable_files(env, signer) -> None:
    on_device(env)
    env["device_dir"].mkdir(parents=True)
    (env["device_dir"] / "notes.txt").write_bytes(as_bytes(make_license(signer, env["work"])))
    (env["device_dir"] / "bad.license").write_bytes(b"{")
    (env["device_dir"] / "big.license").write_bytes(b" " * 5000)
    (env["device_dir"] / "foreign.license").write_bytes(
        as_bytes(make_license(signer, env["work"], hardware=OTHER_ID))
    )
    assert runtime_license.compute_status() == runtime_license.GPL


def test_status_reads_at_most_the_cap(env, signer, monkeypatch) -> None:
    on_device(env)
    env["device_dir"].mkdir(parents=True)
    (env["device_dir"] / "b.license").write_bytes(as_bytes(make_license(signer, env["work"])))
    (env["device_dir"] / "a.license").write_bytes(b"{")
    monkeypatch.setattr(runtime_license, "MAX_STORED_LICENSES", 1)
    assert runtime_license.compute_status() == runtime_license.GPL


def seat(paths: dict[str, pathlib.Path], content: bytes) -> None:
    paths["seat"].write_bytes(content)


def seat_file(signer: Signer, work: pathlib.Path, **kwargs: object) -> bytes:
    return json.dumps(
        {"fmt": 1, "vplc": "vplc-1", "license": make_license(signer, work, **kwargs)}
    ).encode()


def test_vplc_seat_file_gives_commercial(env, signer) -> None:
    in_vplc(env)
    seat(env, seat_file(signer, env["work"]))
    assert runtime_license.compute_status() == runtime_license.COMMERCIAL


def test_vplc_host_licence_alone_is_gpl(env, signer) -> None:
    in_vplc(env)
    assert runtime_license.store(as_bytes(make_license(signer, env["work"]))).ok
    assert runtime_license.compute_status() == runtime_license.GPL
    seat(env, b"")
    assert runtime_license.compute_status() == runtime_license.GPL


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"\xff",
        json.dumps({"fmt": 2, "vplc": "v", "license": {}}).encode(),
        json.dumps({"fmt": 1, "vplc": "", "license": {}}).encode(),
        json.dumps([1]).encode(),
        pytest.param(b" " * 9000, id="too-large"),
    ],
)
def test_vplc_malformed_seat_file_is_gpl(env, content) -> None:
    in_vplc(env)
    seat(env, content)
    assert runtime_license.compute_status() == runtime_license.GPL


def test_vplc_seat_for_another_host_is_gpl(env, signer) -> None:
    in_vplc(env)
    seat(env, seat_file(signer, env["work"], hardware=OTHER_ID))
    assert runtime_license.compute_status() == runtime_license.GPL


def test_status_is_cached_until_a_file_changes(env, signer, monkeypatch) -> None:
    in_vplc(env)
    calls: list[int] = []
    real = runtime_license.compute_status

    def counted() -> str:
        calls.append(1)
        return real()

    monkeypatch.setattr(runtime_license, "compute_status", counted)
    assert runtime_license.status() == runtime_license.GPL
    assert runtime_license.status() == runtime_license.GPL
    assert len(calls) == 1
    seat(env, seat_file(signer, env["work"]))
    assert runtime_license.status() == runtime_license.COMMERCIAL
    assert len(calls) == 2


def test_startup_log(env, signer, monkeypatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(runtime_license.logger, "info", lambda msg, *args: lines.append(msg % args))
    on_device(env)
    runtime_license.log_startup_status()
    assert runtime_license.store(as_bytes(make_license(signer, env["work"]))).ok
    runtime_license.log_startup_status()
    assert lines[0] == "OpenPLC Runtime licence: GPLv3 (no commercial licence found)"
    assert lines[-1] == "OpenPLC Runtime licence: commercial"


# --- endpoint -----------------------------------------------------------------


@pytest.fixture()
def user_token(client) -> str:
    assert create_user(client, "admin", "admin-pass-123").status_code == 201
    return token_for(client, "admin", "admin-pass-123")


def test_endpoint_needs_a_login(client, env) -> None:
    assert client.post("/api/runtime-license", data=b"{}").status_code == 401


def test_endpoint_stores_a_licence(client, env, signer, user_token) -> None:
    on_device(env)
    resp = client.post(
        "/api/runtime-license",
        data=as_bytes(make_license(signer, env["work"])),
        headers=auth(user_token),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == {"orderId": "ord_1", "license": "commercial"}


def test_endpoint_refuses_a_foreign_licence(client, env, signer, user_token) -> None:
    on_device(env)
    resp = client.post(
        "/api/runtime-license",
        data=as_bytes(make_license(signer, env["work"], hardware=OTHER_ID)),
        headers=auth(user_token),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "another hardware ID" in resp.get_json()["error"]


def test_endpoint_refuses_a_large_body(client, env, user_token) -> None:
    resp = client.post("/api/runtime-license", data=b"x" * 5000, headers=auth(user_token))
    assert resp.status_code == 413


def test_endpoint_refuses_a_large_streamed_body(client, env, user_token) -> None:
    resp = client.post(
        "/api/runtime-license",
        input_stream=io.BytesIO(b"x" * 5000),
        headers={**auth(user_token), "Transfer-Encoding": "chunked"},
        environ_overrides={"wsgi.input_terminated": True},
    )
    assert resp.status_code == 413


def test_endpoint_reports_a_write_failure(client, env, signer, user_token, monkeypatch) -> None:
    on_device(env)

    def fail(_path: str, _blob: bytes) -> None:
        raise OSError("full")

    monkeypatch.setattr(runtime_license, "_write_license_atomically", fail)
    resp = client.post(
        "/api/runtime-license",
        data=as_bytes(make_license(signer, env["work"])),
        headers=auth(user_token),
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
def test_status_reports_the_licence(env, signer, monkeypatch, plc_status) -> None:
    from webserver import app as app_module

    monkeypatch.setattr(app_module, "runtime_manager", FakeRuntime(plc_status))
    on_device(env)
    assert app_module.handle_status({})["license"] == "gpl"
    assert runtime_license.store(as_bytes(make_license(signer, env["work"]))).ok
    assert app_module.handle_status({})["license"] == "commercial"
