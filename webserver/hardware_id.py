# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The hardware ID v2 this runtime advertises (spec: docs/HARDWARE_ID.md).

Two sources, in order:

  * ``INJECTED_PATH`` -- a read-only record that orchestrator-agent places in a
    vPLC container before its first boot. A vPLC cannot see the host hardware,
    so this is the only ID it may advertise.
  * ``STORED_PATH`` -- the record in the persistent data directory (native
    install and bootloader container), advertised only where the host hardware
    is visible.

Installers write the record and ``ensure_stored()`` recomputes it on every
start, replacing it when the live ID differs (a cloned disk, changed hardware)
or when it is malformed. The stored record is kept when no ID can be computed.

The host hardware is visible natively on Linux and in a container that has
every capability (privileged) and the host network. Elsewhere (Windows, an
unprivileged or bridged container, a run without access to the root-only DMI
files) a partial view would give another ID, so no ID is computed or
advertised.
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
PROC_DIR = Path("/proc")
SYS_DIR = Path("/sys")
COMPUTE_TIMEOUT_S = 30
# Set by the bootloader on the runtime container it starts; other containers derive no ID.
BOOTLOADER_CONTAINER_ENV = "OPENPLC_PERSISTENT_DATA_DIR"

_ID_RE = re.compile(r"^hw2:[0-9a-f]{64}$")
_ELEMENT_RE = re.compile(r"^(serial|uuid|board|mac|cpu) ([0-9a-f]{64})$")
_CAP_BOUNDING_RE = re.compile(r"^CapBnd:\s*([0-9a-fA-F]+)\s*$", re.MULTILINE)
_ROOT_ONLY_DMI = ("product_uuid", "board_serial")
# Highest capability the bounding set must hold: CAP_AUDIT_READ.
_CHECKED_CAP_LAST = 37


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
    elements: list[Element] = []
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
    """The ID to advertise: the orchestrator's record in a vPLC, else the stored one
    where the host hardware is visible, else None."""
    injected = _read(INJECTED_PATH, "orchestrator")
    if injected is not None:
        return injected
    if not _sees_host_hardware():
        return None
    return _read(STORED_PATH, "device")


def _has_all_capabilities() -> bool:
    """True when the bounding set holds capabilities 0 to ``_CHECKED_CAP_LAST`` (privileged)."""
    try:
        last_cap = int((PROC_DIR / "sys" / "kernel" / "cap_last_cap").read_text().strip())
        status = (PROC_DIR / "self" / "status").read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        logger.debug("Cannot read the capability set: %s", exc)
        return False
    match = _CAP_BOUNDING_RE.search(status)
    if match is None:
        return False
    full = (1 << (min(last_cap, _CHECKED_CAP_LAST) + 1)) - 1
    return int(match.group(1), 16) & full == full


def _sees_physical_nic() -> bool:
    """True when a network interface backed by a device is visible (host network namespace)."""
    try:
        return any((nic / "device").exists() for nic in (SYS_DIR / "class" / "net").iterdir())
    except OSError:
        return False


def _reads_root_only_dmi() -> bool:
    """False when a root-only DMI file exists but this process cannot read it."""
    dmi = SYS_DIR / "class" / "dmi" / "id"
    return all(
        not (dmi / name).exists() or os.access(dmi / name, os.R_OK) for name in _ROOT_ONLY_DMI
    )


def _sees_host_hardware() -> bool:
    if platform.system() != "Linux" or not _reads_root_only_dmi():
        return False
    if not is_running_in_container():
        return True
    return (
        bool(os.environ.get(BOOTLOADER_CONTAINER_ENV))
        and _has_all_capabilities()
        and _sees_physical_nic()
    )


def ensure_stored() -> None:
    """Recompute the record where the host hardware is visible and store it when it changed.

    A vPLC (injected record) and a runtime that cannot see the host hardware never compute one.
    When no ID can be computed, a well-formed stored record is kept.
    """
    if INJECTED_PATH.exists() or not _sees_host_hardware():
        return
    previous = _read(STORED_PATH, "device")
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
    record = _read(STORED_PATH, "device")
    if record is None:
        logger.warning("Hardware ID record %s is unreadable after storing it", STORED_PATH)
    elif previous is None:
        logger.info("Hardware ID stored: %s", record.value)
    elif previous.value != record.value:
        logger.warning("Hardware ID changed from %s to %s", previous.value, record.value)
