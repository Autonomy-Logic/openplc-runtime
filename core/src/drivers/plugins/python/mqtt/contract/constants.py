# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""MQTT contract constants. Mirror of ``src/frontend/utils/mqtt/contract/contract.ts``."""

from __future__ import annotations

from typing import Final

RUNTIME_CONFIG_CONTRACT: Final = "openplc-mqtt-runtime-config"
RUNTIME_CONFIG_FORMAT_VERSION: Final = 1
RUNTIME_CONFIG_FILE: Final = "conf/mqtt.json"

MAX_LEAVES: Final = 1000
MAX_NAME_BYTES: Final = 512
MAX_PAYLOAD_BYTES: Final = 262_144
MAX_CONFIG_BYTES: Final = 4_194_304
MAX_CONFIG_DEPTH: Final = 32
MAX_CA_PEM_CHARS: Final = 65_536
MIN_INTERVAL_MS: Final = 100
MAX_INTERVAL_MS: Final = 60_000

CONNECT_TIMEOUT_MS: Final = 10_000
KEEPALIVE_S: Final = 30
BACKOFF_BASES_S: Final = (1, 2, 4, 8, 16, 30)
BACKOFF_JITTER: Final = 0.2
BACKOFF_RESET_AFTER_MS: Final = 60_000
SHUTDOWN_BUDGET_MS: Final = 5_000
IDENTICAL_ERROR_LOG_INTERVAL_MS: Final = 30_000

STAGING_TTL_S: Final = 600
MAX_STAGED: Final = 4

DEBUG_SIZE: Final = {
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
