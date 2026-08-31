"""Storage for the source project snapshot a client sends with an upload.

The editor compiles a project and uploads only the build artifacts, so nothing
on the device has ever recorded the project the artifacts came from.  This
module stores an optional snapshot of that project so an admin can retrieve it
later ("Retrieve Project from PLC").

Two rules shape everything here:

  * **The blob is opaque.**  The runtime never opens the archive, never parses
    it, never validates its contents.  Everything the device needs to *say*
    about the stored project arrives as separate metadata alongside the bytes.
    Keeping this property is what keeps the device side small: the archive
    format can change entirely without touching the runtime.

  * **The stored project must match the running program.**  An upload clears
    the snapshot first and only promotes the new one once the build succeeds.
    A device therefore never advertises a project it is not running, and an
    upload from a client that sends no snapshot (an older editor, the CLI, any
    third party) correctly leaves the device with none.

The staged/promoted split exists for the second rule.  ``stage()`` runs when
the upload arrives, ``promote()`` when the build succeeds, and
``discard_staged()`` when it fails -- which matches what the build itself does
to the program: ``compile-clean.sh`` removes ``libplc_*.so`` before it has a
replacement, so a failed build leaves no program, and "no program, no stored
project" is the accurate state.

Note the snapshot deliberately survives a credentials reset (``config.py``
deletes ``restapi.db`` when ``.env`` is regenerated).  Retrieval is gated on
holding admin credentials at the time of the request, so a freshly created
admin set is expected to be able to retrieve the stored project.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Final, Optional

from webserver.config import PERSISTENT_DATA_DIR
from webserver.logger import get_logger

logger, _ = get_logger(__name__)

SNAPSHOT_DIR: Final[Path] = PERSISTENT_DATA_DIR / "project_snapshot"

_PROMOTED_BLOB: Final[Path] = SNAPSHOT_DIR / "project.zip"
_PROMOTED_META: Final[Path] = SNAPSHOT_DIR / "project.json"
_STAGED_BLOB: Final[Path] = SNAPSHOT_DIR / "staged.zip"
_STAGED_META: Final[Path] = SNAPSHOT_DIR / "staged.json"

# Cap for the snapshot field.  Deliberately its own constant and much larger
# than the program-zip limits in plcapp_management: those guard an archive that
# gets extracted and compiled, while this one is stored untouched, and a real
# project carrying its bundled libraries is a great deal bigger than the
# generated sources.
MAX_SNAPSHOT_BYTES: Final[int] = 100 * 1024 * 1024

# Bounds on the metadata the device will repeat back to clients.  This is not
# security -- an authenticated client could write anything -- it just stops a
# malformed or hostile upload from parking unbounded junk in the discovery
# payload and the info endpoint.
_MAX_STRING_LEN: Final[int] = 512
_MAX_LIBRARIES: Final[int] = 256


class SnapshotError(ValueError):
    """Raised for a snapshot the runtime will not store."""


def _ensure_dir() -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _clean_string(value: Any) -> str:
    """One metadata string, truncated and stripped of control characters.

    Control characters matter because these values land in a JSON discovery
    reply and in client UI; a newline in a project name should not be able to
    reshape either.
    """
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned.strip()[:_MAX_STRING_LEN]


def normalize_metadata(raw: Any) -> dict:
    """Coerce client-supplied metadata into the record the device will serve.

    Unknown keys are dropped rather than stored: everything here is echoed back
    to other clients, so the shape is the device's, not the uploader's.
    """
    if not isinstance(raw, dict):
        raise SnapshotError("Snapshot metadata must be a JSON object")

    libraries = []
    raw_libraries = raw.get("libraries")
    if isinstance(raw_libraries, list):
        for entry in raw_libraries[:_MAX_LIBRARIES]:
            if not isinstance(entry, dict):
                continue
            name = _clean_string(entry.get("name"))
            if not name:
                continue
            libraries.append(
                {
                    "name": name,
                    "version": _clean_string(entry.get("version")),
                    "hash": _clean_string(entry.get("hash")),
                }
            )

    format_version = raw.get("formatVersion")
    if not isinstance(format_version, int) or format_version < 1:
        raise SnapshotError("Snapshot metadata needs an integer formatVersion of 1 or more")

    project_name = _clean_string(raw.get("projectName"))
    if not project_name:
        raise SnapshotError("Snapshot metadata needs a projectName")

    return {
        "formatVersion": format_version,
        "projectName": project_name,
        "editorVersion": _clean_string(raw.get("editorVersion")),
        "uploadedBy": _clean_string(raw.get("uploadedBy")),
        "timestamp": _clean_string(raw.get("timestamp")),
        "libraries": libraries,
    }


def clear() -> None:
    """Remove the stored snapshot and anything staged.

    Called at the start of every upload.  A missing directory is the normal
    state on a device that has never stored one, not an error.
    """
    try:
        shutil.rmtree(SNAPSHOT_DIR)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not clear the stored project snapshot: %s", exc)


def stage(blob: bytes, metadata: dict) -> None:
    """Replace the stored snapshot with one awaiting a successful build.

    Clearing first is what makes an upload carrying no snapshot erase the old
    one -- the caller simply does not follow with a stage().
    """
    if len(blob) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError(
            f"Project snapshot is too large " f"({len(blob)} bytes, limit {MAX_SNAPSHOT_BYTES})"
        )

    record = dict(metadata)
    record["sizeBytes"] = len(blob)

    clear()
    _ensure_dir()
    try:
        _STAGED_BLOB.write_bytes(blob)
        _STAGED_META.write_text(json.dumps(record), encoding="utf-8")
    except OSError as exc:
        # Leave nothing half-written: a staged blob without its metadata would
        # be promoted into a snapshot the info endpoint cannot describe.
        clear()
        raise SnapshotError(f"Could not store the project snapshot: {exc}") from exc

    logger.info("Project snapshot staged (%d bytes)", len(blob))


def promote() -> bool:
    """Make the staged snapshot the stored one.  Returns True if one was staged."""
    if not (_STAGED_BLOB.exists() and _STAGED_META.exists()):
        return False
    try:
        os.replace(_STAGED_BLOB, _PROMOTED_BLOB)
        os.replace(_STAGED_META, _PROMOTED_META)
    except OSError as exc:
        logger.error("Could not promote the staged project snapshot: %s", exc)
        discard_staged()
        return False
    logger.info("Project snapshot promoted")
    return True


def discard_staged() -> None:
    """Drop a staged snapshot after a failed build."""
    for path in (_STAGED_BLOB, _STAGED_META):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not discard the staged project snapshot: %s", exc)


def read_metadata() -> Optional[dict]:
    """Metadata for the stored snapshot, or None when none is stored."""
    try:
        record = json.loads(_PROMOTED_META.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Stored project snapshot metadata is unreadable: %s", exc)
        return None
    if not isinstance(record, dict):
        return None
    # A metadata file without its blob describes nothing retrievable.
    if not _PROMOTED_BLOB.exists():
        return None
    return record


def read_blob() -> Optional[bytes]:
    """Bytes of the stored snapshot, or None when none is stored."""
    try:
        return _PROMOTED_BLOB.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.error("Could not read the stored project snapshot: %s", exc)
        return None


def has_snapshot() -> bool:
    return read_metadata() is not None


def advertised_fields() -> dict:
    """The subset of the metadata the unauthenticated discovery reply carries.

    Name and timestamp only: enough for a client to populate its device picker
    without a login per device, and nothing beyond what the picker shows.
    Absent keys mean no stored project, so no separate flag is needed.
    """
    record = read_metadata()
    if record is None:
        return {}
    return {
        "project_name": record.get("projectName", ""),
        "project_timestamp": record.get("timestamp", ""),
    }
