# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Generates fixtures/runtime-config/: debug map, valid and invalid conf/mqtt.json, and cases.json."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import TypeAlias

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
Obj = dict[str, JsonValue]

MD5 = "3f2c9d0e8b7a6f5e4d3c2b1a09f8e7d6"
OTHER_MD5 = "00000000000000000000000000000000"
TAG = {
    "BOOL": 0,
    "SINT": 1,
    "USINT": 2,
    "INT": 3,
    "UINT": 4,
    "DINT": 5,
    "UDINT": 6,
    "LINT": 7,
    "ULINT": 8,
    "REAL": 9,
    "LREAL": 10,
    "BYTE": 11,
    "WORD": 12,
    "DWORD": 13,
    "LWORD": 14,
    "TIME": 15,
    "DATE": 16,
    "TOD": 17,
    "DT": 18,
    "STRING": 19,
    "WSTRING": 20,
}
SIZE = {
    "BOOL": 1,
    "SINT": 1,
    "USINT": 1,
    "BYTE": 1,
    "INT": 2,
    "UINT": 2,
    "WORD": 2,
    "DINT": 4,
    "UDINT": 4,
    "DWORD": 4,
    "REAL": 4,
    "LINT": 8,
    "ULINT": 8,
    "LWORD": 8,
    "LREAL": 8,
    "TIME": 8,
    "DATE": 8,
    "TOD": 8,
    "DT": 8,
    "STRING": 127,
    "WSTRING": 253,
}
SCALARS = list(TAG)
BIG_COUNT = 1001
CA_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBfixtureONLYnotAvalidCertificateButShapedLikeOneForSchemaTests\n"
    "-----END CERTIFICATE-----\n"
)


def name(role: str, value: str) -> Obj:
    return {"role": role, "name": value}


def index(*values: int) -> Obj:
    return {"role": "index", "indices": list(values)}


def debug_map() -> Obj:
    leaves: list[JsonValue] = []
    for elem, iec in enumerate(SCALARS):
        leaves.append(
            {
                "arrayIdx": 0,
                "elemIdx": elem,
                "path": f"INST0.V_{iec}",
                "type": iec,
                "size": SIZE[iec],
            }
        )
    base = len(SCALARS)
    for i, (path, iec) in enumerate(
        [
            ("INST0.MOTOR1.SPEED", "REAL"),
            ("INST0.MOTOR1.RUNNING", "BOOL"),
            ("INST0.MOTOR2.SPEED", "REAL"),
            ("INST0.TEMPS[-2]", "INT"),
            ("INST0.TEMPS[-1]", "INT"),
            ("INST0.TEMPS[0]", "INT"),
            ("INST0.TIMER1.ET", "TIME"),
            ("INST0.TIMER1.Q", "BOOL"),
        ]
    ):
        leaves.append(
            {"arrayIdx": 0, "elemIdx": base + i, "path": path, "type": iec, "size": SIZE[iec]}
        )
    for i in range(BIG_COUNT):
        leaves.append(
            {"arrayIdx": 1, "elemIdx": i, "path": f"INST0.BIG[{i}]", "type": "DINT", "size": 4}
        )
    return {
        "version": 2,
        "md5": MD5,
        "typeTags": dict(TAG),
        "arrays": [{"index": 0, "count": base + 8}, {"index": 1, "count": BIG_COUNT}],
        "leaves": leaves,
    }


def leaf(
    tokens: list[JsonValue], path: str, arr: int, elem: int, iec: str, destination: str
) -> Obj:
    return {
        "source": {"tokens": tokens, "debug_path": path},
        "address": {"arr": arr, "elem": elem},
        "iec_type": iec,
        "size": SIZE[iec],
        "destination": destination,
    }


def base(mode: str, auth: Obj, tls: Obj, leaves: list[JsonValue], section: Obj) -> Obj:
    doc: Obj = {
        "contract": "openplc-mqtt-runtime-config",
        "format_version": 1,
        "enabled": True,
        "mode": mode,
        "program": {"md5": MD5},
        "connection": {
            "host": "broker.example.net",
            "port": 8883 if tls["enabled"] else 1883,
            "client_id": "plc-linha1",
            "tls": tls,
        },
        "auth": auth,
    }
    doc["generic" if mode == "generic" else "sparkplug"] = section
    doc["leaves"] = leaves
    return doc


def generic_leaves() -> list[JsonValue]:
    p = "openplc/plc-linha1/Inst0"
    n = len(SCALARS)
    return [
        leaf(
            [name("instance", "Inst0"), name("variable", "Motor1"), name("field", "Speed")],
            "INST0.MOTOR1.SPEED",
            0,
            n,
            "REAL",
            f"{p}/Motor1/Speed",
        ),
        leaf(
            [name("instance", "Inst0"), name("variable", "Motor2"), name("field", "Speed")],
            "INST0.MOTOR2.SPEED",
            0,
            n + 2,
            "REAL",
            f"{p}/Motor2/Speed",
        ),
        leaf(
            [name("instance", "Inst0"), name("variable", "Temps"), index(-2)],
            "INST0.TEMPS[-2]",
            0,
            n + 3,
            "INT",
            f"{p}/Temps/-2",
        ),
        leaf(
            [name("instance", "Inst0"), name("variable", "Temps"), index(0)],
            "INST0.TEMPS[0]",
            0,
            n + 5,
            "INT",
            f"{p}/Temps/0",
        ),
        leaf(
            [name("instance", "Inst0"), name("variable", "Timer1"), name("field", "ET")],
            "INST0.TIMER1.ET",
            0,
            n + 6,
            "TIME",
            f"{p}/Timer1/ET",
        ),
        leaf(
            [name("instance", "Inst0"), name("variable", "V_LINT")],
            "INST0.V_LINT",
            0,
            SCALARS.index("LINT"),
            "LINT",
            f"{p}/V_LINT",
        ),
    ]


def sparkplug_leaves() -> list[JsonValue]:
    return [
        leaf(
            [name("instance", "Inst0"), name("variable", f"V_{iec}")],
            f"INST0.V_{iec}",
            0,
            i,
            iec,
            f"Inst0.V_{iec}",
        )
        for i, iec in enumerate(SCALARS)
    ]


CRED = {"id": "6f1c2a4e-8d3b-4c5a-9e7f-0a1b2c3d4e5f", "revision": 3, "staging_id": None}
STAGED = {
    "id": "6f1c2a4e-8d3b-4c5a-9e7f-0a1b2c3d4e5f",
    "revision": 4,
    "staging_id": "0d9e8f7a-6b5c-4d3e-8f1a-2b3c4d5e6f70",
}


def valid() -> dict[str, Obj]:
    plain = {"enabled": False}
    tls_system = {"enabled": True, "trust": {"source": "system"}}
    tls_pem = {"enabled": True, "trust": {"source": "pem", "ca_pem": CA_PEM}}
    return {
        "generic-on-change-anonymous-plaintext": base(
            "generic",
            {"method": "anonymous"},
            plain,
            generic_leaves(),
            {"publish_mode": "on_change", "sample_interval_ms": 100, "qos": 0, "retain": False},
        ),
        "generic-periodic-password-private-ca": base(
            "generic",
            {"method": "username_password", "credential_ref": dict(CRED)},
            tls_pem,
            generic_leaves(),
            {"publish_mode": "periodic", "publish_interval_ms": 1000, "qos": 1, "retain": True},
        ),
        "sparkplug-client-certificate-all-types": base(
            "sparkplug_b",
            {"method": "client_certificate", "credential_ref": dict(STAGED)},
            tls_system,
            sparkplug_leaves(),
            {"group_id": "Plant 1", "edge_node_id": "plc-linha1", "sample_interval_ms": 100},
        ),
    }


def mutate(doc: Obj, pointer: str, value: JsonValue, delete: bool = False) -> Obj:
    out = copy.deepcopy(doc)
    parts = pointer.strip("/").split("/")
    target: JsonValue = out
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]  # type: ignore[index]
    last = parts[-1]
    if isinstance(target, list):
        if delete:
            del target[int(last)]
        else:
            target[int(last)] = value
    elif isinstance(target, dict):
        if delete:
            del target[last]
        else:
            target[last] = value
    return out


def invalid(v: dict[str, Obj]) -> list[tuple[str, Obj, str, str, str, str]]:
    g = v["generic-on-change-anonymous-plaintext"]
    pw = v["generic-periodic-password-private-ca"]
    s = v["sparkplug-client-certificate-all-types"]
    dup_topic = mutate(g, "/leaves/1/destination", g["leaves"][0]["destination"])  # type: ignore[index]
    return [
        (
            "unknown-contract",
            mutate(g, "/contract", "openplc-mqtt-runtime"),
            "schema",
            "config_unknown_contract",
            "/contract",
            "",
        ),
        (
            "unknown-version",
            mutate(g, "/format_version", 2),
            "schema",
            "config_unknown_version",
            "/format_version",
            "",
        ),
        ("enabled-false", mutate(g, "/enabled", False), "schema", "config_schema", "/enabled", ""),
        ("unknown-field", mutate(g, "/extra", 1), "schema", "config_schema", "/extra", ""),
        (
            "mode-section-mismatch",
            mutate(g, "/mode", "sparkplug_b"),
            "schema",
            "config_schema",
            "/mode",
            "",
        ),
        (
            "both-sections",
            mutate(g, "/sparkplug", s["sparkplug"]),
            "schema",
            "config_schema",
            "/sparkplug",
            "",
        ),
        (
            "client-certificate-without-tls",
            mutate(mutate(s, "/connection/tls", {"enabled": False}), "/connection/port", 1883),
            "schema",
            "config_tls_required",
            "/connection/tls/enabled",
            "",
        ),
        (
            "trust-on-plaintext",
            mutate(g, "/connection/tls/trust", {"source": "system"}),
            "schema",
            "config_schema",
            "/connection/tls/trust",
            "",
        ),
        (
            "tls-without-trust",
            mutate(g, "/connection/tls/enabled", True),
            "schema",
            "config_schema",
            "/connection/tls",
            "",
        ),
        (
            "interval-below-minimum",
            mutate(g, "/generic/sample_interval_ms", 99),
            "schema",
            "config_schema",
            "/generic/sample_interval_ms",
            "",
        ),
        (
            "interval-above-maximum",
            mutate(pw, "/generic/publish_interval_ms", 60001),
            "schema",
            "config_schema",
            "/generic/publish_interval_ms",
            "",
        ),
        ("qos-2", mutate(g, "/generic/qos", 2), "schema", "config_schema", "/generic/qos", ""),
        ("no-leaves", mutate(g, "/leaves", []), "schema", "config_no_leaves", "/leaves", ""),
        (
            "size-mismatch",
            mutate(g, "/leaves/0/size", 8),
            "schema",
            "config_size_mismatch",
            "/leaves/0/size",
            "",
        ),
        (
            "unsupported-type",
            mutate(g, "/leaves/0/iec_type", "MOTOR_T"),
            "schema",
            "config_type_unsupported",
            "/leaves/0/iec_type",
            "",
        ),
        (
            "credential-ref-missing",
            mutate(pw, "/auth", {"method": "username_password"}),
            "schema",
            "config_schema",
            "/auth",
            "",
        ),
        (
            "arr-out-of-range",
            mutate(g, "/leaves/0/address/arr", 256),
            "schema",
            "config_schema",
            "/leaves/0/address/arr",
            "",
        ),
        (
            "client-id-invalid",
            mutate(g, "/connection/client_id", "plc linha"),
            "schema",
            "config_schema",
            "/connection/client_id",
            "",
        ),
        (
            "topic-duplicate",
            dup_topic,
            "names",
            "config_destination_conflict",
            "/leaves/1/destination",
            "topic_duplicate",
        ),
        (
            "topic-wildcard",
            mutate(g, "/leaves/0/destination", "openplc/plc-linha1/+/x"),
            "names",
            "config_destination_invalid",
            "/leaves/0/destination",
            "topic_invalid_char",
        ),
        (
            "topic-dollar",
            mutate(g, "/leaves/0/destination", "$SYS/x"),
            "names",
            "config_destination_invalid",
            "/leaves/0/destination",
            "topic_reserved_prefix",
        ),
        (
            "topic-empty-segment",
            mutate(g, "/leaves/0/destination", "openplc//x"),
            "names",
            "config_destination_invalid",
            "/leaves/0/destination",
            "topic_empty_segment",
        ),
        (
            "topic-too-long",
            mutate(g, "/leaves/0/destination", "t/" + "a" * 511),
            "names",
            "config_destination_invalid",
            "/leaves/0/destination",
            "topic_too_long",
        ),
        (
            "metric-case-collision",
            mutate(s, "/leaves/1/destination", "INST0.V_BOOL"),
            "names",
            "config_destination_conflict",
            "/leaves/1/destination",
            "metric_collision",
        ),
        (
            "metric-unicode-collision",
            mutate(s, "/leaves/1/destination", "Inst0.V_\uff22OOL"),
            "names",
            "config_destination_conflict",
            "/leaves/1/destination",
            "metric_collision",
        ),
        (
            "metric-reserved-bdseq",
            mutate(s, "/leaves/0/destination", "bdSeq"),
            "names",
            "config_destination_invalid",
            "/leaves/0/destination",
            "metric_reserved",
        ),
        (
            "metric-reserved-node-control",
            mutate(s, "/leaves/0/destination", "Node Control/Rebirth"),
            "names",
            "config_destination_invalid",
            "/leaves/0/destination",
            "metric_reserved",
        ),
        (
            "metric-control-char",
            mutate(s, "/leaves/0/destination", "a\u0007b"),
            "names",
            "config_destination_invalid",
            "/leaves/0/destination",
            "metric_invalid_char",
        ),
        (
            "group-id-slash",
            mutate(s, "/sparkplug/group_id", "Plant/1"),
            "names",
            "config_sparkplug_id_invalid",
            "/sparkplug/group_id",
            "node_id_invalid_char",
        ),
        (
            "edge-node-outer-space",
            mutate(s, "/sparkplug/edge_node_id", "plc "),
            "names",
            "config_sparkplug_id_invalid",
            "/sparkplug/edge_node_id",
            "node_id_outer_whitespace",
        ),
        (
            "program-mismatch",
            mutate(g, "/program/md5", OTHER_MD5),
            "semantic",
            "config_program_mismatch",
            "/program/md5",
            "",
        ),
        (
            "address-not-in-program",
            mutate(g, "/leaves/0/address/elem", 4000),
            "semantic",
            "config_address_invalid",
            "/leaves/0/address",
            "",
        ),
        (
            "debug-path-mismatch",
            mutate(g, "/leaves/0/source/debug_path", "INST0.MOTOR1.RUNNING"),
            "semantic",
            "config_address_invalid",
            "/leaves/0/source/debug_path",
            "",
        ),
        (
            "type-differs-from-program",
            mutate(mutate(g, "/leaves/0/iec_type", "DINT"), "/leaves/0/size", 4),
            "semantic",
            "config_address_invalid",
            "/leaves/0/iec_type",
            "",
        ),
        ("duplicate-address", mutate(g, "/leaves/1/address", g["leaves"][0]["address"]), "semantic", "config_leaf_duplicate", "/leaves/1/address", ""),  # type: ignore[index]
    ]


def write(path: Path, doc: JsonValue) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main(out: Path) -> None:
    (out / "debug-map.json").parent.mkdir(parents=True, exist_ok=True)
    (out / "debug-map.json").write_text(
        json.dumps(debug_map(), separators=(",", ":")) + "\n", encoding="utf-8"
    )
    v = valid()
    for key, doc in v.items():
        write(out / "valid" / f"{key}.json", doc)
    cases: list[JsonValue] = []
    for key, doc, layer, code, pointer, detail in invalid(v):
        write(out / "invalid" / f"{key}.json", doc)
        case: Obj = {
            "file": f"invalid/{key}.json",
            "layer": layer,
            "code": code,
            "pointer": pointer,
        }
        if detail:
            case["detail"] = detail
        cases.append(case)
    dup = out / "invalid" / "duplicate-key.json"
    text = json.dumps(v["generic-on-change-anonymous-plaintext"], ensure_ascii=True, indent=2)
    dup.write_text(
        text.replace('"enabled": true,', '"enabled": true,\n  "enabled": true,', 1) + "\n",
        encoding="utf-8",
    )
    cases.insert(
        0,
        {
            "file": "invalid/duplicate-key.json",
            "layer": "parse",
            "code": "config_duplicate_key",
            "pointer": "/enabled",
        },
    )
    write(
        out / "cases.json",
        {
            "contract": "openplc-mqtt-runtime-config-cases",
            "version": 1,
            "context": {"debugMap": "debug-map.json", "programMd5": MD5},
            "valid": [f"valid/{k}.json" for k in v],
            "invalid": cases,
            "generated": [
                {
                    "id": "leaves-1000",
                    "base": "valid/generic-on-change-anonymous-plaintext.json",
                    "op": "big-leaves",
                    "count": 1000,
                    "expect": "valid",
                },
                {
                    "id": "leaves-1001",
                    "base": "valid/generic-on-change-anonymous-plaintext.json",
                    "op": "big-leaves",
                    "count": 1001,
                    "expect": {
                        "layer": "schema",
                        "code": "config_too_many_leaves",
                        "pointer": "/leaves",
                    },
                },
                {
                    "id": "birth-over-limit",
                    "base": "valid/sparkplug-client-certificate-all-types.json",
                    "op": "big-leaves-long-metric-names",
                    "count": 1000,
                    "expect": {
                        "layer": "semantic",
                        "code": "config_payload_too_large",
                        "pointer": "/leaves",
                    },
                },
                {
                    "id": "file-over-limit",
                    "op": "pad-to-bytes",
                    "bytes": 4194305,
                    "expect": {"layer": "parse", "code": "config_too_large", "pointer": ""},
                },
                {
                    "id": "nesting-too-deep",
                    "op": "nest-arrays",
                    "depth": 64,
                    "expect": {"layer": "parse", "code": "config_too_deep", "pointer": ""},
                },
            ],
            "ops": {
                "big-leaves": "leaves := for i in 0..count-1: tokens [instance Inst0, variable Big, index [i]], debug_path INST0.BIG[i], address {arr:1, elem:i}, DINT size 4, destination 'openplc/plc-linha1/Inst0/Big/' + i",
                "big-leaves-long-metric-names": "as big-leaves, destination = zero-padded 4-digit i followed by 'n' repeated to exactly 512 UTF-8 bytes",
                "pad-to-bytes": "valid generic fixture with leaves[0].source.debug_path unchanged and a JSON whitespace tail so the file is exactly 'bytes' long",
                "nest-arrays": "'[' * depth + ']' * depth",
            },
        },
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
