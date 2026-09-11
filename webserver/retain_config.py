"""Persistent-storage (retain) settings for the built-in file store.

WHO OWNS THESE SETTINGS
-----------------------
The PROJECT does. They are configured on the editor's Persistent Storage screen,
saved with the project, and emitted as ``retain.conf`` into the program upload —
the same way VPP plugin configuration travels. The webserver's only job is to
install what arrives (``apply_retain_conf`` in ``plcapp_management``) and to
refuse a stanza the core would not be able to honour. It does not author these
settings and there is no endpoint to change them on a running device: a device
configured out of band would disagree with the project that is running on it,
and the project is the thing a user can see.

The runtime core reads the installed ``retain.conf`` once per program load.

The file is deliberately a flat ``key=value`` list rather than JSON. The core
parses it in C++ during startup, before anything else is available, and a
dependency-free parser for three keys is a better trade there than pulling a
JSON library into the PLC application.

A missing file means "this project does not use persistent storage", which is
the default state and not an error. It is also how a target whose VPP owns
retention switches the built-in store off — see ``apply_retain_conf``.

WHAT THIS MODULE DOES NOT DO
----------------------------
It has nothing to say about retained VALUES. Whether the bytes on disk still
belong to the program now running is decided by the store itself, at program
start, by comparing the program MD5 it wrote alongside them. That keeps the
behaviour identical on baremetal, which has no webserver to consult.
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


def read_retain_conf_file(path: str | os.PathLike) -> dict:
    """Parse a ``retain.conf``, with defaults filled in for anything unset.

    Takes a path rather than assuming the runtime root, because the file worth
    checking is the one that just arrived in the upload — validating the
    installed copy would be validating it after the point where a refusal could
    still help.

    Mirrors the core's parser (``plc_retain_file_store.cpp``) key for key. A
    missing file yields the defaults, which is the disabled state.
    """
    cfg = {
        "enabled": False,
        "path": DEFAULT_RETAIN_PATH,
        "flushSeconds": DEFAULT_FLUSH_SECONDS,
    }
    try:
        with open(path, "r", encoding="utf-8") as handle:
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


def write_retain_conf_file(path: str | os.PathLike, *, enabled: bool, path_value: str,
                           flush_seconds: int) -> None:
    """Write a ``retain.conf`` the core can read.

    Used to install the RESOLVED stanza rather than byte-copying the upload: the
    project may leave ``path`` empty to mean "this device's default", and the
    core treats enabled-with-no-path as a misconfiguration, so the default has to
    be materialised into the file before it is installed.

    Write-and-rename, for the same reason the store itself does it: a torn
    retain.conf read at the next program load would silently disable retention.
    """
    body = (
        "# Persistent storage for RETAIN variables.\n"
        "# Installed from the program upload; the project's Persistent Storage\n"
        "# settings are the source. Read by the PLC application at program load.\n"
        "# Edits here are overwritten on the next upload.\n"
        f"enabled={'1' if enabled else '0'}\n"
        f"path={path_value}\n"
        f"flush_seconds={flush_seconds}\n"
    )
    target = Path(path)
    tmp = target.with_suffix(".conf.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def validate_retain_path(path: str) -> str:
    """Normalise and sanity-check a store path.

    NOT a privilege boundary.  The caller has uploaded a program this runtime
    compiles and executes — so an arbitrary path is not an escalation, and
    pretending otherwise with a denylist would give false assurance.  What these
    checks are for is ordinary mistakes: a relative path (which would resolve
    against whatever directory the core happened to start in), a traversal that
    makes the stored location unobvious, and a directory that does not exist,
    where the store would fail on every flush with nothing but a log line to
    show for it.
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
