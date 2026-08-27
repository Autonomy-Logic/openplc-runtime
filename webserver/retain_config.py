"""Persistent-storage (retain) settings for the built-in file store.

The runtime core reads ``retain.conf`` once per program load; this module is
the only thing that writes it.  Two separate processes touch retention, so the
split matters: the webserver owns the SETTINGS and the core owns the BYTES.

The file is deliberately a flat ``key=value`` list rather than JSON.  The core
parses it in C++ during startup, before anything else is available, and a
dependency-free parser for three keys is a better trade there than pulling a
JSON library into the PLC application.

A missing file means "nobody has configured retention on this device", which
is the default state and not an error.
"""

from __future__ import annotations

import os
from pathlib import Path

from webserver.config import PERSISTENT_DATA_DIR

# The runtime's working directory (systemd `WorkingDirectory=$OPENPLC_DIR`), so
# retain.conf lands beside plugins.conf where the core looks for it.
RUNTIME_ROOT = Path(os.path.abspath(os.path.dirname(__file__))).parent
RETAIN_CONF_PATH = RUNTIME_ROOT / "retain.conf"

DEFAULT_RETAIN_PATH = str(PERSISTENT_DATA_DIR / "retain.bin")
DEFAULT_FLUSH_SECONDS = 5

# Bounds on the flush period.  The floor is not arbitrary: the runtime hands the
# blob over every scan cycle, and a sub-second flush would write through at
# something close to scan rate, which is exactly what the buffering exists to
# avoid.  The ceiling keeps "enabled" from meaning "saved once an hour", which
# would look like retention and behave like none.
MIN_FLUSH_SECONDS = 1
MAX_FLUSH_SECONDS = 3600


class RetainConfigError(ValueError):
    """Raised for a setting the runtime would not be able to honour."""


def read_retain_config() -> dict:
    """Current settings, with defaults filled in for anything unset."""
    cfg = {
        "enabled": False,
        "path": DEFAULT_RETAIN_PATH,
        "flushSeconds": DEFAULT_FLUSH_SECONDS,
    }
    try:
        with open(RETAIN_CONF_PATH, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key == "enabled":
                    cfg["enabled"] = value in ("1", "true", "True")
                elif key == "path" and value:
                    cfg["path"] = value
                elif key == "flush_seconds":
                    try:
                        cfg["flushSeconds"] = int(value)
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return cfg


def validate_retain_path(path: str) -> str:
    """Normalise and sanity-check a store path.

    NOT a privilege boundary.  The caller is an authenticated admin of this
    runtime, who can already upload a program the runtime compiles and executes
    — so an arbitrary path is not an escalation, and pretending otherwise with
    a denylist would give false assurance.  What these checks are for is
    ordinary mistakes: a relative path (which would resolve against whatever
    directory the core happened to start in), a traversal that makes the stored
    location unobvious, and a directory that does not exist, where the store
    would fail on every flush with nothing but a log line to show for it.
    """
    candidate = (path or "").strip()
    if not candidate:
        raise RetainConfigError("A storage path is required when retention is enabled.")
    if not candidate.startswith("/"):
        raise RetainConfigError("The storage path must be absolute.")

    normalised = os.path.normpath(candidate)
    if normalised != candidate.rstrip("/") and normalised != candidate:
        raise RetainConfigError(
            f"The storage path must be given in normalised form (did you mean {normalised}?)."
        )
    if os.path.isdir(normalised):
        raise RetainConfigError("The storage path names a directory, not a file.")

    parent = os.path.dirname(normalised)
    if not os.path.isdir(parent):
        raise RetainConfigError(
            f"The directory {parent} does not exist, so nothing could be written there."
        )
    return normalised


def validate_flush_seconds(value) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise RetainConfigError("The flush period must be a whole number of seconds.")
    if seconds < MIN_FLUSH_SECONDS or seconds > MAX_FLUSH_SECONDS:
        raise RetainConfigError(
            f"The flush period must be between {MIN_FLUSH_SECONDS} and "
            f"{MAX_FLUSH_SECONDS} seconds."
        )
    return seconds


def write_retain_config(enabled: bool, path: str, flush_seconds: int) -> dict:
    """Persist the settings the core will read on the next program load."""
    resolved_path = validate_retain_path(path)
    seconds = validate_flush_seconds(flush_seconds)

    body = (
        "# Persistent storage for RETAIN variables.\n"
        "# Written by the OpenPLC runtime's REST API; read by the PLC\n"
        "# application at program load. Edits here are overwritten.\n"
        f"enabled={'1' if enabled else '0'}\n"
        f"path={resolved_path}\n"
        f"flush_seconds={seconds}\n"
    )

    # Write-and-rename, for the same reason the store itself does: a torn
    # retain.conf read at the next start would silently disable retention.
    tmp = RETAIN_CONF_PATH.with_suffix(".conf.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, RETAIN_CONF_PATH)

    return {"enabled": bool(enabled), "path": resolved_path, "flushSeconds": seconds}
