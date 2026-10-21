# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Reception and activation of ``conf/mqtt.json`` on program upload.

The upload is validated against the ``debug-map.json`` of the same archive
before anything on the device changes. The configuration only becomes active
after the build succeeds; until then, and whenever an upload carries no MQTT
configuration, the plugin is disabled and its installed copy removed.

A configuration that references a staged credential claims it for the build
of its own upload; the staged credential becomes the active one only when the
configuration is activated, and is discarded on any failure.

The integration status comes from the plugin's local status channel while the
plugin runs; otherwise it is derived from the installed configuration.
"""

from __future__ import annotations

import os
import sys
import threading
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Final, Protocol

from webserver.config import PERSISTENT_DATA_DIR
from webserver.logger import get_logger
from webserver.plugin_config_model import PluginConfig, PluginsConfiguration, PluginType

_MQTT_PLUGIN_DIR: Final = str(
    Path(__file__).resolve().parent.parent
    / "core"
    / "src"
    / "drivers"
    / "plugins"
    / "python"
    / "mqtt"
)
if _MQTT_PLUGIN_DIR not in sys.path:
    sys.path.append(_MQTT_PLUGIN_DIR)

# pylint: disable=wrong-import-position
from contract import constants
from contract.runtime_config import (
    MAX_DEBUG_MAP_BYTES,
    ConfigError,
    ConfigRejected,
    CredentialRef,
    DebugMapError,
    MqttRuntimeConfig,
    decode_runtime_config,
    parse_debug_map,
)
from mqtt_credentials import material
from mqtt_credentials.model import (
    SECRET_METHODS,
    Actor,
    Binding,
    StagedRecord,
    Tombstone,
    VaultCorrupt,
    VaultLocator,
)
from mqtt_credentials.session import LOCATOR_NAME
from mqtt_credentials.tls import CA_MESSAGE, CA_POINTER, check_ca_pem
from mqtt_credentials.vault import CredentialVault, owner_key, write_atomically
from mqtt_publisher.clock import SystemClock
from mqtt_publisher.status import (
    STATUS_CONTRACT,
    STATUS_VERSION,
    State,
    StatusDocument,
    StatusTracker,
)
from mqtt_publisher.status_channel import (
    REQUEST_DROP_CREDENTIAL,
    REQUEST_STATUS,
    STATUS_SOCKET_NAME,
    ChannelError,
)
from mqtt_publisher.status_channel import request as channel_request

# pylint: enable=wrong-import-position

PLUGIN_NAME: Final = "mqtt"
CONFIG_ENTRY: Final = "conf/mqtt.json"
DEBUG_MAP_ENTRY: Final = "debug-map.json"
ACTIVE_CONFIG_NAME: Final = "mqtt.json"
DEFAULT_PLUGINS_CONF: Final = "plugins_default.conf"
PLUGINS_CONF: Final = "plugins.conf"
STATUS_TIMEOUT_S: Final = 2.0
# Stopping a session takes up to the 5 s shutdown budget of the publisher.
DROP_TIMEOUT_S: Final = 7.0
_NOT_REPORTING: Final = "the MQTT plugin is not reporting its status"
_STATES: Final = frozenset(
    {
        "disabled",
        "plc_stopped",
        "connecting",
        "connected",
        "retry_wait",
        "config_error",
        "credential_error",
    }
)
_REF_POINTER: Final = "/auth/credential_ref"
_STAGING_POINTER: Final = "/auth/credential_ref/staging_id"

logger, _ = get_logger(__name__)


class ZipEntry(Protocol):  # pylint: disable=too-few-public-methods
    filename: str
    file_size: int


class MqttActivationError(RuntimeError):
    """The installed configuration could not be validated at activation."""


@dataclass(frozen=True)
class BuildClaim:
    """Staged credential accepted with an upload, reserved for its build."""

    staging_id: str
    actor: Actor


_vault: CredentialVault | None = None
_vault_lock = threading.Lock()
_claim: BuildClaim | None = None
_claim_lock = threading.Lock()


def vault() -> CredentialVault:
    """The device vault, outside core/generated, plugin directories and snapshots."""
    global _vault  # pylint: disable=global-statement
    with _vault_lock:
        if _vault is None:
            _vault = CredentialVault(PERSISTENT_DATA_DIR / "mqtt", PERSISTENT_DATA_DIR / "keys")
        return _vault


def audit(event: str, result: str, actor: Actor | None, **fields: object) -> None:
    """One sanitized line per sensitive operation: ids and outcomes, never secrets."""
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    who = "unauthenticated" if actor is None else f"{actor.label()!r} user_id={actor.user_id}"
    origin = "" if actor is None else actor.origin
    extra = "".join(f" {key}={value}" for key, value in fields.items())
    logger.info(
        "MQTT credential audit: event=%s result=%s actor=%s origin=%s at=%s%s",
        event,
        result,
        who,
        origin or "-",
        at,
        extra,
    )


def credential_methods() -> tuple[str, ...]:
    """Authentication methods with secrets this runtime can serve.

    Empty when the pinned ``cryptography`` is not available (for example on
    MSYS2/Cygwin): only anonymous configurations can then be activated.
    """
    return SECRET_METHODS if material.crypto_available() else ()


def capabilities() -> dict[str, object]:
    """Body of ``GET /api/mqtt/capabilities`` (``$defs/capabilities``)."""
    return {
        "contract": "openplc-mqtt-capabilities",
        "version": 1,
        "runtimeConfig": {
            "contract": constants.RUNTIME_CONFIG_CONTRACT,
            "formatVersions": [constants.RUNTIME_CONFIG_FORMAT_VERSION],
        },
        "modes": ["generic", "sparkplug_b"],
        "credentials": {
            "api": 1,
            "methods": list(credential_methods()),
            "stagingTtlSeconds": constants.STAGING_TTL_S,
            "maxStaged": constants.MAX_STAGED,
        },
        "limits": {
            "maxLeaves": constants.MAX_LEAVES,
            "maxNameBytes": constants.MAX_NAME_BYTES,
            "maxPayloadBytes": constants.MAX_PAYLOAD_BYTES,
            "maxConfigBytes": constants.MAX_CONFIG_BYTES,
        },
    }


def _missing(message: str) -> ConfigError:
    return ConfigError("credential", "config_credential_missing", _REF_POINTER, message)


def _binding_mismatch() -> ConfigError:
    return ConfigError(
        "credential",
        "config_credential_binding_mismatch",
        "/connection",
        "the credential is bound to another broker connection or method",
    )


def _active_error(ref: CredentialRef, binding: Binding) -> ConfigError | None:
    active = vault().snapshot().active
    if active is None or (active.credential_id, active.revision) != (ref.id, ref.revision):
        return _missing("the referenced credential is not active on this runtime")
    return None if active.binding == binding else _binding_mismatch()


def _staging_error(
    config: MqttRuntimeConfig,
    ref: CredentialRef,
    binding: Binding,
    actor: Actor | None,
    claim: BuildClaim | None,
) -> ConfigError | None:
    staging_id = ref.staging_id or ""
    protect = () if claim is None else (claim.staging_id,)
    found = vault().find(staging_id, protect)
    if isinstance(found, Tombstone):
        code = "staging_consumed" if found.reason == "consumed" else "staging_expired"
        message = (
            "the staged credential was already used; stage it again"
            if found.reason == "consumed"
            else "the staged credential expired; stage it again"
        )
        return ConfigError("credential", code, _STAGING_POINTER, message)
    if found is None:
        return _missing("the staged credential does not exist on this runtime")
    if claim is not None and claim.staging_id != staging_id:
        return _missing("the staged credential is not the one accepted with the upload")
    owner = actor if claim is None else claim.actor
    if owner is None or found.owner_id != owner_key(owner):
        return ConfigError(
            "credential",
            "staging_owner_mismatch",
            _STAGING_POINTER,
            "the staged credential belongs to another user",
        )
    if (found.record.credential_id, found.record.revision) != (ref.id, ref.revision):
        return _missing("the credential reference does not match the staged credential")
    if found.program_md5 != config.program_md5:
        return ConfigError(
            "credential",
            "staging_program_mismatch",
            _STAGING_POINTER,
            "the staged credential was prepared for another program",
        )
    return None if found.record.binding == binding else _binding_mismatch()


def _credential_error(
    config: MqttRuntimeConfig, actor: Actor | None, claim: BuildClaim | None
) -> ConfigError | None:
    trust = config.connection.tls.trust
    if trust is not None and trust.ca_pem is not None and not check_ca_pem(trust.ca_pem):
        return ConfigError("credential", "config_schema", CA_POINTER, CA_MESSAGE)
    binding = Binding.from_config(config)
    ref = config.auth.credential_ref
    if binding is None or ref is None:
        return None
    if binding.method not in credential_methods():
        return _missing("the credential for this configuration is not available on this runtime")
    try:
        if ref.staging_id is None:
            return _active_error(ref, binding)
        return _staging_error(config, ref, binding, actor, claim)
    except (VaultCorrupt, OSError):
        logger.error("MQTT: the credential vault cannot be read")
        return _missing("the credential vault cannot be read")


def _program_unverifiable(reason: str) -> ConfigError:
    return ConfigError(
        "semantic",
        "config_program_mismatch",
        "/program/md5",
        f"the program debug map is {reason}, so the configuration cannot be verified",
    )


def validate(
    config_raw: bytes,
    debug_map_raw: bytes | None,
    actor: Actor | None = None,
    claim: BuildClaim | None = None,
) -> MqttRuntimeConfig | ConfigError:
    """Decode against the program; return the config or the first ``ConfigError``.

    Configuration errors are reported before a missing or invalid debug map,
    which only blocks the program-bound checks. A staged credential must
    belong to ``actor`` (upload) or be the one ``claim`` reserved (activation).
    """
    program = None
    program_error = None
    if debug_map_raw is None:
        program_error = _program_unverifiable("missing")
    else:
        try:
            program = parse_debug_map(debug_map_raw)
        except DebugMapError:
            program_error = _program_unverifiable("invalid")
    try:
        config = decode_runtime_config(config_raw, program)
    except ConfigRejected as exc:
        return exc.error
    if program_error is not None:
        return program_error
    credential_error = _credential_error(config, actor, claim)
    return config if credential_error is None else credential_error


def describe(error: ConfigError) -> str:
    return f"{error.code} at '{error.pointer}': {error.message}"


def _read_entry(archive: zipfile.ZipFile, entry: ZipEntry, limit: int) -> bytes:
    with archive.open(entry.filename) as handle:
        return handle.read(limit + 1)


def _set_claim(claim: BuildClaim | None) -> None:
    global _claim  # pylint: disable=global-statement
    with _claim_lock:
        _claim = claim


def _discard_owned_reference(config_raw: bytes, actor: Actor | None) -> None:
    """A rejected upload discards the staged credential it referenced, if the caller's."""
    if actor is None:
        return
    try:
        ref = decode_runtime_config(config_raw, None).auth.credential_ref
    except ConfigRejected:
        return
    if ref is None or ref.staging_id is None:
        return
    try:
        found = vault().find(ref.staging_id)
        if isinstance(found, StagedRecord) and found.owner_id == owner_key(actor):
            vault().discard(ref.staging_id)
            audit("discard", "upload_rejected", actor, staging_id=ref.staging_id)
    except (VaultCorrupt, OSError):
        logger.error("MQTT: the credential vault cannot be read")


def validate_upload(
    zip_source: str | IO[bytes], entries: Mapping[str, ZipEntry], actor: Actor | None = None
) -> dict[str, str] | None:
    """Validate the archive's MQTT configuration before the device changes.

    ``entries`` maps logical paths (as extracted into ``core/generated``) to
    archive members. Returns the sanitized error, or None when the archive
    has no MQTT configuration or it is valid. A valid configuration that
    references a staged credential claims it for this upload's build.
    """
    _set_claim(None)
    config_entry = entries.get(CONFIG_ENTRY)
    if config_entry is None:
        return None
    debug_entry = entries.get(DEBUG_MAP_ENTRY)
    with zipfile.ZipFile(zip_source, "r") as archive:
        config_raw = _read_entry(archive, config_entry, constants.MAX_CONFIG_BYTES)
        debug_raw = (
            None if debug_entry is None else _read_entry(archive, debug_entry, MAX_DEBUG_MAP_BYTES)
        )
    result = validate(config_raw, debug_raw, actor)
    if isinstance(result, ConfigError):
        _discard_owned_reference(config_raw, actor)
        return result.to_api()
    ref = result.auth.credential_ref
    if ref is not None and ref.staging_id is not None and actor is not None:
        _set_claim(BuildClaim(ref.staging_id, actor))
    return None


def abandon_upload() -> None:
    """The upload failed after validation: discard the staged credential it claimed."""
    with _claim_lock:
        claim = _claim
    _set_claim(None)
    if claim is not None:
        _discard_claimed(claim, "upload_failed")


def begin_build() -> BuildClaim | None:
    with _claim_lock:
        return _claim


def finish_build(claim: BuildClaim | None, succeeded: bool) -> None:
    """End of a build: release its claim, discard an unused staged credential, sweep."""
    global _claim  # pylint: disable=global-statement
    with _claim_lock:
        if claim is not None and _claim is claim:
            _claim = None
    if claim is not None:
        _discard_claimed(claim, "build_succeeded" if succeeded else "build_failed")
    sweep_staging()


def sweep_staging() -> None:
    """Destroy expired staged credentials (webserver start and end of each build)."""
    try:
        for staging_id in vault().sweep():
            audit("expire", "discarded", None, staging_id=staging_id)
    except (VaultCorrupt, OSError):
        logger.error("MQTT: the credential vault cannot be read")


def _discard_claimed(claim: BuildClaim, reason: str) -> None:
    try:
        if vault().discard(claim.staging_id):
            audit("discard", reason, claim.actor, staging_id=claim.staging_id)
    except (VaultCorrupt, OSError):
        logger.error("MQTT: the credential vault cannot be read")


def _read_bounded(path: Path, limit: int) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit + 1)
    except FileNotFoundError:
        return None


def _default_entry() -> PluginConfig | None:
    if not os.path.exists(DEFAULT_PLUGINS_CONF):
        return None
    return PluginsConfiguration.from_file(DEFAULT_PLUGINS_CONF).get_plugin(PLUGIN_NAME)


def ensure_registered(plugins_config: PluginsConfiguration) -> PluginConfig | None:
    """Add the default entry to a plugins.conf created before the plugin existed.

    The entry goes before every other Python plugin, as in plugins_default.conf:
    the core puts each plugin venv in front of sys.path in load order, so the
    MQTT venv loaded first never shadows a package the existing plugins import.
    """
    plugin = plugins_config.get_plugin(PLUGIN_NAME)
    if plugin is not None:
        return plugin
    default = _default_entry()
    if default is None:
        return None
    entry = PluginConfig(
        default.name,
        default.path,
        False,
        default.plugin_type,
        default.config_path,
        default.venv_path,
    )
    python_positions = [
        i for i, p in enumerate(plugins_config.plugins) if p.plugin_type == PluginType.PYTHON
    ]
    plugins_config.plugins.insert(python_positions[0] if python_positions else 0, entry)
    return entry


def active_config_path(plugin: PluginConfig) -> Path:
    return Path(os.path.dirname(plugin.path) or ".") / ACTIVE_CONFIG_NAME


def locator_path(plugin: PluginConfig) -> Path:
    return active_config_path(plugin).with_name(LOCATOR_NAME)


def _deactivate(plugin: PluginConfig, reason: str) -> list[str]:
    plugin.enabled = False
    active = active_config_path(plugin)
    messages = [f"MQTT: integration inactive ({reason})"]
    try:
        active.unlink()
        messages.append("MQTT: removed the previously installed configuration")
    except FileNotFoundError:
        pass
    locator_path(plugin).unlink(missing_ok=True)
    return messages


def _plugin_python(plugin: PluginConfig) -> str:
    if not plugin.venv_path:
        return os.path.abspath(sys.executable)
    return os.path.join(os.path.abspath(plugin.venv_path), "bin", "python3")


def _locator(plugin: PluginConfig) -> bytes:
    store = vault()
    return VaultLocator(
        str(store.vault_path.resolve()),
        str(store.key_path.resolve()),
        _plugin_python(plugin),
    ).to_bytes()


def _install_atomically(target: Path, raw: bytes) -> None:
    write_atomically(target, raw)


def apply_activation(
    plugins_config: PluginsConfiguration, generated_dir: str, program_built: bool
) -> list[str]:
    """Enable the plugin with the uploaded configuration, or disable it.

    Before the build (``program_built`` False) the plugin is always disabled:
    the previous configuration belongs to the previous program. After a
    successful build the configuration is validated again against the built
    program and installed atomically; any failure raises and leaves it inactive.
    """
    source = Path(generated_dir) / CONFIG_ENTRY
    plugin = ensure_registered(plugins_config)
    if plugin is None:
        if source.exists():
            raise MqttActivationError("plugin 'mqtt' is not registered in plugins_default.conf")
        return []
    if not program_built:
        return _deactivate(plugin, "waiting for the build of the uploaded program")
    config_raw = _read_bounded(source, constants.MAX_CONFIG_BYTES)
    if config_raw is None:
        return _deactivate(plugin, "the program has no MQTT configuration")
    messages = _deactivate(plugin, "replacing the configuration")
    debug_raw = _read_bounded(Path(generated_dir) / DEBUG_MAP_ENTRY, MAX_DEBUG_MAP_BYTES)
    claim = begin_build()
    result = validate(config_raw, debug_raw, claim=claim)
    if isinstance(result, ConfigError):
        raise MqttActivationError(f"MQTT configuration rejected: {describe(result)}")
    ref = result.auth.credential_ref
    previous_vault = None
    if ref is not None and ref.staging_id is not None and claim is not None:
        try:
            previous_vault, active = vault().promote(ref.staging_id, claim.actor)
        except (VaultCorrupt, OSError) as exc:
            raise MqttActivationError("MQTT: the credential vault cannot be updated") from exc
        audit(
            "activate",
            "committed",
            claim.actor,
            staging_id=ref.staging_id,
            credential=f"{active.credential_id}#{active.revision}",
        )
    target = active_config_path(plugin)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if ref is not None:
            _install_atomically(locator_path(plugin), _locator(plugin))
        _install_atomically(target, config_raw)
    except OSError as exc:
        locator_path(plugin).unlink(missing_ok=True)
        if previous_vault is not None:
            vault().restore(previous_vault)
            audit("activate", "rolled_back", claim.actor if claim else None)
        raise MqttActivationError("MQTT: the configuration could not be installed") from exc
    plugin.config_path = str(target)
    plugin.enabled = True
    messages.append(f"MQTT: configuration installed ({result.mode}, {len(result.leaves)} leaves)")
    return messages


def status_socket(plugin: PluginConfig) -> Path:
    return active_config_path(plugin).with_name(STATUS_SOCKET_NAME)


def _installed_plugin() -> PluginConfig | None:
    """The MQTT plugin when a configuration is installed and enabled; no file is created."""
    if not os.path.exists(PLUGINS_CONF):
        return None
    plugin = PluginsConfiguration.from_file(PLUGINS_CONF).get_plugin(PLUGIN_NAME)
    if plugin is None or not plugin.enabled or not active_config_path(plugin).is_file():
        return None
    return plugin


def _is_status_document(reply: dict[str, object]) -> bool:
    return (
        reply.get("contract") == STATUS_CONTRACT
        and reply.get("version") == STATUS_VERSION
        and reply.get("state") in _STATES
        and isinstance(reply.get("configured"), bool)
    )


def _derived(
    state: State,
    config: MqttRuntimeConfig | None,
    error: tuple[str, str] | None = None,
) -> StatusDocument:
    tracker = StatusTracker(SystemClock())
    tracker.reset(state, config, error)
    return tracker.snapshot()


def status_document(plc_running: Callable[[], bool]) -> StatusDocument:
    """Body of ``GET /api/mqtt/status`` (``$defs/status``), sanitized like the plugin's."""
    plugin = _installed_plugin()
    if plugin is None:
        return _derived("disabled", None)
    try:
        reply = channel_request(str(status_socket(plugin)), REQUEST_STATUS, STATUS_TIMEOUT_S)
    except ChannelError:
        reply = None
    if reply is not None and _is_status_document(reply):
        return reply
    raw = _read_bounded(active_config_path(plugin), constants.MAX_CONFIG_BYTES)
    try:
        config = decode_runtime_config(raw or b"", None)
    except ConfigRejected as exc:
        return _derived("config_error", None, (exc.error.code, exc.error.message))
    if reply is not None or plc_running():
        return _derived("config_error", config, ("internal_error", _NOT_REPORTING))
    return _derived("plc_stopped", config)


def notify_credential_removed() -> str:
    """Ask the running plugin to stop a session that used the removed credential."""
    plugin = _installed_plugin()
    if plugin is None:
        return "not_installed"
    try:
        reply = channel_request(str(status_socket(plugin)), REQUEST_DROP_CREDENTIAL, DROP_TIMEOUT_S)
    except ChannelError:
        return "not_running"
    return "stopped" if reply.get("revoked") is True else "unaffected"
