# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

import sys
from pathlib import Path

_MQTT_DIR = (
    Path(__file__).resolve().parents[4] / "core" / "src" / "drivers" / "plugins" / "python" / "mqtt"
)

for _path in (_MQTT_DIR.parent, _MQTT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
