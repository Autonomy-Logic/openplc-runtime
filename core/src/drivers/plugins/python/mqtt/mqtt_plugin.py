# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Runtime entry point of the MQTT / Sparkplug B plugin.

``start_loop`` activates the configuration the webserver installed: it decodes
it again with the runtime decoder, binds every leaf to the loaded program,
prepares the credential and TLS context, keeps them as the current generation
and starts its publisher thread; ``stop_loop`` and ``cleanup`` stop the
publisher within the shutdown budget and drop the generation. The previous
publisher is always stopped before a new generation is activated. While the
plugin runs it answers the webserver on its local status channel.

The MQTT client library and the Sparkplug codec are imported here, while the
plugin venv is first on ``sys.path``: a lazy import after the other plugins
are loaded would resolve to their unpinned copies. Without them the module
still loads, and activating the affected mode fails closed.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGINS_ROOT = os.path.dirname(_PLUGIN_DIR)
for _path in (_PLUGIN_DIR, _PLUGINS_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# pylint: disable=wrong-import-position
from contract.constants import MAX_CONFIG_BYTES
from contract.runtime_config import (
    ConfigError,
    ConfigRejected,
    MqttRuntimeConfig,
    decode_runtime_config,
)
from mqtt_credentials.session import SessionMaterial, Unsealer, prepare_session
from mqtt_credentials.unseal import request_unseal
from mqtt_publisher.backoff import Backoff
from mqtt_publisher.clock import Clock, SystemClock
from mqtt_publisher.connection import ConnectionFactory, ConnectSpec
from mqtt_publisher.generic import build_generic_strategy
from mqtt_publisher.status import (
    StatusDocument,
    StatusTracker,
    StatusWriter,
    activation_state,
)
from mqtt_publisher.status_channel import (
    REQUEST_DROP_CREDENTIAL,
    StatusServer,
    status_socket_path,
)
from mqtt_publisher.strategy import PublicationStrategy
from mqtt_publisher.supervisor import Publisher, Supervisor, SupervisorSettings
from mqtt_reader.reader import DebugAccess, LeafReader, bind_leaves
from mqtt_reader.runtime_access import RuntimeDebugAccess
from shared import SafeLoggingAccess, safe_extract_runtime_args_from_capsule
from shared.config_handler import ConfigHandler

try:
    from mqtt_publisher.transport import paho_connection as _paho_connection
except ImportError:
    _paho_connection = None  # pylint: disable=invalid-name

try:
    from mqtt_publisher import sparkplug as _sparkplug
except ImportError:
    _sparkplug = None  # pylint: disable=invalid-name

# pylint: enable=wrong-import-position

StrategyBuilder = Callable[[MqttRuntimeConfig, StatusWriter], PublicationStrategy]


def build_sparkplug_strategy(
    config: MqttRuntimeConfig, status: StatusWriter
) -> PublicationStrategy:
    if _sparkplug is None:
        raise ValueError("the pinned Sparkplug codec is not available")
    return _sparkplug.build_sparkplug_strategy(config, status, log=_log)


STRATEGY_BUILDERS: dict[str, StrategyBuilder] = {"generic": build_generic_strategy}
if _sparkplug is not None:
    STRATEGY_BUILDERS["sparkplug_b"] = build_sparkplug_strategy
TRANSPORT_FACTORY: ConnectionFactory | None = _paho_connection


@dataclass(frozen=True)
class ActiveGeneration:
    number: int
    config: MqttRuntimeConfig
    session: SessionMaterial
    reader: LeafReader | None


def load_active_config(path: str) -> MqttRuntimeConfig | ConfigError:
    """Read the installed configuration with the same bounds the webserver applies."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        return ConfigError("parse", "config_schema", "", "active configuration is not readable")
    try:
        return decode_runtime_config(raw, None)
    except ConfigRejected as exc:
        return exc.error


def tick_interval_ms(config: MqttRuntimeConfig) -> int:
    if config.generic is not None:
        return config.generic.interval_ms
    if config.sparkplug is not None:
        return config.sparkplug.sample_interval_ms
    raise ValueError("configuration has no publication section")


def _unavailable(message: str) -> ConfigError:
    return ConfigError("semantic", "internal_error", "/mode", message)


class PublisherFactory:  # pylint: disable=too-few-public-methods
    """Builds the publisher of a generation from the registered strategy and transport."""

    def __init__(
        self,
        strategies: dict[str, StrategyBuilder] | None = None,
        transport: ConnectionFactory | None = None,
        clock: Clock | None = None,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._strategies = STRATEGY_BUILDERS if strategies is None else strategies
        self._transport = transport
        self._clock = clock or SystemClock()
        self._log = log

    def __call__(
        self, generation: ActiveGeneration, tracker: StatusTracker
    ) -> Publisher | ConfigError:
        transport = self._transport if self._transport is not None else TRANSPORT_FACTORY
        if transport is None:
            return _unavailable("the pinned MQTT client library is not available")
        builder = self._strategies.get(generation.config.mode)
        if builder is None:
            return _unavailable("this publication mode is not available in this runtime")
        if generation.reader is None:
            return _unavailable("the leaves are not bound to the loaded program")
        config = generation.config
        session = generation.session
        spec = ConnectSpec(
            host=config.connection.host,
            port=config.connection.port,
            client_id=config.connection.client_id,
            tls_context=session.tls_context,
            username=session.username,
            password=session.password,
        )
        writer = tracker.reset("connecting", config)
        try:
            strategy = builder(config, writer)
        except ValueError:
            return _unavailable("the configuration does not fit the publication mode")
        supervisor = Supervisor(
            spec,
            transport,
            strategy,
            generation.reader,
            writer,
            self._clock,
            Backoff(),
            SupervisorSettings(tick_interval_s=tick_interval_ms(config) / 1000),
            self._log or _log,
        )
        return Publisher(supervisor, f"mqtt-publisher-{generation.number}")


class PluginState:
    """Single owner of the active configuration generation and its publisher."""

    def __init__(
        self,
        unseal: Unsealer = request_unseal,
        publishers: (
            Callable[[ActiveGeneration, StatusTracker], Publisher | ConfigError] | None
        ) = None,
        clock: Clock | None = None,
    ) -> None:
        self._unseal = unseal
        self._publishers = publishers or PublisherFactory()
        self._lock = threading.Lock()
        self._generation: ActiveGeneration | None = None
        self._publisher: Publisher | None = None
        self._counter = 0
        self.status = StatusTracker(clock or SystemClock())
        self.config_path = ""
        self.last_error: ConfigError | None = None

    @property
    def generation(self) -> ActiveGeneration | None:
        with self._lock:
            return self._generation

    @property
    def publisher(self) -> Publisher | None:
        with self._lock:
            return self._publisher

    def _stop_publisher(self) -> bool:
        publisher, self._publisher = self._publisher, None
        if publisher is None:
            return True
        stopped = publisher.stop()
        if not stopped:
            _log("error", "the previous publisher did not stop within the shutdown budget")
        return stopped

    def _reject(self, error: ConfigError, config: MqttRuntimeConfig | None) -> ConfigError:
        self.last_error = error
        self._generation = None
        self.status.reset(activation_state(error.code), config, (error.code, error.message))
        return error

    def activate(
        self, config_path: str, program: DebugAccess | None = None
    ) -> ActiveGeneration | ConfigError:
        """Activate ``config_path``; leaves are bound only when the loaded ``program`` is given."""
        with self._lock:
            self._stop_publisher()
            self._generation = None
            loaded = load_active_config(config_path)
            if isinstance(loaded, ConfigError):
                return self._reject(loaded, None)
            reader = None if program is None else bind_leaves(program, loaded.leaves)
            if isinstance(reader, ConfigError):
                return self._reject(reader, loaded)
            session = prepare_session(loaded, config_path, self._unseal)
            if isinstance(session, ConfigError):
                return self._reject(session, loaded)
            self._counter += 1
            self._generation = ActiveGeneration(self._counter, loaded, session, reader)
            self.last_error = None
            self.status.reset("plc_stopped", loaded)
            return self._generation

    def start(self, config_path: str, program: DebugAccess) -> ActiveGeneration | ConfigError:
        """Activate and start publishing; nothing connects unless every step succeeded."""
        generation = self.activate(config_path, program)
        if isinstance(generation, ConfigError):
            return generation
        with self._lock:
            if self._generation is not generation:
                return self._reject(_unavailable("activation was superseded"), generation.config)
            publisher = self._publishers(generation, self.status)
            if isinstance(publisher, ConfigError):
                return self._reject(publisher, generation.config)
            self._publisher = publisher
            publisher.start()
            return generation

    def deactivate(self, installed: bool = True) -> bool:
        """Stop the publisher and drop the generation; False when the stop overran the budget."""
        with self._lock:
            stopped = self._stop_publisher()
            config = None if self._generation is None else self._generation.config
            self._generation = None
            self.status.reset("plc_stopped" if installed else "disabled", config)
            return stopped

    def revoke_credential(self) -> bool:
        """Stop a session that uses a stored credential; True when one was stopped."""
        with self._lock:
            generation = self._generation
            if generation is None or generation.config.auth.credential_ref is None:
                return False
            self._stop_publisher()
            self._generation = None
            self.last_error = ConfigError(
                "credential", "credential_missing", "/auth/credential_ref", _REVOKED_MESSAGE
            )
            self.status.reset(
                "credential_error", generation.config, ("credential_missing", _REVOKED_MESSAGE)
            )
            return True

    def snapshot(self) -> StatusDocument:
        return self.status.snapshot()

    def handle_request(self, request: str) -> dict[str, object]:
        """Answer one request of the local status channel."""
        if request == REQUEST_DROP_CREDENTIAL:
            return {"revoked": self.revoke_credential()}
        return self.snapshot()


_REVOKED_MESSAGE = "the credential was removed from this runtime"
_state = PluginState()
_logging: SafeLoggingAccess | None = None
_program: DebugAccess | None = None
_channel: StatusServer | None = None


# Levels used by the plugin mapped to the methods SafeLoggingAccess really has.
LOG_METHODS: Final[dict[str, str]] = {
    "debug": "log_debug",
    "info": "log_info",
    "warning": "log_warn",
    "error": "log_error",
}


def _log(level: str, message: str) -> None:
    if _logging is not None and _logging.is_valid:
        getattr(_logging, LOG_METHODS.get(level, "log_error"))(f"[MQTT] {message}")
    else:
        print(f"[MQTT] {level.upper()}: {message}")


def describe_error(error: ConfigError) -> str:
    return f"configuration rejected: {error.code} at '{error.pointer}': {error.message}"


def init(args_capsule: object) -> bool:
    global _logging, _program
    runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
    if runtime_args is None:
        _log("error", f"cannot read runtime arguments: {error_msg}")
        return False
    _logging = SafeLoggingAccess(runtime_args)
    try:
        _program = RuntimeDebugAccess(runtime_args)
    except ValueError as exc:
        _program = None
        _log("error", f"program variable access unavailable: {exc}")
    path, path_error = ConfigHandler(runtime_args).get_config_path()
    _state.config_path = path or ""
    if not _state.config_path:
        _log("debug", f"no configuration path ({path_error})")
    return True


def _open_channel() -> None:
    global _channel
    _close_channel()
    channel = StatusServer(status_socket_path(_state.config_path), _state.handle_request, _log)
    _channel = channel if channel.start() else None


def _close_channel() -> bool:
    global _channel
    channel, _channel = _channel, None
    return True if channel is None else channel.stop()


def start_loop() -> bool:
    if not _state.config_path:
        _log("error", "no configuration installed; integration stays inactive")
        return False
    _open_channel()
    if _program is None:
        _log("error", "program variable access unavailable; integration stays inactive")
        return False
    result = _state.start(_state.config_path, _program)
    if isinstance(result, ConfigError):
        _log("error", describe_error(result))
        return False
    _log(
        "info",
        f"configuration active: generation {result.number}, mode {result.config.mode}, "
        f"{len(result.config.leaves)} leaves",
    )
    return True


def stop_loop() -> bool:
    stopped = _state.deactivate(os.path.isfile(_state.config_path))
    return _close_channel() and stopped


def cleanup() -> bool:
    stopped = _state.deactivate(os.path.isfile(_state.config_path))
    return _close_channel() and stopped


def status_snapshot() -> StatusDocument:
    return _state.snapshot()


__all__ = ["cleanup", "init", "start_loop", "status_snapshot", "stop_loop"]
