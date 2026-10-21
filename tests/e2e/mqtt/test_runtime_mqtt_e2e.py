# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""MQTT and Sparkplug B end to end: real runtime image, real plc_main, real program, real brokers.

The program (payload/generate_payloads.test.ts) increments a counter every 10 ms
scan, flips a slow toggle, alternates a REAL between a value and NaN, and holds
known extremes of every supported type. Expected values come from that program,
never from the serializers under test.
"""

from __future__ import annotations

import base64
import itertools
import json
import secrets
import struct
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import sparkplug_wire as wire
from api_schema import check
from e2e_env import (
    Consumer,
    Message,
    Network,
    Runtime,
    host_port,
    modbus_read_holding,
    until,
)

PREFIX = "openplc/plc-e2e/instance0/Main/"
SPB = "spBv1.0/openplc/"
NODE = "plc-e2e"
NS_PER_DAY = 86_400 * 10**9
# Generated per run, so no source file shipped in an image can contain it.
WRONG_PASSWORD = f"MARKER-PW-wrong-{secrets.token_hex(6)}"


def utf16(*units: int) -> bytes:
    return b"".join(struct.pack("<H", u) for u in units)


GENERIC_CONSTANTS: dict[str, object] = {
    "LMin": "-9223372036854775808",
    "LMax": "9223372036854775807",
    "UMax": "18446744073709551615",
    "WMax": "18446744073709551615",
    "Unsafe": "9007199254740993",
    "S8": -128,
    "U8": 255,
    "I16": -32768,
    "U16": 65535,
    "D32": -2147483648,
    "U32": 4294967295,
    "Bt": 165,
    "Wd": 48879,
    "Dw": 3735928559,
    "Lr": 0.1,
    "T1": "-1500000000",
    "Tn": "1002003004",
    "Td": "12:34:56.789000000",
    "D1": "2024-02-29",
    "Dt1": "2024-02-29T12:34:56.789000000Z",
    "Txt": 'line "1" \\ end',
    "LongTxt": None,
    "W": base64.b64encode(utf16(0x41, 0xD83D, 0xDE00, 0x42)).decode(),
    "WOdd": base64.b64encode(utf16(0xD800, 0x78)).decode(),
}
CHANGING = ["Counter", "Toggle", "Flaky", "Grid/-2,3", "Grid/1,4", "Vec/-1/Count"]
CHANGING += ["Vec/1/Value", "S1/Out/Count", "S2/Out/Count", "S2/Seen"]
ALL_GENERIC = CHANGING + list(GENERIC_CONSTANTS)

DATE_NS = 19782 * NS_PER_DAY
SPARKPLUG_CONSTANTS: dict[str, tuple[int, object, dict[str, str]]] = {
    "LMin": (4, -(2**63), {"iecType": "LINT", "encoding": "native"}),
    "LMax": (4, 2**63 - 1, {"iecType": "LINT", "encoding": "native"}),
    "UMax": (8, 2**64 - 1, {"iecType": "ULINT", "encoding": "native"}),
    "WMax": (8, 2**64 - 1, {"iecType": "LWORD", "encoding": "native"}),
    "Unsafe": (4, 2**53 + 1, {"iecType": "LINT", "encoding": "native"}),
    "S8": (1, -128, {"iecType": "SINT", "encoding": "native"}),
    "U16": (6, 65535, {"iecType": "UINT", "encoding": "native"}),
    "D32": (3, -(2**31), {"iecType": "DINT", "encoding": "native"}),
    "U32": (7, 2**32 - 1, {"iecType": "UDINT", "encoding": "native"}),
    "Bt": (5, 165, {"iecType": "BYTE", "encoding": "native"}),
    "Dw": (7, 3735928559, {"iecType": "DWORD", "encoding": "native"}),
    "Lr": (10, 0.1, {"iecType": "LREAL", "encoding": "native"}),
    "T1": (
        4,
        -1_500_000_000,
        {"iecType": "TIME", "encoding": "int64-ns-duration", "unit": "ns"},
    ),
    "Tn": (4, 1_002_003_004, {"iecType": "TIME", "encoding": "int64-ns-duration", "unit": "ns"}),
    "Td": (
        4,
        45_296_789_000_000,
        {"iecType": "TOD", "encoding": "int64-ns-since-midnight", "unit": "ns"},
    ),
    "D1": (
        4,
        DATE_NS,
        {
            "iecType": "DATE",
            "encoding": "int64-ns-since-unix-epoch-utc-midnight",
            "unit": "ns",
            "epoch": "unix",
        },
    ),
    "Dt1": (
        4,
        DATE_NS + 45_296_789_000_000,
        {
            "iecType": "DT",
            "encoding": "int64-ns-since-unix-epoch",
            "unit": "ns",
            "epoch": "unix",
        },
    ),
    "Txt": (12, 'line "1" \\ end', {"iecType": "STRING", "encoding": "native"}),
    "W": (17, utf16(0x41, 0xD83D, 0xDE00, 0x42), {"iecType": "WSTRING", "encoding": "utf16le"}),
    "WOdd": (17, utf16(0xD800, 0x78), {"iecType": "WSTRING", "encoding": "utf16le"}),
}


# --- helpers ---------------------------------------------------------------------


def num(stats: dict[str, object], key: str) -> int:
    value = stats[key]
    assert isinstance(value, int), (key, value)
    return value


def generic_value(payload: bytes) -> object:
    def only_value(pairs: list[tuple[str, object]]) -> object:
        assert [k for k, _ in pairs] == ["value"], pairs
        return pairs[0][1]

    return json.loads(payload, object_pairs_hook=only_value)


def by_leaf(messages: list[Message]) -> dict[str, list[object]]:
    out: dict[str, list[object]] = {}
    for msg in messages:
        if msg.topic.startswith(PREFIX):
            out.setdefault(msg.topic[len(PREFIX) :], []).append(generic_value(msg.payload))
    return out


def wait_status(
    runtime: Runtime, predicate: Callable[[dict[str, object]], bool], seconds: float
) -> dict[str, object]:
    holder: dict[str, dict[str, object]] = {}

    def probe() -> bool:
        holder["s"] = runtime.mqtt_status()
        return predicate(holder["s"])

    assert until(probe, seconds, 0.25), holder.get("s")
    status = holder["s"]
    assert check(status, "status") == [], status
    return status


def error_code(status: dict[str, object]) -> str | None:
    error = status.get("error")
    return str(error["code"]) if isinstance(error, dict) else None


def program_md5(payloads: Path, name: str) -> str:
    return str(json.loads((payloads / name / "debug-map.json").read_text())["md5"])


def tls_connection(network: Network, port: int, host: str = "broker") -> dict[str, object]:
    ca = network.pki.read("ca.crt").decode()
    return {
        "host": host,
        "port": port,
        "client_id": "plc-e2e",
        "tls": {"enabled": True, "trust": {"source": "pem", "ca_pem": ca}},
    }


def deploy_running(runtime: Runtime, payload: Path, mutate=None) -> None:  # type: ignore[no-untyped-def]
    result = runtime.deploy(payload, mutate)
    assert result.get("build") == "SUCCESS", result
    runtime.start_plc()


def stage_password(runtime: Runtime, md5: str, password: str, port: int) -> dict[str, object]:
    request = {
        "method": "username_password",
        "binding": {
            "host": "broker",
            "port": port,
            "tls_enabled": True,
            "client_id": "plc-e2e",
            "method": "username_password",
        },
        "programMd5": md5,
        "username": "spike-user",
    }
    receipt = runtime.stage(request, {"password": password.encode()})
    ref = receipt["credentialRef"]
    assert isinstance(ref, dict)
    return ref


def stage_certificate(runtime: Runtime, network: Network, md5: str, stem: str) -> dict[str, object]:
    request = {
        "method": "client_certificate",
        "binding": {
            "host": "broker",
            "port": 8884,
            "tls_enabled": True,
            "client_id": "plc-e2e",
            "method": "client_certificate",
        },
        "programMd5": md5,
    }
    secrets = {
        "client_certificate": network.pki.read(f"{stem}.crt"),
        "client_key": network.pki.read(f"{stem}.key"),
    }
    ref = runtime.stage(request, secrets)["credentialRef"]
    assert isinstance(ref, dict)
    return ref


@pytest.fixture
def generic_consumer(network: Network) -> Consumer:
    consumer = Consumer(network.broker_port, ["openplc/#"]).start()
    yield consumer  # type: ignore[misc]
    consumer.stop()


@pytest.fixture
def spb_consumer(network: Network) -> Consumer:
    consumer = Consumer(network.broker_port, [f"{SPB}#"]).start()
    yield consumer  # type: ignore[misc]
    consumer.stop()


# --- generic ---------------------------------------------------------------------


def test_generic_on_change_publishes_the_real_program(
    runtime: Runtime, payloads: Path, generic_consumer: Consumer, record_property
) -> None:
    deploy_running(runtime, payloads / "e2e-generic")
    started = runtime.scan_stats()
    time.sleep(6)
    stats = runtime.scan_stats()
    messages = generic_consumer.snapshot()
    leaves = by_leaf(messages)
    assert sorted(leaves) == sorted(ALL_GENERIC)
    assert {(m.qos, m.retain) for m in messages} == {(0, False)}
    for name, expected in GENERIC_CONSTANTS.items():
        assert leaves[name] == [expected], (name, leaves[name])
    counter = [int(v) for v in leaves["Counter"]]  # type: ignore[call-overload]
    assert len(counter) >= 40 and all(b > a for a, b in itertools.pairwise(counter))
    assert counter[-1] <= num(stats, "scan_count")
    assert num(stats, "scan_count") - counter[-1] < 200  # type: ignore[call-overload]
    toggle = leaves["Toggle"]
    assert len(toggle) >= 15 and all(a != b for a, b in itertools.pairwise(toggle))
    flaky = leaves["Flaky"]
    assert None in flaky and 0.25 in flaky and all(a != b for a, b in itertools.pairwise(flaky))
    assert all(v is None or v == 0.25 for v in flaky)
    grid_up = [int(v) for v in leaves["Grid/-2,3"]]  # type: ignore[call-overload]
    grid_down = [int(v) for v in leaves["Grid/1,4"]]  # type: ignore[call-overload]
    assert grid_up[-1] > 0 and grid_down[-1] < 0
    s1 = [int(v) for v in leaves["S1/Out/Count"]]  # type: ignore[call-overload]
    s2 = [int(v) for v in leaves["S2/Out/Count"]]  # type: ignore[call-overload]
    assert all(1000 < v < 2000 for v in s1) and all(v > 2000 for v in s2)
    assert {int(v) - 2000 for v in s2} <= {int(v) for v in leaves["S2/Seen"]}  # type: ignore[call-overload]
    vec_value = leaves["Vec/1/Value"]
    assert all(isinstance(v, float) and v == int(v) for v in vec_value)
    status = wait_status(runtime, lambda s: s["state"] == "connected", 5)
    assert status["mode"] == "generic" and status["stats"]["leaves"] == 34  # type: ignore[index]
    assert status["stats"]["published"] >= len(messages)  # type: ignore[index]
    assert status["stats"]["readErrors"] > 0  # type: ignore[index]
    # Overruns are recorded, not asserted: this host shows them without MQTT too (see run notes).
    record_property("overruns_delta", num(stats, "overruns") - num(started, "overruns"))
    record_property("generic_messages", len(messages))
    record_property("scan_stats", json.dumps(stats))


def test_stop_and_start_close_and_reannounce(
    runtime: Runtime, payloads: Path, generic_consumer: Consumer
) -> None:
    deploy_running(runtime, payloads / "e2e-generic")
    assert until(lambda: len(by_leaf(generic_consumer.snapshot())) == len(ALL_GENERIC), 10)
    stop_at = time.monotonic()
    elapsed = runtime.stop_plc()
    assert elapsed < 5.0
    last = max(m.at for m in generic_consumer.snapshot())
    assert last - stop_at < 5.0
    status = wait_status(runtime, lambda s: s["state"] == "plc_stopped", 5)
    assert status["configured"] is True and status["error"] is None
    before = len(generic_consumer.snapshot())
    time.sleep(2)
    assert len(generic_consumer.snapshot()) == before
    generic_consumer.clear()
    runtime.start_plc()
    assert until(lambda: len(by_leaf(generic_consumer.snapshot())) == len(ALL_GENERIC), 10)
    time.sleep(1)
    # A start reloads the program, so the counter restarts with the scan count.
    counter = [int(v) for v in by_leaf(generic_consumer.snapshot())["Counter"]]  # type: ignore[call-overload]
    assert all(b > a for a, b in itertools.pairwise(counter))
    assert counter[-1] <= int(runtime.scan_stats()["scan_count"])
    assert "failed to stop cleanly" not in runtime.logs()


def test_broker_outage_keeps_the_plc_running_and_recovers_current_state(
    runtime: Runtime, payloads: Path, network: Network, generic_consumer: Consumer, record_property
) -> None:
    def via_proxy(doc: dict[str, object]) -> None:
        doc["connection"]["host"] = "mqtt-proxy"  # type: ignore[index]

    deploy_running(runtime, payloads / "e2e-generic", via_proxy)
    wait_status(runtime, lambda s: s["state"] == "connected", 10)
    before = runtime.scan_stats()
    network.cut()
    cut_at = time.monotonic()
    status = wait_status(runtime, lambda s: s["state"] == "retry_wait", 40)
    assert status["error"]["category"] == "network"  # type: ignore[index]
    seen_waits: list[int] = []
    attempts: list[int] = []

    def capped() -> bool:
        s = runtime.mqtt_status()
        reconnect = s["reconnect"]
        if reconnect["nextRetryInMs"] is not None:  # type: ignore[index]
            seen_waits.append(int(reconnect["nextRetryInMs"]))  # type: ignore[index]
        attempts.append(int(reconnect["attempt"]))  # type: ignore[index]
        return max(attempts) >= 7

    assert until(capped, 120, 0.5), attempts
    during = runtime.scan_stats()
    during_at = time.monotonic()
    generic_consumer.clear()
    network.restore()
    restored_at = time.monotonic()
    restored_scan = int(runtime.scan_stats()["scan_count"])
    wait_status(runtime, lambda s: s["state"] == "connected", 45)
    assert until(lambda: "Counter" in by_leaf(generic_consumer.snapshot()), 5)
    first = int(by_leaf(generic_consumer.snapshot())["Counter"][0])
    after = runtime.scan_stats()
    assert max(seen_waits) <= 36_000
    scans_during = num(during, "scan_count") - num(before, "scan_count")
    assert scans_during >= 0.9 * 100 * (during_at - cut_at)
    assert first >= restored_scan - 20
    record_property("overruns_delta", num(after, "overruns") - num(before, "overruns"))
    record_property("cycle_latency_max_us", after["cycle_latency_max"])
    record_property("outage_s", round(restored_at - cut_at, 1))
    record_property("max_attempt", max(attempts))
    record_property("max_next_retry_ms", max(seen_waits))
    record_property("first_counter_after_restore", first)
    record_property("scan_count_at_restore", restored_scan)


def test_periodic_qos1_retain_with_password_and_private_ca(
    runtime: Runtime, payloads: Path, network: Network, generic_consumer: Consumer
) -> None:
    md5 = program_md5(payloads, "e2e-periodic")
    ref = stage_password(runtime, md5, network.pki.password, 8883)

    def secure(doc: dict[str, object]) -> None:
        doc["connection"] = tls_connection(network, 8883)
        doc["auth"] = {"method": "username_password", "credential_ref": ref}

    deploy_running(runtime, payloads / "e2e-periodic", secure)
    status = wait_status(runtime, lambda s: s["state"] == "connected", 15)
    assert status["broker"] == {"host": "broker", "port": 8883, "tls": True}
    generic_consumer.clear()
    time.sleep(3.6)
    messages = generic_consumer.snapshot()
    leaves = by_leaf(messages)
    assert sorted(leaves) == sorted(ALL_GENERIC)
    assert {(m.qos, m.retain) for m in messages} == {(1, False)}
    for name, expected in GENERIC_CONSTANTS.items():
        assert 3 <= len(leaves[name]) <= 5 and set(map(json.dumps, leaves[name])) == {
            json.dumps(expected)
        }, (name, leaves[name])
    times = [m.at for m in messages if m.topic == PREFIX + "LMax"]
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert all(0.8 < g < 1.2 for g in gaps), gaps
    late = Consumer(network.broker_port, [PREFIX + "#"]).start()
    try:
        assert until(lambda: len({m.topic for m in late.snapshot()}) == len(ALL_GENERIC), 5)
        retained = {m.topic: m for m in late.snapshot() if m.retain}
        assert len(retained) == len(ALL_GENERIC)
        assert generic_value(retained[PREFIX + "LMax"].payload) == GENERIC_CONSTANTS["LMax"]
    finally:
        runtime.stop_plc()
        for leaf in ALL_GENERIC:
            late.publish(PREFIX + leaf, b"", retain=True)
        late.stop()
    credentials = runtime.get("/api/mqtt/credentials")
    assert credentials["active"]["method"] == "username_password"  # type: ignore[index]
    assert network.pki.password not in json.dumps(credentials) + json.dumps(status)


def test_removing_the_credential_stops_the_running_session(
    runtime: Runtime, payloads: Path, network: Network, generic_consumer: Consumer
) -> None:
    md5 = program_md5(payloads, "e2e-generic")
    ref = stage_password(runtime, md5, network.pki.password, 8883)

    def secure(doc: dict[str, object]) -> None:
        doc["connection"] = tls_connection(network, 8883)
        doc["auth"] = {"method": "username_password", "credential_ref": ref}

    deploy_running(runtime, payloads / "e2e-generic", secure)
    wait_status(runtime, lambda s: s["state"] == "connected", 15)
    assert until(lambda: len(generic_consumer.snapshot()) > 0, 5)
    before = runtime.scan_stats()
    status, _ = runtime.request("DELETE", "/api/mqtt/credentials/active")
    assert status == 204
    removed_at = time.monotonic()
    status_doc = wait_status(runtime, lambda s: s["state"] == "credential_error", 7)
    assert error_code(status_doc) == "credential_missing"
    time.sleep(1.0)
    late = [m for m in generic_consumer.snapshot() if m.at > removed_at + 0.5]
    assert late == []
    assert runtime.plc_state() == "STATUS:RUNNING"
    assert int(runtime.scan_stats()["scan_count"]) > num(before, "scan_count")
    assert "event=remove result=destroyed" in runtime.logs()
    assert "session=stopped" in runtime.logs()


def test_wrong_password_is_an_authentication_error_without_the_secret(
    runtime: Runtime, payloads: Path, network: Network
) -> None:
    wrong = WRONG_PASSWORD
    md5 = program_md5(payloads, "e2e-generic")
    ref = stage_password(runtime, md5, wrong, 8883)

    def secure(doc: dict[str, object]) -> None:
        doc["connection"] = tls_connection(network, 8883)
        doc["auth"] = {"method": "username_password", "credential_ref": ref}

    deploy_running(runtime, payloads / "e2e-generic", secure)
    status = wait_status(runtime, lambda s: error_code(s) == "auth_rejected", 20)
    assert status["state"] == "retry_wait"
    assert status["error"]["category"] == "authentication"  # type: ignore[index]
    assert wrong not in json.dumps(status) and wrong not in runtime.logs()
    assert runtime.plc_state() == "STATUS:RUNNING"


@pytest.mark.parametrize(
    ("stem", "expected"),
    [("client-ok", None), ("client-rogue", "client_certificate_rejected")],
)
def test_client_certificate_identity(
    runtime: Runtime, payloads: Path, network: Network, stem: str, expected: str | None
) -> None:
    md5 = program_md5(payloads, "e2e-generic")
    ref = stage_certificate(runtime, network, md5, stem)

    def mtls(doc: dict[str, object]) -> None:
        doc["connection"] = tls_connection(network, 8884)
        doc["auth"] = {"method": "client_certificate", "credential_ref": ref}

    deploy_running(runtime, payloads / "e2e-generic", mtls)
    if expected is None:
        status = wait_status(runtime, lambda s: s["state"] == "connected", 15)
    else:
        status = wait_status(runtime, lambda s: error_code(s) == expected, 20)
        assert status["error"]["category"] == "certificate"  # type: ignore[index]
    key_marker = network.pki.read(f"{stem}.key").decode().splitlines()[1][:40]
    assert key_marker not in runtime.logs() and key_marker not in json.dumps(status)


@pytest.mark.parametrize(
    ("port", "expected"),
    [
        (8884, "client_certificate_rejected"),
        (8885, "broker_certificate_hostname_mismatch"),
        (8886, "broker_certificate_expired"),
        (8887, "broker_certificate_untrusted"),
    ],
)
def test_tls_rejections_never_connect(
    runtime: Runtime, payloads: Path, network: Network, port: int, expected: str
) -> None:
    def anonymous_tls(doc: dict[str, object]) -> None:
        doc["connection"] = tls_connection(network, port)

    deploy_running(runtime, payloads / "e2e-generic", anonymous_tls)
    status = wait_status(runtime, lambda s: error_code(s) == expected, 20)
    assert status["state"] == "retry_wait"
    assert status["error"]["category"] == "certificate"  # type: ignore[index]
    assert status["stats"]["published"] == 0  # type: ignore[index]


# --- Sparkplug -------------------------------------------------------------------


def spb_messages(consumer: Consumer, kind: str) -> list[tuple[Message, wire.Payload]]:
    topic = f"{SPB}{kind}/{NODE}"
    return [(m, wire.payload(m.payload)) for m in consumer.snapshot() if m.topic == topic]


def metric_value(metric: wire.Metric) -> object:
    if metric.is_null:
        return None
    if metric.datatype in (1, 2, 3):
        bits = {1: 8, 2: 16, 3: 32}[metric.datatype]
        raw = int(metric.value) & ((1 << bits) - 1)  # type: ignore[arg-type]
        return raw - (1 << bits) if raw >= 1 << (bits - 1) else raw
    if metric.datatype == 4:
        return wire.signed64(int(metric.value))  # type: ignore[arg-type]
    return metric.value


def test_sparkplug_lifecycle_against_an_independent_host(
    runtime: Runtime, payloads: Path, network: Network, spb_consumer: Consumer, record_property
) -> None:
    deploy_running(runtime, payloads / "e2e-sparkplug")
    assert until(lambda: len(spb_messages(spb_consumer, "NDATA")) >= 20, 10)
    (birth_msg, birth), *_ = spb_messages(spb_consumer, "NBIRTH")
    assert (birth_msg.qos, birth_msg.retain) == (0, False)
    assert birth.seq == 0 and birth.timestamp is not None
    names = [m.name for m in birth.metrics]
    assert names[:2] == ["bdSeq", "Node Control/Rebirth"]
    assert len(names) == 2 + 34
    bd_seq = int(birth.metric("bdSeq").value)  # type: ignore[arg-type]
    assert birth.metric("Node Control/Rebirth").value is False
    first_ndata = min(m.at for m, _ in spb_messages(spb_consumer, "NDATA"))
    assert birth_msg.at <= first_ndata
    for name, (datatype, value, props) in SPARKPLUG_CONSTANTS.items():
        metric = birth.metric(f"instance0.Main.{name}")
        assert metric.datatype == datatype, name
        assert metric_value(metric) == value, (name, metric)
        assert metric.properties == props, (name, metric.properties)
    assert birth.metric("instance0.Main.LongTxt").is_null
    seqs = [p.seq for _, p in spb_messages(spb_consumer, "NDATA")]
    assert seqs == [(i % 256) for i in range(1, len(seqs) + 1)]
    counters: list[int] = []
    for _, data in spb_messages(spb_consumer, "NDATA"):
        names_in = {m.name for m in data.metrics}
        assert not names_in & {f"instance0.Main.{n}" for n in SPARKPLUG_CONSTANTS}
        for metric in data.metrics:
            assert not metric.properties and metric.datatype is not None
            if metric.name == "instance0.Main.Counter":
                counters.append(int(metric_value(metric)))
    assert all(b > a for a, b in itertools.pairwise(counters))
    status = wait_status(runtime, lambda s: s["state"] == "connected", 5)
    assert status["sparkplug"]["bdSeq"] == bd_seq  # type: ignore[index]

    ncmd = f"{SPB}NCMD/{NODE}"
    stats_before = runtime.scan_stats()
    before_at = time.monotonic()
    births_before = len(spb_messages(spb_consumer, "NBIRTH"))
    spb_consumer.publish(ncmd, wire.command([("instance0.Main.Counter", 3, "int_value", 999)]))
    spb_consumer.publish(ncmd, wire.command([("Node Control/Rebirth", 11, "boolean_value", False)]))
    spb_consumer.publish(ncmd, b"\xff\xfe\x00garbage")
    time.sleep(2.5)
    assert len(spb_messages(spb_consumer, "NBIRTH")) == births_before
    spb_consumer.publish(ncmd, wire.command([("Node Control/Rebirth", 11, "boolean_value", True)]))
    assert until(lambda: len(spb_messages(spb_consumer, "NBIRTH")) == births_before + 1, 5)
    rebirth = spb_messages(spb_consumer, "NBIRTH")[-1][1]
    assert rebirth.seq == 0 and int(rebirth.metric("bdSeq").value) == bd_seq  # type: ignore[arg-type]
    assert len(rebirth.metrics) == 36
    flood_start = time.monotonic()
    for _ in range(30):
        spb_consumer.publish(
            ncmd, wire.command([("Node Control/Rebirth", 11, "boolean_value", True)])
        )
        time.sleep(0.05)
    time.sleep(3)
    flood_births = [m for m, _ in spb_messages(spb_consumer, "NBIRTH") if m.at >= flood_start]
    assert 1 <= len(flood_births) <= 3
    gaps = [b.at - a.at for a, b in itertools.pairwise(flood_births)]
    assert all(g >= 1.9 for g in gaps), gaps
    after_cmds = [
        int(metric_value(m))
        for _, data in spb_messages(spb_consumer, "NDATA")
        for m in data.metrics
        if m.name == "instance0.Main.Counter"
    ]
    assert 999 not in after_cmds and all(b > a for a, b in itertools.pairwise(after_cmds))
    stats_after = runtime.scan_stats()
    abuse_s = time.monotonic() - before_at
    scans = num(stats_after, "scan_count") - num(stats_before, "scan_count")
    assert scans >= 0.9 * 100 * abuse_s
    record_property(
        "abuse_overruns_delta", num(stats_after, "overruns") - num(stats_before, "overruns")
    )

    spb_consumer.publish(ncmd, b"\x0a" + b"\xff" * 20_480)
    assert until(lambda: len(spb_messages(spb_consumer, "NDEATH")) == 1, 10)
    _will_msg, will = spb_messages(spb_consumer, "NDEATH")[0]
    assert int(will.metric("bdSeq").value) == bd_seq and will.timestamp is None  # type: ignore[arg-type]
    assert until(
        lambda: spb_messages(spb_consumer, "NBIRTH")[-1][1].metric("bdSeq").value != bd_seq, 15
    )
    new_bd = int(spb_messages(spb_consumer, "NBIRTH")[-1][1].metric("bdSeq").value)  # type: ignore[arg-type]
    assert new_bd != bd_seq
    status = wait_status(runtime, lambda s: s["state"] == "connected", 10)

    stop_at = time.monotonic()
    assert runtime.stop_plc() < 5.0
    assert until(lambda: len(spb_messages(spb_consumer, "NDEATH")) == 2, 5)
    death_msg, death = spb_messages(spb_consumer, "NDEATH")[1]
    assert int(death.metric("bdSeq").value) == new_bd and death.timestamp is not None  # type: ignore[arg-type]
    assert (death_msg.qos, death_msg.retain) == (1, False)
    assert death_msg.at - stop_at < 5.0
    record_property("bd_seq", [bd_seq, new_bd])
    record_property("flood_births", len(flood_births))
    record_property("ndata", len(spb_messages(spb_consumer, "NDATA")))


def test_broker_packet_limit_is_diagnosed(runtime: Runtime, payloads: Path) -> None:
    def small(doc: dict[str, object]) -> None:
        doc["connection"]["host"] = "broker-small"  # type: ignore[index]

    deploy_running(runtime, payloads / "e2e-sparkplug", small)
    status = wait_status(runtime, lambda s: error_code(s) == "broker_packet_limit", 30)
    assert status["error"]["category"] == "limit"  # type: ignore[index]
    assert runtime.plc_state() == "STATUS:RUNNING"


# --- program/mode changes and other protocols --------------------------------------


def test_mode_switch_and_disable_leave_no_publisher(
    runtime: Runtime, payloads: Path, generic_consumer: Consumer, spb_consumer: Consumer
) -> None:
    deploy_running(runtime, payloads / "e2e-generic")
    assert until(lambda: len(generic_consumer.snapshot()) > 10, 10)
    deploy_running(runtime, payloads / "e2e-sparkplug")
    assert until(lambda: len(spb_messages(spb_consumer, "NBIRTH")) >= 1, 10)
    switched = spb_messages(spb_consumer, "NBIRTH")[0][0].at
    time.sleep(2)
    assert [m for m in generic_consumer.snapshot() if m.at > switched] == []
    deploy_running(runtime, payloads / "e2e-none")
    disabled = time.monotonic()
    time.sleep(3)
    assert [m for m in generic_consumer.snapshot() if m.at > disabled] == []
    data_after = [m for m, _ in spb_messages(spb_consumer, "NDATA") if m.at > disabled]
    assert data_after == []
    status = wait_status(runtime, lambda s: s["state"] == "disabled", 5)
    assert status["configured"] is False
    conf = runtime.exec("cat", "plugins.conf")
    assert any(line.startswith("mqtt,") and ",0," in line for line in conf.splitlines())
    assert runtime.exec("ls", "core/src/drivers/plugins/python/mqtt/").count("mqtt.json") == 0


def test_modbus_opcua_and_mqtt_run_together(
    runtime: Runtime, payloads: Path, generic_consumer: Consumer
) -> None:
    deploy_running(runtime, payloads / "e2e-regression")
    assert until(lambda: "Counter" in by_leaf(generic_consumer.snapshot()), 10)
    port = host_port(runtime.name, 5020)
    assert until(lambda: _modbus_ok(port), 15, 0.5)
    first = modbus_read_holding(port, 0)
    time.sleep(0.5)
    second = modbus_read_holding(port, 0)
    assert second > first > 0
    script = (
        "import asyncio\nfrom asyncua import Client\n"
        "import socket\n"
        "async def main():\n"
        "    host = socket.gethostbyname(socket.gethostname())\n"
        "    async with Client(f'opc.tcp://{host}:4840/openplc') as c:\n"
        "        nodes = [n for n in await c.nodes.objects.get_children()\n"
        "                 if (await n.read_browse_name()).Name == 'Counter']\n"
        "        a = await nodes[0].read_value()\n"
        "        await asyncio.sleep(0.5)\n"
        "        b = await nodes[0].read_value()\n"
        "        print(a, b)\n"
        "asyncio.run(main())\n"
    )
    out = ""

    def opcua_ok() -> bool:
        nonlocal out
        out = runtime.exec("venvs/opcua/bin/python", "-c", script).strip()
        return len(out.split()) == 2

    assert until(opcua_ok, 20, 1.0), out
    a, b = (int(x) for x in out.split())
    assert b > a > 0
    assert runtime.stop_plc() < 5.0
    runtime.start_plc()
    assert until(lambda: _modbus_ok(port), 15, 0.5)
    assert until(opcua_ok, 20, 1.0), out
    wait_status(runtime, lambda s: s["state"] == "connected", 10)


def _modbus_ok(port: int) -> bool:
    try:
        return modbus_read_holding(port, 0) > 0
    except (OSError, AssertionError, struct.error):
        return False


# --- secrets ---------------------------------------------------------------------


def test_marker_secrets_appear_nowhere_on_the_device(runtime: Runtime, network: Network) -> None:
    markers = [network.pki.password, WRONG_PASSWORD]
    markers += [base64.b64encode(m.encode()).decode() for m in markers]
    for stem in ("client-ok", "client-rogue"):
        markers.append(network.pki.read(f"{stem}.key").decode().splitlines()[1][:40])
    found = []
    for marker in markers:
        hits = runtime.exec(
            "sh",
            "-c",
            f"grep -rlaF -- '{marker}' /workdir /var/run/runtime /tmp 2>/dev/null || true",
        ).strip()
        if hits:
            found.append((marker[:12], hits))
        if marker in runtime.logs():
            found.append((marker[:12], "docker logs"))
    assert found == []
    snapshot_status, _ = runtime.request("GET", "/api/project-snapshot")
    assert snapshot_status in (200, 404)
    assert "event=stage result=accepted" in runtime.logs()
    assert "event=activate result=committed" in runtime.logs()
