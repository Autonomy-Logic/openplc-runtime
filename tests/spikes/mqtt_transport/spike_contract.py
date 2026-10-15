# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Schema-level evidence for the shared fixtures: every case fails or passes at the layer it claims."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import TypeAlias

import pytest
import sparkplug_b_pb2 as pb
from jsonschema import Draft202012Validator

CONTRACT = Path(os.environ.get("SPIKE_CONTRACT", "/contract"))
FIXTURES = CONTRACT / "fixtures"
sys.path.insert(0, str(CONTRACT))

import names

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

CONFIG_SCHEMA = json.loads((FIXTURES / "schema" / "mqtt-runtime-config.v1.schema.json").read_text())
API_SCHEMA = json.loads((FIXTURES / "schema" / "mqtt-api.v1.schema.json").read_text())
CASES = json.loads((FIXTURES / "runtime-config" / "cases.json").read_text())
API_CASES = json.loads((FIXTURES / "api" / "cases.json").read_text())["cases"]
CONFIG = Draft202012Validator(CONFIG_SCHEMA)
MAX_PAYLOAD = 262_144
MAX_CONFIG_BYTES = 4_194_304


def load(rel: str) -> dict[str, JsonValue]:
    return json.loads((FIXTURES / "runtime-config" / rel).read_text())


def reject_duplicates(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("config_duplicate_key")
    return dict(pairs)


def name_errors(doc: dict[str, JsonValue]) -> list[tuple[str, str]]:
    """(pointer, detail) for every name-rule violation, mirroring what producer and runtime must report."""
    out: list[tuple[str, str]] = []
    leaves = doc["leaves"]
    assert isinstance(leaves, list)
    mode = doc["mode"]
    destinations = [str(leaf["destination"]) for leaf in leaves]  # type: ignore[index]
    validate = names.validate_topic if mode == "generic" else names.validate_metric_name
    for i, destination in enumerate(destinations):
        error = validate(destination)
        if error:
            out.append((f"/leaves/{i}/destination", error))
    for conflict in names.find_destination_conflicts(mode, destinations):  # type: ignore[arg-type]
        out.append((f"/leaves/{conflict.second}/destination", conflict.code))
    if mode == "sparkplug_b":
        section = doc["sparkplug"]
        assert isinstance(section, dict)
        for key in ("group_id", "edge_node_id"):
            error = names.validate_node_id(str(section[key]))
            if error:
                out.append((f"/sparkplug/{key}", error))
    return out


def big_leaves(base: dict[str, JsonValue], count: int, long_names: bool) -> dict[str, JsonValue]:
    doc = copy.deepcopy(base)
    leaves: list[JsonValue] = []
    for i in range(count):
        destination = f"openplc/plc-linha1/Inst0/Big/{i}"
        if long_names:
            destination = f"{i:04d}" + "n" * 508
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


def birth_size(doc: dict[str, JsonValue]) -> int:
    payload = pb.Payload()
    payload.timestamp = 2**41
    payload.seq = 0
    for name, datatype, value in (("bdSeq", 4, 255), ("Node Control/Rebirth", 11, False)):
        m = payload.metrics.add()
        m.name = name
        m.timestamp = 2**41
        m.datatype = datatype
        if datatype == 4:
            m.long_value = value
        else:
            m.boolean_value = bool(value)
    for leaf in doc["leaves"]:  # type: ignore[union-attr]
        m = payload.metrics.add()
        m.name = str(leaf["destination"])  # type: ignore[index]
        m.timestamp = 2**41
        m.datatype = 3
        for key, text in (("iecType", "DINT"), ("encoding", "native")):
            m.properties.keys.append(key)
            pv = m.properties.values.add()
            pv.type = 12
            pv.string_value = text
        m.int_value = 0xFFFFFFFF
    return payload.ByteSize()


def test_schemas_are_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(CONFIG_SCHEMA)
    Draft202012Validator.check_schema(API_SCHEMA)


def test_every_fixture_file_is_listed() -> None:
    listed = set(CASES["valid"]) | {c["file"] for c in CASES["invalid"]}
    on_disk = {
        str(p.relative_to(FIXTURES / "runtime-config"))
        for p in (FIXTURES / "runtime-config").rglob("*.json")
        if p.parent.name in ("valid", "invalid")
    }
    assert listed == on_disk
    api_on_disk = {p.name for p in (FIXTURES / "api").glob("*.json")} - {"cases.json"}
    assert {c["file"] for c in API_CASES} == api_on_disk


@pytest.mark.parametrize("rel", CASES["valid"])
def test_valid_configs_pass_every_layer(rel: str) -> None:
    text = (FIXTURES / "runtime-config" / rel).read_text()
    doc = json.loads(text, object_pairs_hook=reject_duplicates)
    assert not list(CONFIG.iter_errors(doc))
    assert name_errors(doc) == []
    assert doc["program"]["md5"] == CASES["context"]["programMd5"]  # type: ignore[index]
    debug = load(CASES["context"]["debugMap"])
    leaves = {(l["arrayIdx"], l["elemIdx"]): l for l in debug["leaves"]}  # type: ignore[union-attr,index]
    for leaf in doc["leaves"]:  # type: ignore[union-attr]
        entry = leaves[(leaf["address"]["arr"], leaf["address"]["elem"])]  # type: ignore[index]
        assert entry["path"] == leaf["source"]["debug_path"]  # type: ignore[index]
        assert entry["type"] == leaf["iec_type"] and entry["size"] == leaf["size"]  # type: ignore[index]
    if doc["mode"] == "sparkplug_b":
        assert birth_size(doc) <= MAX_PAYLOAD


@pytest.mark.parametrize("case", CASES["invalid"], ids=lambda c: c["file"])
def test_invalid_configs_fail_at_their_layer(case: dict[str, str]) -> None:
    text = (FIXTURES / "runtime-config" / case["file"]).read_text()
    if case["layer"] == "parse":
        with pytest.raises(ValueError, match=case["code"]):
            json.loads(text, object_pairs_hook=reject_duplicates)
        assert not list(CONFIG.iter_errors(json.loads(text))), "a plain parser hides the duplicate"
        return
    doc = json.loads(text, object_pairs_hook=reject_duplicates)
    schema_errors = list(CONFIG.iter_errors(doc))
    if case["layer"] == "schema":
        assert schema_errors, "schema must reject"
        pointers = {"/" + "/".join(str(p) for p in e.absolute_path) for e in schema_errors}
        top_level = case["pointer"].count("/") == 1
        assert any(
            (p == "/" and top_level)
            or (p != "/" and (case["pointer"].startswith(p) or p.startswith(case["pointer"])))
            for p in pointers
        ), pointers
        return
    assert (
        not schema_errors
    ), "names/semantic cases must be schema-valid so the next layer is exercised"
    found = name_errors(doc)
    if case["layer"] == "names":
        assert (case["pointer"], case["detail"]) in found
    else:
        assert found == []


@pytest.mark.parametrize("gen", CASES["generated"], ids=lambda g: g["id"])
def test_generated_cases(gen: dict[str, JsonValue]) -> None:
    op = gen["op"]
    if op in ("big-leaves", "big-leaves-long-metric-names"):
        doc = big_leaves(load(str(gen["base"])), int(gen["count"]), op == "big-leaves-long-metric-names")  # type: ignore[arg-type]
        errors = list(CONFIG.iter_errors(doc))
        if gen["expect"] == "valid":
            assert not errors and name_errors(doc) == []
        elif gen["expect"]["layer"] == "schema":  # type: ignore[index]
            assert any(e.validator == "maxItems" for e in errors)
        else:
            assert not errors and name_errors(doc) == []
            assert birth_size(doc) > MAX_PAYLOAD
    elif op == "pad-to-bytes":
        base = (
            FIXTURES / "runtime-config" / "valid" / "generic-on-change-anonymous-plaintext.json"
        ).read_text()
        padded = base + " " * (int(gen["bytes"]) - len(base.encode()))  # type: ignore[arg-type]
        assert len(padded.encode()) == MAX_CONFIG_BYTES + 1
        assert not list(
            CONFIG.iter_errors(json.loads(padded))
        ), "size must be checked before parsing"
    elif op == "nest-arrays":
        depth = int(gen["depth"])  # type: ignore[arg-type]
        assert list(CONFIG.iter_errors(json.loads("[" * depth + "]" * depth)))


@pytest.mark.parametrize("case", API_CASES, ids=lambda c: c["file"])
def test_api_fixtures(case: dict[str, str | bool]) -> None:
    schema = {
        "$schema": API_SCHEMA["$schema"],
        "$defs": API_SCHEMA["$defs"],
        "$ref": f"#/$defs/{case['def']}",
    }
    doc = json.loads((FIXTURES / "api" / str(case["file"])).read_text())
    errors = list(Draft202012Validator(schema).iter_errors(doc))
    assert (not errors) == case["valid"], [e.message for e in errors]
