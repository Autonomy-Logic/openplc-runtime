# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Typed decoder for ``conf/mqtt.json`` (openplc-mqtt-runtime-config, format 1).

Independent of the producers: the runtime is the final authority. Validation
runs in layers (parse, schema, names, semantic) and stops at the first error,
so nothing is ever activated from a partially valid document. Standard library
only, because the webserver and the plugin venv both import it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, NoReturn, TypeAlias

from .constants import (
    DEBUG_SIZE,
    MAX_CA_PEM_CHARS,
    MAX_CONFIG_BYTES,
    MAX_CONFIG_DEPTH,
    MAX_INTERVAL_MS,
    MAX_LEAVES,
    MAX_PAYLOAD_BYTES,
    MIN_INTERVAL_MS,
    RUNTIME_CONFIG_CONTRACT,
    RUNTIME_CONFIG_FORMAT_VERSION,
)
from .names import (
    IndexToken,
    NameToken,
    SourceToken,
    find_destination_conflicts,
    utf8_length,
    validate_client_id,
    validate_metric_name,
    validate_node_id,
    validate_topic,
)

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
Layer = Literal["parse", "schema", "names", "semantic", "credential"]
Mode = Literal["generic", "sparkplug_b"]
AuthMethod = Literal["anonymous", "username_password", "client_certificate"]
PublishMode = Literal["on_change", "periodic"]

ERROR_CATEGORY: Final = "configuration"
# Rejections of an otherwise valid configuration by the credential vault or TLS material.
NON_CONFIG_ERROR_CATEGORY: Final[dict[str, str]] = {
    "credential_missing": "credential",
    "credential_binding_mismatch": "credential",
    "staging_consumed": "credential",
    "staging_expired": "credential",
    "staging_owner_mismatch": "credential",
    "staging_program_mismatch": "credential",
    "client_key_mismatch": "certificate",
    "client_certificate_expired": "certificate",
    "internal_error": "internal",
}
CONFIG_ERROR_CODES: Final = frozenset(
    {
        "config_too_large",
        "config_too_deep",
        "config_duplicate_key",
        "config_unknown_contract",
        "config_unknown_version",
        "config_schema",
        "config_no_leaves",
        "config_too_many_leaves",
        "config_type_unsupported",
        "config_size_mismatch",
        "config_tls_required",
        "config_destination_invalid",
        "config_destination_conflict",
        "config_sparkplug_id_invalid",
        "config_program_mismatch",
        "config_address_invalid",
        "config_leaf_duplicate",
        "config_payload_too_large",
        "config_credential_missing",
        "config_credential_binding_mismatch",
    }
)

MAX_DEBUG_MAP_BYTES: Final = 10 * 1024 * 1024
MAX_HOST_CHARS: Final = 253
MAX_TOKEN_NAME_CHARS: Final = 256
MAX_DEBUG_PATH_CHARS: Final = 1024
MIN_TOKENS: Final = 2
MAX_TOKENS: Final = 64
MAX_INDICES: Final = 8
INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1
MAX_ARR: Final = 255
MAX_ELEM: Final = 65_535
MAX_PORT: Final = 65_535

_ROOT_REQUIRED: Final = (
    "contract",
    "format_version",
    "enabled",
    "mode",
    "program",
    "connection",
    "auth",
    "leaves",
)
_SECTION_FOR_MODE: Final[dict[str, str]] = {"generic": "generic", "sparkplug_b": "sparkplug"}
_NAME_ROLES: Final = ("instance", "program", "variable", "field")
_MD5: Final = re.compile(r"^[0-9a-f]{32}$")
_UUID_V4: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_HOST: Final = re.compile(
    r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"|[0-9]{1,3}(\.[0-9]{1,3}){3}"
    r"|\[[0-9A-Fa-f:.]+\])$"
)
_PEM_HEADER: Final = "-----BEGIN CERTIFICATE-----"
_JSON_STRING: Final = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_JSON_BRACKET: Final = re.compile(r"[\[\]{}]")

# Sparkplug B datatype per IEC type (Tahu sparkplug_b.proto DataType enum).
SPARKPLUG_DATATYPE: Final[dict[str, int]] = {
    "BOOL": 11,
    "SINT": 1,
    "INT": 2,
    "DINT": 3,
    "LINT": 4,
    "USINT": 5,
    "UINT": 6,
    "UDINT": 7,
    "ULINT": 8,
    "BYTE": 5,
    "WORD": 6,
    "DWORD": 7,
    "LWORD": 8,
    "REAL": 9,
    "LREAL": 10,
    "STRING": 12,
    "WSTRING": 17,
    "TIME": 4,
    "TOD": 4,
    "DATE": 4,
    "DT": 4,
}
_ENCODING: Final[dict[str, str]] = {
    "WSTRING": "utf16le",
    "TIME": "int64-ns-duration",
    "TOD": "int64-ns-since-midnight",
    "DATE": "int64-ns-since-unix-epoch-utc-midnight",
    "DT": "int64-ns-since-unix-epoch",
}
_TEMPORAL: Final = frozenset({"TIME", "TOD", "DATE", "DT"})
_EPOCH_BASED: Final = frozenset({"DATE", "DT"})
BIRTH_CONTROL_METRICS: Final = (("bdSeq", "long"), ("Node Control/Rebirth", "boolean"))


@dataclass(frozen=True)
class ConfigError:
    """Sanitized rejection: never carries configuration values or secrets."""

    layer: Layer
    code: str
    pointer: str
    message: str
    detail: str | None = None

    @property
    def category(self) -> str:
        return NON_CONFIG_ERROR_CATEGORY.get(self.code, ERROR_CATEGORY)

    def to_api(self) -> dict[str, str]:
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "pointer": self.pointer,
        }


class ConfigRejected(Exception):
    def __init__(self, error: ConfigError) -> None:
        super().__init__(f"{error.code} at '{error.pointer}': {error.message}")
        self.error = error


class DebugMapError(ValueError):
    """The program's debug-map.json is missing, oversized or malformed."""


@dataclass(frozen=True)
class TlsTrust:
    source: Literal["system", "pem"]
    ca_pem: str | None


@dataclass(frozen=True)
class TlsSettings:
    enabled: bool
    trust: TlsTrust | None


@dataclass(frozen=True)
class Connection:
    host: str
    port: int
    client_id: str
    tls: TlsSettings


@dataclass(frozen=True)
class CredentialRef:
    id: str
    revision: int
    staging_id: str | None


@dataclass(frozen=True)
class Auth:
    method: AuthMethod
    credential_ref: CredentialRef | None


@dataclass(frozen=True)
class GenericSettings:
    publish_mode: PublishMode
    interval_ms: int
    qos: Literal[0, 1]
    retain: bool


@dataclass(frozen=True)
class SparkplugSettings:
    group_id: str
    edge_node_id: str
    sample_interval_ms: int


@dataclass(frozen=True)
class Leaf:
    tokens: tuple[SourceToken, ...]
    debug_path: str
    arr: int
    elem: int
    iec_type: str
    size: int
    destination: str


@dataclass(frozen=True)
class MqttRuntimeConfig:
    mode: Mode
    program_md5: str
    connection: Connection
    auth: Auth
    generic: GenericSettings | None
    sparkplug: SparkplugSettings | None
    leaves: tuple[Leaf, ...]


@dataclass(frozen=True)
class ProgramLeaf:
    path: str
    iec_type: str
    size: int


@dataclass(frozen=True)
class ProgramMap:
    """Addresses of the compiled program, from the debug-map.json of the same build."""

    md5: str
    leaves: Mapping[tuple[int, int], ProgramLeaf]


class _Pairs(list[tuple[str, JsonValue]]):
    """Object as parsed, before duplicate keys are checked."""


def _pointer(base: str, key: str | int) -> str:
    token = str(key).replace("~", "~0").replace("/", "~1")
    return f"{base}/{token}"


def _reject(
    layer: Layer, code: str, pointer: str, message: str, detail: str | None = None
) -> NoReturn:
    raise ConfigRejected(ConfigError(layer, code, pointer, message, detail))


def _schema_error(pointer: str, message: str) -> NoReturn:
    _reject("schema", "config_schema", pointer, message)


def _max_depth(text: str) -> int:
    depth = 0
    deepest = 0
    for match in _JSON_BRACKET.finditer(_JSON_STRING.sub("", text)):
        if match.group() in "[{":
            depth += 1
            deepest = max(deepest, depth)
        else:
            depth -= 1
    return deepest


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant {name}")


def _plain(value: JsonValue | _Pairs, pointer: str) -> JsonValue:
    if isinstance(value, _Pairs):
        out: dict[str, JsonValue] = {}
        for key, item in value:
            if key in out:
                _reject("parse", "config_duplicate_key", _pointer(pointer, key), "duplicate key")
            out[key] = _plain(item, _pointer(pointer, key))
        return out
    if isinstance(value, list):
        return [_plain(item, _pointer(pointer, i)) for i, item in enumerate(value)]
    return value


def parse_json_document(raw: bytes, max_bytes: int, max_depth: int) -> JsonValue:
    """Bounded parse: size and depth are checked before any decoding."""
    if len(raw) > max_bytes:
        _reject("parse", "config_too_large", "", f"document exceeds {max_bytes} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _reject("parse", "config_schema", "", "document is not valid UTF-8")
    if _max_depth(text) > max_depth:
        _reject("parse", "config_too_deep", "", f"nesting exceeds {max_depth} levels")
    try:
        tree = json.loads(text, object_pairs_hook=_Pairs, parse_constant=_reject_constant)
    except ValueError:
        _reject("parse", "config_schema", "", "document is not valid JSON")
    return _plain(tree, "")


def _object(value: JsonValue, pointer: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        _schema_error(pointer, "expected an object")
    return value


def _members(
    obj: dict[str, JsonValue], pointer: str, required: Sequence[str], optional: Sequence[str] = ()
) -> None:
    for key in obj:
        if key not in required and key not in optional:
            _schema_error(_pointer(pointer, key), "unknown member")
    for key in required:
        if key not in obj:
            _schema_error(pointer, f"missing member '{key}'")


def _string(
    value: JsonValue, pointer: str, min_chars: int = 1, max_chars: int | None = None
) -> str:
    if not isinstance(value, str):
        _schema_error(pointer, "expected a string")
    if len(value) < min_chars or (max_chars is not None and len(value) > max_chars):
        _schema_error(pointer, "string length out of range")
    return value


def _integer(value: JsonValue, pointer: str, lowest: int, highest: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _schema_error(pointer, "expected an integer")
    if not lowest <= value <= highest:
        _schema_error(pointer, f"integer out of range {lowest}..{highest}")
    return value


def _boolean(value: JsonValue, pointer: str) -> bool:
    if not isinstance(value, bool):
        _schema_error(pointer, "expected a boolean")
    return value


def _array(value: JsonValue, pointer: str) -> list[JsonValue]:
    if not isinstance(value, list):
        _schema_error(pointer, "expected an array")
    return value


def is_valid_host(host: str) -> bool:
    return 1 <= len(host) <= MAX_HOST_CHARS and _HOST.fullmatch(host) is not None


def is_uuid_v4(value: str) -> bool:
    return _UUID_V4.fullmatch(value) is not None


def _uuid(value: JsonValue, pointer: str) -> str:
    text = _string(value, pointer)
    if not _UUID_V4.fullmatch(text):
        _schema_error(pointer, "expected a lowercase UUID v4")
    return text


def _decode_tls(value: JsonValue, pointer: str) -> TlsSettings:
    obj = _object(value, pointer)
    _members(obj, pointer, ("enabled",), ("trust",))
    enabled = _boolean(obj["enabled"], _pointer(pointer, "enabled"))
    if not enabled:
        if "trust" in obj:
            _schema_error(_pointer(pointer, "trust"), "trust is only valid with TLS enabled")
        return TlsSettings(False, None)
    if "trust" not in obj:
        _schema_error(pointer, "missing member 'trust'")
    trust_pointer = _pointer(pointer, "trust")
    trust = _object(obj["trust"], trust_pointer)
    source = trust.get("source")
    if source == "system":
        _members(trust, trust_pointer, ("source",))
        return TlsSettings(True, TlsTrust("system", None))
    if source == "pem":
        _members(trust, trust_pointer, ("source", "ca_pem"))
        ca_pointer = _pointer(trust_pointer, "ca_pem")
        ca_pem = _string(trust["ca_pem"], ca_pointer, 1, MAX_CA_PEM_CHARS)
        if not ca_pem.startswith(_PEM_HEADER):
            _schema_error(ca_pointer, "expected a PEM certificate")
        return TlsSettings(True, TlsTrust("pem", ca_pem))
    _schema_error(_pointer(trust_pointer, "source"), "expected 'system' or 'pem'")


def _decode_connection(value: JsonValue) -> Connection:
    pointer = "/connection"
    obj = _object(value, pointer)
    _members(obj, pointer, ("host", "port", "client_id", "tls"))
    host_pointer = _pointer(pointer, "host")
    host = _string(obj["host"], host_pointer, 1, MAX_HOST_CHARS)
    if not _HOST.fullmatch(host):
        _schema_error(host_pointer, "expected a hostname or IP address")
    port = _integer(obj["port"], _pointer(pointer, "port"), 1, MAX_PORT)
    client_pointer = _pointer(pointer, "client_id")
    client_id = _string(obj["client_id"], client_pointer)
    if validate_client_id(client_id) is not None:
        _schema_error(client_pointer, "client_id_invalid")
    tls = _decode_tls(obj["tls"], _pointer(pointer, "tls"))
    return Connection(host, port, client_id, tls)


def _decode_auth(value: JsonValue) -> Auth:
    pointer = "/auth"
    obj = _object(value, pointer)
    method = obj.get("method")
    if method == "anonymous":
        _members(obj, pointer, ("method",))
        return Auth("anonymous", None)
    if method not in ("username_password", "client_certificate"):
        _schema_error(_pointer(pointer, "method"), "unknown authentication method")
    _members(obj, pointer, ("method", "credential_ref"))
    ref_pointer = _pointer(pointer, "credential_ref")
    ref = _object(obj["credential_ref"], ref_pointer)
    _members(ref, ref_pointer, ("id", "revision", "staging_id"))
    ref_id = _uuid(ref["id"], _pointer(ref_pointer, "id"))
    revision = _integer(ref["revision"], _pointer(ref_pointer, "revision"), 1, INT32_MAX)
    staging = ref["staging_id"]
    staging_id = None if staging is None else _uuid(staging, _pointer(ref_pointer, "staging_id"))
    secret_method: AuthMethod = (
        "username_password" if method == "username_password" else "client_certificate"
    )
    return Auth(secret_method, CredentialRef(ref_id, revision, staging_id))


def _interval(value: JsonValue, pointer: str) -> int:
    return _integer(value, pointer, MIN_INTERVAL_MS, MAX_INTERVAL_MS)


def _decode_generic(value: JsonValue) -> GenericSettings:
    pointer = "/generic"
    obj = _object(value, pointer)
    publish_mode = obj.get("publish_mode")
    if publish_mode == "on_change":
        interval_key = "sample_interval_ms"
    elif publish_mode == "periodic":
        interval_key = "publish_interval_ms"
    else:
        _schema_error(_pointer(pointer, "publish_mode"), "expected 'on_change' or 'periodic'")
    _members(obj, pointer, ("publish_mode", interval_key, "qos", "retain"))
    interval = _interval(obj[interval_key], _pointer(pointer, interval_key))
    qos_pointer = _pointer(pointer, "qos")
    qos = _integer(obj["qos"], qos_pointer, 0, 1)
    retain = _boolean(obj["retain"], _pointer(pointer, "retain"))
    mode: PublishMode = "on_change" if publish_mode == "on_change" else "periodic"
    return GenericSettings(mode, interval, 0 if qos == 0 else 1, retain)


def _decode_sparkplug(value: JsonValue) -> SparkplugSettings:
    pointer = "/sparkplug"
    obj = _object(value, pointer)
    _members(obj, pointer, ("group_id", "edge_node_id", "sample_interval_ms"))
    group_id = _string(obj["group_id"], _pointer(pointer, "group_id"))
    edge_node_id = _string(obj["edge_node_id"], _pointer(pointer, "edge_node_id"))
    interval = _interval(obj["sample_interval_ms"], _pointer(pointer, "sample_interval_ms"))
    return SparkplugSettings(group_id, edge_node_id, interval)


def _decode_token(value: JsonValue, pointer: str) -> SourceToken:
    obj = _object(value, pointer)
    role = obj.get("role")
    if role == "index":
        _members(obj, pointer, ("role", "indices"))
        indices_pointer = _pointer(pointer, "indices")
        raw = _array(obj["indices"], indices_pointer)
        if not 1 <= len(raw) <= MAX_INDICES:
            _schema_error(indices_pointer, f"expected 1..{MAX_INDICES} indices")
        indices = tuple(
            _integer(item, _pointer(indices_pointer, i), INT32_MIN, INT32_MAX)
            for i, item in enumerate(raw)
        )
        return IndexToken(indices)
    if role not in _NAME_ROLES:
        _schema_error(_pointer(pointer, "role"), "unknown token role")
    _members(obj, pointer, ("role", "name"))
    name = _string(obj["name"], _pointer(pointer, "name"), 1, MAX_TOKEN_NAME_CHARS)
    if role == "instance":
        return NameToken("instance", name)
    if role == "program":
        return NameToken("program", name)
    if role == "variable":
        return NameToken("variable", name)
    return NameToken("field", name)


def _decode_source(value: JsonValue, pointer: str) -> tuple[tuple[SourceToken, ...], str]:
    source = _object(value, pointer)
    _members(source, pointer, ("tokens", "debug_path"))
    tokens_pointer = _pointer(pointer, "tokens")
    raw_tokens = _array(source["tokens"], tokens_pointer)
    if not MIN_TOKENS <= len(raw_tokens) <= MAX_TOKENS:
        _schema_error(tokens_pointer, f"expected {MIN_TOKENS}..{MAX_TOKENS} tokens")
    tokens = tuple(
        _decode_token(item, _pointer(tokens_pointer, i)) for i, item in enumerate(raw_tokens)
    )
    debug_path = _string(
        source["debug_path"], _pointer(pointer, "debug_path"), 1, MAX_DEBUG_PATH_CHARS
    )
    return tokens, debug_path


def _decode_address(value: JsonValue, pointer: str) -> tuple[int, int]:
    address = _object(value, pointer)
    _members(address, pointer, ("arr", "elem"))
    arr = _integer(address["arr"], _pointer(pointer, "arr"), 0, MAX_ARR)
    elem = _integer(address["elem"], _pointer(pointer, "elem"), 0, MAX_ELEM)
    return arr, elem


def _decode_leaf(value: JsonValue, pointer: str) -> Leaf:
    obj = _object(value, pointer)
    _members(obj, pointer, ("source", "address", "iec_type", "size", "destination"))
    tokens, debug_path = _decode_source(obj["source"], _pointer(pointer, "source"))
    arr, elem = _decode_address(obj["address"], _pointer(pointer, "address"))
    type_pointer = _pointer(pointer, "iec_type")
    iec_type = _string(obj["iec_type"], type_pointer)
    if iec_type not in DEBUG_SIZE:
        _reject("schema", "config_type_unsupported", type_pointer, "unsupported IEC type")
    size_pointer = _pointer(pointer, "size")
    size = _integer(obj["size"], size_pointer, 1, max(DEBUG_SIZE.values()))
    if size != DEBUG_SIZE[iec_type]:
        _reject("schema", "config_size_mismatch", size_pointer, "size does not match the type")
    destination = _string(obj["destination"], _pointer(pointer, "destination"))
    return Leaf(tokens, debug_path, arr, elem, iec_type, size, destination)


def _decode_leaves(value: JsonValue) -> tuple[Leaf, ...]:
    pointer = "/leaves"
    items = _array(value, pointer)
    if not items:
        _reject("schema", "config_no_leaves", pointer, "at least one leaf is required")
    if len(items) > MAX_LEAVES:
        _reject("schema", "config_too_many_leaves", pointer, f"more than {MAX_LEAVES} leaves")
    return tuple(_decode_leaf(item, _pointer(pointer, i)) for i, item in enumerate(items))


def _decode_schema(tree: JsonValue) -> MqttRuntimeConfig:
    root = _object(tree, "")
    if root.get("contract") != RUNTIME_CONFIG_CONTRACT:
        _reject("schema", "config_unknown_contract", "/contract", "unknown contract")
    version = root.get("format_version")
    if not _is_int(version) or version != RUNTIME_CONFIG_FORMAT_VERSION:
        _reject("schema", "config_unknown_version", "/format_version", "unsupported format_version")
    _members(root, "", _ROOT_REQUIRED, tuple(_SECTION_FOR_MODE.values()))
    if root["enabled"] is not True:
        _schema_error("/enabled", "an enabled integration is required; disabled means no file")
    mode_value = root["mode"]
    if not isinstance(mode_value, str) or mode_value not in _SECTION_FOR_MODE:
        _schema_error("/mode", "expected 'generic' or 'sparkplug_b'")
    mode: Mode = "generic" if mode_value == "generic" else "sparkplug_b"
    own_section = _SECTION_FOR_MODE[mode]
    other_section = "sparkplug" if own_section == "generic" else "generic"
    if own_section not in root:
        _schema_error("/mode", f"mode requires the '{own_section}' section")
    if other_section in root:
        _schema_error(_pointer("", other_section), "section does not belong to the mode")
    program = _object(root["program"], "/program")
    _members(program, "/program", ("md5",))
    md5 = _string(program["md5"], "/program/md5")
    if not _MD5.fullmatch(md5):
        _schema_error("/program/md5", "expected a lowercase MD5 digest")
    connection = _decode_connection(root["connection"])
    auth = _decode_auth(root["auth"])
    if auth.method == "client_certificate" and not connection.tls.enabled:
        _reject(
            "schema",
            "config_tls_required",
            "/connection/tls/enabled",
            "client certificate authentication requires TLS",
        )
    generic = _decode_generic(root["generic"]) if mode == "generic" else None
    sparkplug = _decode_sparkplug(root["sparkplug"]) if mode == "sparkplug_b" else None
    leaves = _decode_leaves(root["leaves"])
    return MqttRuntimeConfig(mode, md5, connection, auth, generic, sparkplug, leaves)


def _check_names(config: MqttRuntimeConfig) -> None:
    if config.sparkplug is not None:
        for key, value in (
            ("group_id", config.sparkplug.group_id),
            ("edge_node_id", config.sparkplug.edge_node_id),
        ):
            rule = validate_node_id(value)
            if rule is not None:
                _reject(
                    "names",
                    "config_sparkplug_id_invalid",
                    _pointer("/sparkplug", key),
                    f"invalid Sparkplug identifier ({rule})",
                    rule,
                )
    validate = validate_topic if config.mode == "generic" else validate_metric_name
    destinations = [leaf.destination for leaf in config.leaves]
    for i, destination in enumerate(destinations):
        rule = validate(destination)
        if rule is not None:
            _reject(
                "names",
                "config_destination_invalid",
                f"/leaves/{i}/destination",
                f"invalid destination ({rule})",
                rule,
            )
    for conflict in find_destination_conflicts(config.mode, destinations):
        _reject(
            "names",
            "config_destination_conflict",
            f"/leaves/{conflict.second}/destination",
            f"destination collides with leaf {conflict.first} ({conflict.code})",
            conflict.code,
        )


def _varint_len(value: int) -> int:
    length = 1
    while value >= 0x80:
        value >>= 7
        length += 1
    return length


def _field_len(field_number: int, payload_len: int) -> int:
    return _varint_len(field_number << 3) + _varint_len(payload_len) + payload_len


def birth_properties(iec_type: str) -> tuple[tuple[str, str], ...]:
    """NBIRTH metric properties (all String) for a leaf of this IEC type."""
    props = [("iecType", iec_type), ("encoding", _ENCODING.get(iec_type, "native"))]
    if iec_type in _TEMPORAL:
        props.append(("unit", "ns"))
    if iec_type in _EPOCH_BASED:
        props.append(("epoch", "unix"))
    return tuple(props)


_UINT64_MAX_LEN: Final = 10
_UINT32_MAX_LEN: Final = 5


def _value_field_upper_bound(iec_type: str, size: int) -> int:
    datatype = SPARKPLUG_DATATYPE[iec_type]
    if datatype == 11:
        return _varint_len(14 << 3) + 1
    if datatype in (4, 8):
        return _varint_len(11 << 3) + _UINT64_MAX_LEN
    if datatype == 9:
        return _varint_len(12 << 3) + 4
    if datatype == 10:
        return _varint_len(13 << 3) + 8
    if datatype == 12:
        return _field_len(15, size - 1)
    if datatype == 17:
        return _field_len(16, size - 1)
    return _varint_len(10 << 3) + _UINT32_MAX_LEN


def _metric_upper_bound(name: str, iec_type: str | None, size: int, control: str = "") -> int:
    body = _field_len(1, utf8_length(name))
    body += _varint_len(3 << 3) + _UINT64_MAX_LEN
    body += _varint_len(4 << 3) + 1
    if iec_type is None:
        if control == "long":
            body += _varint_len(11 << 3) + _UINT64_MAX_LEN
        else:
            body += _varint_len(14 << 3) + 1
        return _field_len(2, body)
    props = 0
    for key, text in birth_properties(iec_type):
        props += _field_len(1, utf8_length(key))
        property_value = (_varint_len(1 << 3) + 1) + _field_len(8, utf8_length(text))
        props += _field_len(2, property_value)
    body += _field_len(9, props)
    is_null = _varint_len(7 << 3) + 1
    body += max(is_null, _value_field_upper_bound(iec_type, size))
    return _field_len(2, body)


def sparkplug_birth_upper_bound(leaves: Sequence[Leaf]) -> int:
    """Largest NBIRTH this configuration can produce, in protobuf bytes.

    Every uint64 varint (timestamps, seq, long values) counts 10 bytes, every
    uint32 int_value 5 bytes; STRING/WSTRING values count ``size - 1`` bytes,
    the debug window the runtime reads. Producers must compute at least this.
    """
    total = (_varint_len(1 << 3) + _UINT64_MAX_LEN) + (_varint_len(3 << 3) + _UINT64_MAX_LEN)
    for name, control in BIRTH_CONTROL_METRICS:
        total += _metric_upper_bound(name, None, 0, control)
    for leaf in leaves:
        total += _metric_upper_bound(leaf.destination, leaf.iec_type, leaf.size)
    return total


def _check_semantics(config: MqttRuntimeConfig, program: ProgramMap | None) -> None:
    if program is not None and config.program_md5 != program.md5:
        _reject(
            "semantic",
            "config_program_mismatch",
            "/program/md5",
            "configuration was generated for a different program",
        )
    seen: set[tuple[int, int]] = set()
    for i, leaf in enumerate(config.leaves):
        base = f"/leaves/{i}"
        key = (leaf.arr, leaf.elem)
        if key in seen:
            _reject(
                "semantic",
                "config_leaf_duplicate",
                f"{base}/address",
                "address is selected more than once",
            )
        seen.add(key)
        if program is not None:
            entry = program.leaves.get(key)
            if entry is None:
                _reject(
                    "semantic",
                    "config_address_invalid",
                    f"{base}/address",
                    "address does not exist in the program",
                )
            if entry.path != leaf.debug_path:
                _reject(
                    "semantic",
                    "config_address_invalid",
                    f"{base}/source/debug_path",
                    "debug_path differs from the program at this address",
                )
            if entry.iec_type != leaf.iec_type:
                _reject(
                    "semantic",
                    "config_address_invalid",
                    f"{base}/iec_type",
                    "type differs from the program at this address",
                )
            if entry.size != leaf.size:
                _reject(
                    "semantic",
                    "config_address_invalid",
                    f"{base}/size",
                    "size differs from the program at this address",
                )
    if config.mode == "sparkplug_b":
        bound = sparkplug_birth_upper_bound(config.leaves)
        if bound > MAX_PAYLOAD_BYTES:
            _reject(
                "semantic",
                "config_payload_too_large",
                "/leaves",
                f"NBIRTH may reach {bound} bytes, limit is {MAX_PAYLOAD_BYTES}",
            )


def decode_runtime_config(raw: bytes, program: ProgramMap | None) -> MqttRuntimeConfig:
    """Decode and validate every layer; raise ``ConfigRejected`` on the first error.

    ``program`` is the compiled program the configuration must match. Without
    it (plugin start) program-bound checks are skipped; the webserver always
    passes it at upload and activation.
    """
    tree = parse_json_document(raw, MAX_CONFIG_BYTES, MAX_CONFIG_DEPTH)
    config = _decode_schema(tree)
    _check_names(config)
    _check_semantics(config, program)
    return config


def _is_int(value: JsonValue) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_debug_map(raw: bytes) -> ProgramMap:
    """Program addresses from a STruC++ debug-map.json (format version 2)."""
    try:
        tree = parse_json_document(raw, MAX_DEBUG_MAP_BYTES, MAX_CONFIG_DEPTH)
    except ConfigRejected as exc:
        raise DebugMapError(exc.error.message) from exc
    if not isinstance(tree, dict) or tree.get("version") != 2:
        raise DebugMapError("unsupported debug map version")
    md5 = tree.get("md5")
    entries = tree.get("leaves")
    if not isinstance(md5, str) or not _MD5.fullmatch(md5) or not isinstance(entries, list):
        raise DebugMapError("debug map is missing md5 or leaves")
    leaves: dict[tuple[int, int], ProgramLeaf] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise DebugMapError("debug map leaf is not an object")
        arr = entry.get("arrayIdx")
        elem = entry.get("elemIdx")
        path = entry.get("path")
        iec_type = entry.get("type")
        size = entry.get("size")
        if not (
            _is_int(arr)
            and _is_int(elem)
            and _is_int(size)
            and isinstance(arr, int)
            and isinstance(elem, int)
            and isinstance(size, int)
            and isinstance(path, str)
            and isinstance(iec_type, str)
        ):
            raise DebugMapError("debug map leaf has an invalid shape")
        if (arr, elem) in leaves:
            raise DebugMapError("debug map repeats an address")
        leaves[(arr, elem)] = ProgramLeaf(path, iec_type, size)
    return ProgramMap(md5, leaves)
