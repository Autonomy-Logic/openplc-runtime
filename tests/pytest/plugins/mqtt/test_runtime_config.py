# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Runtime decoder for conf/mqtt.json against the shared contract fixtures."""

import copy
import json
from pathlib import Path

import pytest
from contract.constants import DEBUG_SIZE, MAX_CONFIG_BYTES, MAX_PAYLOAD_BYTES
from contract.names import IndexToken, NameToken
from contract.runtime_config import (
    CONFIG_ERROR_CODES,
    SPARKPLUG_DATATYPE,
    ConfigError,
    ConfigRejected,
    DebugMapError,
    Leaf,
    MqttRuntimeConfig,
    ProgramMap,
    birth_properties,
    decode_runtime_config,
    parse_debug_map,
    sparkplug_birth_upper_bound,
)

FIXTURES = (
    Path(__file__).resolve().parents[4] / "core/src/drivers/plugins/python/mqtt/contract/fixtures"
)
CONFIG_DIR = FIXTURES / "runtime-config"
CASES = json.loads((CONFIG_DIR / "cases.json").read_text(encoding="utf-8"))
ERROR_CODES = json.loads((FIXTURES / "errors" / "error-codes.json").read_text(encoding="utf-8"))
VALUES = json.loads((FIXTURES / "values" / "value-vectors.json").read_text(encoding="utf-8"))
DEBUG_MAP_RAW = (CONFIG_DIR / CASES["context"]["debugMap"]).read_bytes()
PROGRAM = parse_debug_map(DEBUG_MAP_RAW)
GENERIC = "valid/generic-on-change-anonymous-plaintext.json"
PERIODIC = "valid/generic-periodic-password-private-ca.json"
SPARKPLUG = "valid/sparkplug-client-certificate-all-types.json"
MARKER = "MARKER-VALUE-7f3a"

JsonDoc = dict[str, object]


def _doc(rel: str) -> JsonDoc:
    loaded: JsonDoc = json.loads((CONFIG_DIR / rel).read_text(encoding="utf-8"))
    return loaded


def _raw(doc: JsonDoc) -> bytes:
    return json.dumps(doc).encode("utf-8")


def _rejection(raw: bytes, program: ProgramMap | None = PROGRAM) -> ConfigError:
    with pytest.raises(ConfigRejected) as caught:
        decode_runtime_config(raw, program)
    return caught.value.error


def _big_leaves(base: JsonDoc, count: int, long_names: bool) -> JsonDoc:
    doc = copy.deepcopy(base)
    leaves = []
    for i in range(count):
        destination = f"{i:04d}" + "n" * 508 if long_names else f"openplc/plc-linha1/Inst0/Big/{i}"
        leaves.append(
            {
                "source": {
                    "tokens": [
                        {"role": "instance", "name": "Inst0"},
                        {"role": "variable", "name": "Big"},
                        {"role": "index", "indices": [i]},
                    ],
                    "debug_path": f"INST0.BIG[{i}]",
                },
                "address": {"arr": 1, "elem": i},
                "iec_type": "DINT",
                "size": 4,
                "destination": destination,
            }
        )
    doc["leaves"] = leaves
    return doc


# --- valid fixtures --------------------------------------------------------


@pytest.mark.parametrize("rel", CASES["valid"])
def test_valid_fixture_decodes_against_its_program(rel: str) -> None:
    config = decode_runtime_config((CONFIG_DIR / rel).read_bytes(), PROGRAM)
    assert isinstance(config, MqttRuntimeConfig)
    assert config.program_md5 == CASES["context"]["programMd5"] == PROGRAM.md5
    for leaf in config.leaves:
        entry = PROGRAM.leaves[(leaf.arr, leaf.elem)]
        assert (entry.path, entry.iec_type, entry.size) == (
            leaf.debug_path,
            leaf.iec_type,
            leaf.size,
        )


def test_generic_fixture_is_typed_without_loss() -> None:
    config = decode_runtime_config((CONFIG_DIR / GENERIC).read_bytes(), PROGRAM)
    assert config.mode == "generic" and config.sparkplug is None
    assert config.generic is not None
    assert (config.generic.publish_mode, config.generic.interval_ms) == ("on_change", 100)
    assert (config.generic.qos, config.generic.retain) == (0, False)
    assert config.connection.tls.enabled is False and config.connection.tls.trust is None
    assert config.auth.method == "anonymous" and config.auth.credential_ref is None
    temps = config.leaves[2]
    assert temps.tokens == (
        NameToken("instance", "Inst0"),
        NameToken("variable", "Temps"),
        IndexToken((-2,)),
    )
    assert temps.destination == "openplc/plc-linha1/Inst0/Temps/-2"


def test_periodic_fixture_keeps_private_ca_and_credential_reference() -> None:
    config = decode_runtime_config((CONFIG_DIR / PERIODIC).read_bytes(), PROGRAM)
    assert config.generic is not None
    assert (config.generic.publish_mode, config.generic.interval_ms) == ("periodic", 1000)
    trust = config.connection.tls.trust
    assert trust is not None and trust.source == "pem"
    assert trust.ca_pem is not None and trust.ca_pem.startswith("-----BEGIN CERTIFICATE-----")
    assert config.auth.method == "username_password"
    assert config.auth.credential_ref is not None


def test_sparkplug_fixture_covers_every_supported_type() -> None:
    config = decode_runtime_config((CONFIG_DIR / SPARKPLUG).read_bytes(), PROGRAM)
    assert config.mode == "sparkplug_b" and config.generic is None
    assert config.sparkplug is not None
    assert {leaf.iec_type for leaf in config.leaves} == set(DEBUG_SIZE)
    assert config.auth.credential_ref is not None
    assert config.auth.credential_ref.staging_id is not None
    assert sparkplug_birth_upper_bound(config.leaves) <= MAX_PAYLOAD_BYTES


# --- invalid fixtures ------------------------------------------------------


@pytest.mark.parametrize("case", CASES["invalid"], ids=lambda c: c["file"])
def test_invalid_fixture_is_rejected_at_its_layer(case: dict[str, str]) -> None:
    error = _rejection((CONFIG_DIR / case["file"]).read_bytes())
    assert (error.layer, error.code, error.pointer) == (
        case["layer"],
        case["code"],
        case["pointer"],
    )
    assert error.detail == case.get("detail")
    assert error.category == "configuration"


@pytest.mark.parametrize("gen", CASES["generated"], ids=lambda g: g["id"])
def test_generated_case(gen: dict[str, object]) -> None:
    op = gen["op"]
    if op in ("big-leaves", "big-leaves-long-metric-names"):
        doc = _big_leaves(_doc(str(gen["base"])), int(str(gen["count"])), op != "big-leaves")
        raw = _raw(doc)
    elif op == "pad-to-bytes":
        base = (CONFIG_DIR / GENERIC).read_bytes()
        raw = base + b" " * (int(str(gen["bytes"])) - len(base))
    else:
        depth = int(str(gen["depth"]))
        raw = ("[" * depth + "]" * depth).encode("ascii")
    expect = gen["expect"]
    if expect == "valid":
        assert len(decode_runtime_config(raw, PROGRAM).leaves) == int(str(gen["count"]))
        return
    assert isinstance(expect, dict)
    error = _rejection(raw)
    assert (error.layer, error.code, error.pointer) == (
        expect["layer"],
        expect["code"],
        expect["pointer"],
    )


# --- parse layer -------------------------------------------------------------


def test_size_is_checked_before_decoding() -> None:
    error = _rejection(b"\xff" * (MAX_CONFIG_BYTES + 1))
    assert error.code == "config_too_large"


def test_depth_ignores_brackets_inside_strings() -> None:
    doc = _doc(GENERIC)
    doc["connection"] = dict(doc["connection"], client_id="[" * 100)  # type: ignore[arg-type]
    assert _rejection(_raw(doc)).pointer == "/connection/client_id"


def test_depth_at_the_limit_is_parsed() -> None:
    error = _rejection(("[" * 32 + "]" * 32).encode("ascii"))
    assert (error.code, error.pointer) == ("config_schema", "")


def test_nested_duplicate_key_points_at_the_member() -> None:
    raw = (
        (CONFIG_DIR / GENERIC)
        .read_text(encoding="utf-8")
        .replace('"port": 1883,', '"port": 1883, "port": 1884,')
    )
    error = _rejection(raw.encode("utf-8"))
    assert (error.code, error.pointer) == ("config_duplicate_key", "/connection/port")


@pytest.mark.parametrize("raw", [b"{", b"NaN", b'{"a": Infinity}', b"\xc3\x28", b"\xef\xbb\xbf{}"])
def test_malformed_documents_are_schema_errors(raw: bytes) -> None:
    error = _rejection(raw)
    assert (error.layer, error.code, error.pointer) == ("parse", "config_schema", "")


# --- schema layer --------------------------------------------------------------


@pytest.mark.parametrize("version", [2, 0, "1", True, 1.0, None])
def test_unknown_format_version(version: object) -> None:
    doc = _doc(GENERIC)
    doc["format_version"] = version
    assert _rejection(_raw(doc)).code == "config_unknown_version"


def test_missing_format_version() -> None:
    doc = _doc(GENERIC)
    del doc["format_version"]
    assert _rejection(_raw(doc)).code == "config_unknown_version"


def test_contract_is_checked_before_anything_else() -> None:
    assert _rejection(b"[]").pointer == ""
    assert _rejection(b"{}").code == "config_unknown_contract"


@pytest.mark.parametrize(
    "path, value, pointer",
    [
        (("mode",), [], "/mode"),
        (("connection", "port"), 0, "/connection/port"),
        (("connection", "port"), True, "/connection/port"),
        (("connection", "port"), 1883.0, "/connection/port"),
        (("connection", "host"), "bad host", "/connection/host"),
        (("generic", "retain"), 0, "/generic/retain"),
        (("generic", "publish_mode"), "burst", "/generic/publish_mode"),
        (("program", "md5"), "3F2C9D0E8B7A6F5E4D3C2B1A09F8E7D6", "/program/md5"),
    ],
)
def test_schema_type_and_range_errors(path: tuple[str, ...], value: object, pointer: str) -> None:
    doc = _doc(GENERIC)
    target: dict[str, object] = doc
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value
    error = _rejection(_raw(doc))
    assert (error.code, error.pointer) == ("config_schema", pointer)


def test_index_token_bounds() -> None:
    doc = _doc(GENERIC)
    leaves = doc["leaves"]
    assert isinstance(leaves, list)
    leaves[2]["source"]["tokens"][2]["indices"] = [2**31]
    assert _rejection(_raw(doc)).pointer == "/leaves/2/source/tokens/2/indices/0"
    leaves[2]["source"]["tokens"][2]["indices"] = list(range(9))
    assert _rejection(_raw(doc)).pointer == "/leaves/2/source/tokens/2/indices"


def test_multidimensional_index_token_is_preserved() -> None:
    doc = _doc(GENERIC)
    leaves = doc["leaves"]
    assert isinstance(leaves, list)
    leaves[2]["source"]["tokens"][2]["indices"] = [-2, 0, 7]
    config = decode_runtime_config(_raw(doc), PROGRAM)
    assert config.leaves[2].tokens[2] == IndexToken((-2, 0, 7))


# --- semantic layer --------------------------------------------------------------


def test_program_checks_are_skipped_without_a_program() -> None:
    raw = (CONFIG_DIR / "invalid/program-mismatch.json").read_bytes()
    assert decode_runtime_config(raw, None).program_md5 != PROGRAM.md5
    duplicate = (CONFIG_DIR / "invalid/duplicate-address.json").read_bytes()
    assert _rejection(duplicate, None).code == "config_leaf_duplicate"


def test_size_differing_from_the_program_is_an_address_error() -> None:
    debug = json.loads(DEBUG_MAP_RAW)
    for entry in debug["leaves"]:
        if entry["path"] == "INST0.MOTOR1.SPEED":
            entry["size"] = 8
    program = parse_debug_map(json.dumps(debug).encode("utf-8"))
    error = _rejection((CONFIG_DIR / GENERIC).read_bytes(), program)
    assert (error.code, error.pointer) == ("config_address_invalid", "/leaves/0/size")


def test_birth_limit_is_not_checked_in_generic_mode() -> None:
    doc = _big_leaves(_doc(GENERIC), 1000, False)
    assert len(decode_runtime_config(_raw(doc), PROGRAM).leaves) == 1000


# --- sanitized errors ----------------------------------------------------------


def test_error_payload_matches_the_api_error_shape() -> None:
    error = _rejection((CONFIG_DIR / "invalid/unknown-version.json").read_bytes())
    payload = error.to_api()
    assert set(payload) == {"code", "category", "message", "pointer"}
    assert len(payload["message"]) <= 512


def test_decoder_codes_are_registered_configuration_codes() -> None:
    registered = {c for c, cat in ERROR_CODES["codes"].items() if cat == "configuration"}
    assert CONFIG_ERROR_CODES <= registered
    assert registered - CONFIG_ERROR_CODES == {"config_multiple_enabled"}


@pytest.mark.parametrize(
    "path",
    [("connection", "host"), ("connection", "client_id"), ("sparkplug", "group_id"), ("mode",)],
)
def test_rejections_never_echo_configuration_values(path: tuple[str, ...]) -> None:
    doc = _doc(SPARKPLUG)
    target: dict[str, object] = doc
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = f"/{MARKER}/"
    error = _rejection(_raw(doc))
    assert MARKER not in json.dumps(error.to_api())
    assert error.detail is None or MARKER not in error.detail


# --- NBIRTH bound ---------------------------------------------------------------


def _leaf(iec_type: str, destination: str, elem: int = 0) -> Leaf:
    tokens = (NameToken("instance", "I"), NameToken("variable", "V"))
    return Leaf(tokens, "I.V", 0, elem, iec_type, DEBUG_SIZE[iec_type], destination)


def _single_metric_bound(iec_type: str, name: str) -> int:
    return sparkplug_birth_upper_bound([_leaf(iec_type, name)]) - sparkplug_birth_upper_bound([])


def _varint_len(value: int) -> int:
    length = 1
    while value >= 0x80:
        value >>= 7
        length += 1
    return length


def _framed_vector(vector: dict[str, object]) -> int:
    sparkplug = vector["sparkplug"]
    assert isinstance(sparkplug, dict)
    # Vectors omit the metric timestamp; NBIRTH carries one (uint64, at most 10 bytes).
    body = len(bytes.fromhex(sparkplug["birthMetric"])) + 1 + 10
    return 1 + _varint_len(body) + body


def test_birth_bound_covers_every_serialized_value_vector() -> None:
    for vector in VALUES["vectors"]:
        assert _framed_vector(vector) <= _single_metric_bound(vector["iecType"], "v"), vector["id"]


@pytest.mark.parametrize(
    "iec_type", ["BOOL", "SINT", "DINT", "LINT", "ULINT", "REAL", "LREAL", "TIME"]
)
def test_birth_bound_is_reached_by_worst_case_vectors(iec_type: str) -> None:
    framed = [_framed_vector(v) for v in VALUES["vectors"] if v["iecType"] == iec_type]
    assert max(framed) == _single_metric_bound(iec_type, "v")


def test_birth_properties_follow_the_contract() -> None:
    for vector in VALUES["vectors"]:
        assert dict(birth_properties(vector["iecType"])) == vector["sparkplug"]["properties"]
        assert SPARKPLUG_DATATYPE[vector["iecType"]] == vector["sparkplug"]["datatype"]


def test_birth_bound_counts_long_names_and_rejects_over_the_limit() -> None:
    names = [f"{i:04d}" + "n" * 508 for i in range(1000)]
    leaves = [_leaf("DINT", name, i) for i, name in enumerate(names)]
    assert sparkplug_birth_upper_bound(leaves) > MAX_PAYLOAD_BYTES
    assert sparkplug_birth_upper_bound(leaves[:400]) <= MAX_PAYLOAD_BYTES


# --- debug map -------------------------------------------------------------------


def test_debug_map_fixture_is_parsed() -> None:
    assert PROGRAM.md5 == "3f2c9d0e8b7a6f5e4d3c2b1a09f8e7d6"
    assert PROGRAM.leaves[(0, 24)].path == "INST0.TEMPS[-2]"
    assert len(PROGRAM.leaves) == 29 + 1001


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=1),
        lambda d: d.update(md5="x"),
        lambda d: d.update(leaves={}),
        lambda d: d["leaves"].append(dict(d["leaves"][0])),
        lambda d: d["leaves"][0].update(size="1"),
        lambda d: d["leaves"][0].update(arrayIdx=True),
    ],
)
def test_malformed_debug_maps_are_refused(mutate: object) -> None:
    debug = json.loads(DEBUG_MAP_RAW)
    assert callable(mutate)
    mutate(debug)
    with pytest.raises(DebugMapError):
        parse_debug_map(json.dumps(debug).encode("utf-8"))


def test_oversized_debug_map_is_refused_before_parsing() -> None:
    with pytest.raises(DebugMapError):
        parse_debug_map(b" " * (10 * 1024 * 1024 + 1))
