# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The hardware ID v2 this runtime advertises (spec: docs/HARDWARE_ID.md).

Two sources, in order:

  * ``INJECTED_PATH`` -- a read-only record that orchestrator-agent places in a
    vPLC container before its first boot. A vPLC cannot see the host hardware,
    so this is the only ID it may advertise.
  * ``STORED_PATH`` -- the record the installers wrote to the persistent data
    directory (native install and bootloader container).

Installers compute the record once. ``ensure_stored()`` covers devices installed
before the ID existed: on start it computes the record only when none is stored,
and only where the host hardware is visible (native, or the bootloader's
privileged container). It never runs inside a vPLC, where a partial view of the
hardware would produce an ID that differs from the host's.
"""

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from webserver.config import PERSISTENT_DATA_DIR, is_running_in_container
from webserver.logger import get_logger

logger, _ = get_logger("logger", use_buffer=True)

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "hardware-id.sh"
STORED_PATH = PERSISTENT_DATA_DIR / "hardware-id"
INJECTED_PATH = Path("/etc/openplc-runtime/hardware-id")
COMPUTE_TIMEOUT_S = 30

_ID_RE = re.compile(r"^hw2:[0-9a-f]{64}$")
_ELEMENT_RE = re.compile(r"^(serial|uuid|board|mac|cpu) ([0-9a-f]{64})$")


@dataclass(frozen=True)
class Element:
    kind: str
    hash: str


@dataclass(frozen=True)
class HardwareId:
    value: str
    elements: tuple[Element, ...]
    source: str


def parse_record(text: str, source: str) -> HardwareId | None:
    """Parse a record: the ID line, then one ``<kind> <hash>`` line per element."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not _ID_RE.match(lines[0]):
        return None
    elements = []
    for line in lines[1:]:
        match = _ELEMENT_RE.match(line)
        if not match:
            return None
        elements.append(Element(kind=match.group(1), hash=match.group(2)))
    return HardwareId(value=lines[0], elements=tuple(elements), source=source)


def _read(path: Path, source: str) -> HardwareId | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Cannot read hardware ID record %s: %s", path, exc)
        return None
    record = parse_record(text, source)
    if record is None:
        logger.warning("Ignoring malformed hardware ID record %s", path)
    return record


def current() -> HardwareId | None:
    """The ID to advertise: the orchestrator's record in a vPLC, else the stored one."""
    return _read(INJECTED_PATH, "orchestrator") or _read(STORED_PATH, "device")


def _sees_host_hardware() -> bool:
    if platform.system() != "Linux":
        return False
    if not is_running_in_container():
        return True
    # The bootloader always sets this on the runtime container; vPLCs never do.
    return bool(os.environ.get("OPENPLC_PERSISTENT_DATA_DIR"))


def ensure_stored() -> None:
    """Compute and store the record once, when this runtime sees the host and has none."""
    if INJECTED_PATH.exists() or STORED_PATH.exists() or not _sees_host_hardware():
        return
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT_PATH), "--store", str(STORED_PATH)],
            capture_output=True,
            text=True,
            timeout=COMPUTE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Hardware ID computation failed: %s", exc)
        return
    if result.returncode == 0:
        record = _read(STORED_PATH, "device")
        logger.info("Hardware ID stored: %s", record.value if record else "unreadable")
    else:
        logger.info("No hardware ID can be derived on this device (exit %d)", result.returncode)
