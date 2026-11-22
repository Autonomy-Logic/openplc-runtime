# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Atomic, durable file replacement shared by the licence stores."""

import os
import tempfile
from pathlib import Path


def fsync_directory(directory: str | Path) -> None:
    """Best-effort fsync of ``directory`` so a rename inside it is durable; errors are ignored."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_file_atomically(path: str | Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` (mode 0644) so a failure leaves the previous file intact.

    Writes a ``mkstemp`` sibling, fsyncs it, renames it over ``path`` with ``os.replace`` and
    fsyncs the directory. Creates the directory when missing. Raises ``OSError`` on failure,
    after removing the temporary file.
    """
    directory = os.path.dirname(os.fspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".license-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    fsync_directory(directory)
