# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Integrity of the fixtures shared byte-for-byte with the Editor/Web producers.

Schema-layer evidence (jsonschema) lives in tests/spikes/mqtt_transport/spike_contract.py;
this suite runs with the standard library only.
"""

import hashlib
import json
from pathlib import Path
from typing import TypeAlias

import pytest
from contract.names import (
    find_destination_conflicts,
    validate_metric_name,
    validate_node_id,
    validate_topic,
)

FIXTURES = (
    Path(__file__).resolve().parents[4] / "core/src/drivers/plugins/python/mqtt/contract/fixtures"
)
CONFIG_DIR = FIXTURES / "runtime-config"
CASES = json.loads((CONFIG_DIR / "cases.json").read_text(encoding="utf-8"))
VALUES = json.loads((FIXTURES / "values" / "value-vectors.json").read_text(encoding="utf-8"))
SECRET_MARKERS = ("MARKER-PW", "PRIVATE KEY")
INTENTIONAL_SECRET_FIXTURES = {"api/invalid-credentials-with-password.json"}

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def _manifest() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in (FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        entries[rel] = digest
    return entries


def _reject_duplicates(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("config_duplicate_key")
    return dict(pairs)


def _name_errors(doc: dict[str, JsonValue]) -> list[tuple[str, str]]:
    leaves = doc["leaves"]
    assert isinstance(leaves, list)
    destinations = []
    for leaf in leaves:
        assert isinstance(leaf, dict)
        destinations.append(str(leaf["destination"]))
    generic = doc["mode"] == "generic"
    out: list[tuple[str, str]] = []
    for i, destination in enumerate(destinations):
        error = validate_topic(destination) if generic else validate_metric_name(destination)
        if error:
            out.append((f"/leaves/{i}/destination", error))
    for c in find_destination_conflicts("generic" if generic else "sparkplug_b", destinations):
        out.append((f"/leaves/{c.second}/destination", c.code))
    section = doc.get("sparkplug")
    if isinstance(section, dict):
        for key in ("group_id", "edge_node_id"):
            error = validate_node_id(str(section[key]))
            if error:
                out.append((f"/sparkplug/{key}", error))
    return out


def test_manifest_matches_every_file() -> None:
    manifest = _manifest()
    on_disk = {
        p.relative_to(FIXTURES).as_posix()
        for p in FIXTURES.rglob("*")
        if p.is_file() and p.name != "MANIFEST.sha256"
    }
    assert set(manifest) == on_disk
    for rel, digest in manifest.items():
        assert hashlib.sha256((FIXTURES / rel).read_bytes()).hexdigest() == digest, rel


def test_cases_list_every_config_fixture() -> None:
    listed = set(CASES["valid"]) | {c["file"] for c in CASES["invalid"]}
    on_disk = {p.relative_to(CONFIG_DIR).as_posix() for p in CONFIG_DIR.glob("*/*.json")}
    assert listed == on_disk


def test_no_secret_material_outside_the_intentional_negative_fixture() -> None:
    for path in FIXTURES.rglob("*.json"):
        rel = path.relative_to(FIXTURES).as_posix()
        text = path.read_text(encoding="utf-8")
        leaked = [m for m in SECRET_MARKERS if m in text]
        assert (rel in INTENTIONAL_SECRET_FIXTURES) == bool(leaked), rel


def test_only_the_duplicate_key_fixture_has_duplicate_keys() -> None:
    for path in CONFIG_DIR.glob("*/*.json"):
        text = path.read_text(encoding="utf-8")
        if path.name == "duplicate-key.json":
            with pytest.raises(ValueError, match="config_duplicate_key"):
                json.loads(text, object_pairs_hook=_reject_duplicates)
        else:
            json.loads(text, object_pairs_hook=_reject_duplicates)


@pytest.mark.parametrize("rel", CASES["valid"])
def test_valid_configs_have_no_name_errors(rel: str) -> None:
    assert _name_errors(json.loads((CONFIG_DIR / rel).read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize(
    "case",
    [c for c in CASES["invalid"] if c["layer"] in ("names", "semantic")],
    ids=lambda c: c["file"],
)
def test_name_layer_cases(case: dict[str, str]) -> None:
    found = _name_errors(json.loads((CONFIG_DIR / case["file"]).read_text(encoding="utf-8")))
    if case["layer"] == "names":
        assert (case["pointer"], case["detail"]) in found
    else:
        assert found == []


def test_value_vectors_are_consistent() -> None:
    vectors = VALUES["vectors"]
    assert len({v["id"] for v in vectors}) == len(vectors)
    for v in vectors:
        envelope = json.loads(v["generic"])
        is_null = envelope["value"] is None
        assert is_null == v["sparkplug"]["isNull"] == v["id"].endswith("-null"), v["id"]
        if v["iecType"] in ("LINT", "ULINT", "LWORD", "TIME") and not is_null:
            assert isinstance(envelope["value"], str), v["id"]


def test_constants_match_the_shared_capabilities_and_error_codes() -> None:
    from contract import constants

    capabilities = json.loads((FIXTURES / "api" / "capabilities.json").read_text(encoding="utf-8"))
    assert capabilities["limits"] == {
        "maxLeaves": constants.MAX_LEAVES,
        "maxNameBytes": constants.MAX_NAME_BYTES,
        "maxPayloadBytes": constants.MAX_PAYLOAD_BYTES,
        "maxConfigBytes": constants.MAX_CONFIG_BYTES,
    }
    assert capabilities["credentials"]["stagingTtlSeconds"] == constants.STAGING_TTL_S
    assert capabilities["credentials"]["maxStaged"] == constants.MAX_STAGED
    assert capabilities["runtimeConfig"] == {
        "contract": constants.RUNTIME_CONFIG_CONTRACT,
        "formatVersions": [constants.RUNTIME_CONFIG_FORMAT_VERSION],
    }
    errors = json.loads((FIXTURES / "errors" / "error-codes.json").read_text(encoding="utf-8"))
    used = {c["code"] for c in CASES["invalid"]} | {
        g["expect"]["code"] for g in CASES["generated"] if g["expect"] != "valid"
    }
    assert used <= {
        code for code, category in errors["codes"].items() if category == "configuration"
    }
    assert set(errors["codes"].values()) <= set(errors["categories"])
