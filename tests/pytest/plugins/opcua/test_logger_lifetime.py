"""
The OPC UA plugin's logger must not outlive one program load.

Its callbacks come from the runtime args of the load that initialised it, and
the runtime frees those args when the plugin is unloaded. A logger kept across
a Stop/Start calls into freed memory on the next init().
"""

from typing import List

import plugin
from opcua_logging import get_logger


class _RecordingAccessor:
    """Stands in for SafeLoggingAccess built on a previous load's runtime args."""

    is_valid = True

    def __init__(self) -> None:
        self.calls: List[str] = []

    def log_info(self, message: str) -> None:
        self.calls.append(message)

    def log_warn(self, message: str) -> None:
        self.calls.append(message)

    def log_error(self, message: str) -> None:
        self.calls.append(message)

    def log_debug(self, message: str) -> None:
        self.calls.append(message)


def _bind_previous_load() -> _RecordingAccessor:
    accessor = _RecordingAccessor()
    assert get_logger().initialize(accessor)
    return accessor


def test_cleanup_drops_the_runtime_log_callbacks() -> None:
    previous = _bind_previous_load()

    assert plugin.cleanup() is True
    calls_during_cleanup = len(previous.calls)
    get_logger().info("after cleanup")

    assert len(previous.calls) == calls_during_cleanup


def test_init_never_logs_through_the_previous_load() -> None:
    previous = _bind_previous_load()

    assert plugin.init(object()) is False

    assert previous.calls == []
