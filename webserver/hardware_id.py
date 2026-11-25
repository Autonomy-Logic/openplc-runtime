# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The hardware ID v2 this runtime advertises (spec: docs/HARDWARE_ID.md).

Every runtime (native, bootloader container, vPLC) computes its ID the same way:
the elements it reads from the hardware it sees, plus the element lines of the
optional read-only file ``/etc/openplc-runtime/elements`` (a vPLC gets the host's
elements and an ``instance`` element there from orchestrator-agent).

Installers write the record and ``ensure_stored()`` recomputes it on every
start, replacing it when the live ID differs or when it is malformed. The
stored record is kept when no ID can be computed.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from webserver.config import PERSISTENT_DATA_DIR
from webserver.logger import get_logger

logger, _ = get_logger("logger", use_buffer=True)

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "hardware-id.sh"
STORED_PATH = PERSISTENT_DATA_DIR / "hardware-id"
COMPUTE_TIMEOUT_S = 30

_ID_RE = re.compile(r"^hw2:[0-9a-f]{64}$")
_ELEMENT_RE = re.compile(r"^(serial|uuid|board|mac|cpu|instance) ([0-9a-f]{64})$")


@dataclass(frozen=True)
class Element:
    kind: str
    hash: str


@dataclass(frozen=True)
class HardwareId:
    value: str
    elements: tuple[Element, ...]


def parse_record(text: str) -> HardwareId | None:
    """Parse a record: the ID line, then one ``<kind> <hash>`` line per element."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not _ID_RE.match(lines[0]):
        return None
    elements: list[Element] = []
    for line in lines[1:]:
        match = _ELEMENT_RE.match(line)
        if not match:
            return None
        elements.append(Element(kind=match.group(1), hash=match.group(2)))
    return HardwareId(value=lines[0], elements=tuple(elements))


def _read(path: Path) -> HardwareId | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Cannot read hardware ID record %s: %s", path, exc)
        return None
    record = parse_record(text)
    if record is None:
        logger.warning("Ignoring malformed hardware ID record %s", path)
    return record


def current() -> HardwareId | None:
    """The stored record, or None when there is none or it is malformed."""
    return _read(STORED_PATH)


def ensure_stored() -> None:
    """Recompute the record and store it when it changed.

    When no ID can be computed, a well-formed stored record is kept.
    """
    previous = _read(STORED_PATH)
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
    if result.returncode != 0:
        logger.info("No hardware ID can be derived on this device (exit %d)", result.returncode)
        return
    record = _read(STORED_PATH)
    if record is None:
        logger.warning("Hardware ID record %s is unreadable after storing it", STORED_PATH)
    elif previous is None:
        logger.info("Hardware ID stored: %s", record.value)
    elif previous.value != record.value:
        logger.warning("Hardware ID changed from %s to %s", previous.value, record.value)
