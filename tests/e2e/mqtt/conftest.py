# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Session fixtures of the MQTT end-to-end tests (Docker, runtime image, Editor-built payloads).

Skipped unless MQTT_E2E_RUNTIME_IMAGE and MQTT_E2E_PAYLOADS are set; run.sh sets both.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for _path in (HERE, HERE.parents[1] / "pytest" / "plugins" / "mqtt"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# pylint: disable=wrong-import-position
from e2e_env import DEFAULT_MOSQUITTO, Network, Runtime, env_paths

# pylint: enable=wrong-import-position


@pytest.fixture(scope="session")
def settings() -> tuple[str, Path]:
    found = env_paths()
    if found is None:
        pytest.skip("set MQTT_E2E_RUNTIME_IMAGE and MQTT_E2E_PAYLOADS (see tests/e2e/mqtt/run.sh)")
    return found


@pytest.fixture(scope="session")
def payloads(settings: tuple[str, Path]) -> Path:
    return settings[1]


@pytest.fixture(scope="session")
def network(settings: tuple[str, Path]) -> Iterator[Network]:
    net = Network(os.environ.get("MQTT_E2E_MOSQUITTO", DEFAULT_MOSQUITTO))
    net.up()
    yield net
    net.cleanup()


@pytest.fixture(scope="session")
def runtime(settings: tuple[str, Path], network: Network) -> Iterator[Runtime]:
    rt = Runtime(settings[0], network)
    rt.up()
    yield rt
    log_dir = os.environ.get("MQTT_E2E_LOG_DIR", "")
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        (Path(log_dir) / "runtime.log").write_text(rt.logs(), encoding="utf-8")
    rt.cleanup()
