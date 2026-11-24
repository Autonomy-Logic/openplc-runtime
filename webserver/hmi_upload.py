# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The ``hmi/`` folder of a Runtime v4 upload (docs/HMI.md, "Upload artefact").

An upload may carry ``hmi/manifest.json``, ``hmi/src/`` (the host-side manifest: ``hmi_access.h``
and the generated ``hmi_access.c``, data only) and ``hmi/client/`` (the browser client, which runs
the screens). This module decides, from the entry names as they will be
extracted, whether the upload installs, removes or leaves the HMI alone, refuses anything else
under ``hmi/``, and keeps the previous HMI across the wipe of ``core/generated/`` when the upload
has none. ``scripts/compile-hmi.sh`` builds what ends up in ``core/generated/hmi/``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import IO, Final

HMI_DIR: Final[str] = "hmi"
MANIFEST: Final[str] = "hmi/manifest.json"
SRC_PREFIX: Final[str] = "hmi/src/"
CLIENT_PREFIX: Final[str] = "hmi/client/"
MANIFEST_MAX_BYTES: Final[int] = 1024
MANIFEST_FORMAT: Final[int] = 2
SRC_EXTENSIONS: Final[frozenset[str]] = frozenset({".c", ".h"})
CLIENT_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".html", ".js", ".css", ".wasm", ".txt", ".ttf"}
)
# Refused anywhere in an upload except as an HMI client file.
CLIENT_ONLY_EXTENSIONS: Final[frozenset[str]] = frozenset({".js", ".wasm", ".html"})
CLIENT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SRC_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# Where the previous HMI waits while core/generated/ is replaced.
STASH_NAME: Final[str] = ".hmi_previous"


class HmiUploadError(ValueError):
    """The upload's ``hmi/`` folder is not one the runtime accepts."""


class HmiAction(Enum):
    KEEP = "keep"  # no hmi/ in the upload: the installed HMI stays
    INSTALL = "install"
    REMOVE = "remove"


@dataclass(frozen=True)
class HmiPlan:
    action: HmiAction
    src_files: int = 0
    client_files: int = 0


def is_client_file(name: str) -> bool:
    """Whether ``name`` (an extracted path) is an allowed ``hmi/client/`` file; lower case
    extensions only."""
    if not name.startswith(CLIENT_PREFIX):
        return False
    base = name[len(CLIENT_PREFIX) :]
    if "/" in base or not CLIENT_NAME_RE.fullmatch(base) or base in (".", ".."):
        return False
    return os.path.splitext(base)[1] in CLIENT_EXTENSIONS


def extension_allowed(name: str, disallowed: tuple[str, ...]) -> bool:
    """The upload's extension rule for one extracted path: ``.js``, ``.wasm`` and ``.html`` only
    as HMI client files, the other ``disallowed`` extensions nowhere."""
    ext = os.path.splitext(name)[1].lower()
    if ext in CLIENT_ONLY_EXTENSIONS:
        return is_client_file(name)
    return ext not in disallowed


def _src_file_allowed(name: str) -> bool:
    """A ``.c`` or ``.h`` file directly in ``hmi/src/``."""
    rel = name[len(SRC_PREFIX) :]
    if "/" in rel or rel in (".", "..") or not SRC_NAME_RE.fullmatch(rel):
        return False
    return os.path.splitext(rel)[1] in SRC_EXTENSIONS


def parse_manifest(data: bytes) -> HmiAction:
    """``{"format": 2}`` installs, ``{"format": 2, "remove": true}`` removes; anything else,
    an HMI built for another runtime included, is refused."""
    if len(data) > MANIFEST_MAX_BYTES:
        raise HmiUploadError("hmi/manifest.json is too large")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HmiUploadError(f"hmi/manifest.json is not valid JSON: {e}") from e
    if not isinstance(manifest, dict):
        raise HmiUploadError("hmi/manifest.json must be an object")
    fmt = manifest.get("format")
    if type(fmt) is not int or fmt != MANIFEST_FORMAT:
        raise HmiUploadError(
            f"hmi/manifest.json format must be {MANIFEST_FORMAT}: this HMI was built by an editor "
            "this runtime does not match; build the project with an editor of the same release"
        )
    keys = set(manifest)
    if keys == {"format"}:
        return HmiAction.INSTALL
    if keys == {"format", "remove"} and manifest["remove"] is True:
        return HmiAction.REMOVE
    raise HmiUploadError("hmi/manifest.json has fields format 1 does not define")


def classify(names: list[str], manifest: bytes | None) -> HmiPlan:
    """The plan for an upload whose extracted file paths are ``names``.

    ``manifest`` is the content of ``hmi/manifest.json`` when the upload has one. Directory
    entries (ending in ``/``) are ignored.
    """
    hmi = [
        n for n in names if (n == HMI_DIR or n.startswith(HMI_DIR + "/")) and not n.endswith("/")
    ]
    if not hmi:
        return HmiPlan(HmiAction.KEEP)
    if MANIFEST not in hmi or manifest is None:
        raise HmiUploadError("hmi/ has no manifest.json")
    action = parse_manifest(manifest)
    others = [n for n in hmi if n != MANIFEST]
    if action is HmiAction.REMOVE:
        if others:
            raise HmiUploadError("an HMI removal carries nothing but hmi/manifest.json")
        return HmiPlan(HmiAction.REMOVE)

    src = client = 0
    for name in others:
        if name.startswith(SRC_PREFIX) and _src_file_allowed(name):
            src += 1
        elif name.startswith(CLIENT_PREFIX) and is_client_file(name):
            client += 1
        else:
            raise HmiUploadError(f"{name} is not allowed in an HMI")
    if src == 0:
        raise HmiUploadError("hmi/src/ has no sources")
    return HmiPlan(HmiAction.INSTALL, src_files=src, client_files=client)


def plan_from_zip(
    zip_path: str | IO[bytes], extracted: list[tuple[zipfile.ZipInfo, str]]
) -> HmiPlan:
    """The plan for an upload, from its entries paired with their extracted paths."""
    names = [name for _, name in extracted]
    manifest: bytes | None = None
    for info, name in extracted:
        if name == MANIFEST:
            if info.file_size > MANIFEST_MAX_BYTES:
                raise HmiUploadError("hmi/manifest.json is too large")
            with zipfile.ZipFile(zip_path, "r") as zf:
                manifest = zf.read(info)
            break
    return classify(names, manifest)


@contextmanager
def keep_previous(generated_dir: str, plan: HmiPlan) -> Iterator[None]:
    """Keeps ``<generated_dir>/hmi`` across the replacement of ``generated_dir`` when the upload
    has no ``hmi/``; on any exit the previous HMI is back in place unless one was extracted."""
    hmi_dir = os.path.join(generated_dir, HMI_DIR)
    stash = os.path.join(os.path.dirname(os.path.abspath(generated_dir)), STASH_NAME)
    if os.path.lexists(stash):
        shutil.rmtree(stash)
    stashed = False
    if plan.action is HmiAction.KEEP and os.path.isdir(hmi_dir) and not os.path.islink(hmi_dir):
        shutil.move(hmi_dir, stash)
        stashed = True
    try:
        yield
    finally:
        if stashed:
            if os.path.lexists(hmi_dir):
                shutil.rmtree(stash)
            else:
                os.makedirs(generated_dir, exist_ok=True)
                shutil.move(stash, hmi_dir)


def apply(generated_dir: str, plan: HmiPlan, log: Callable[[str], None]) -> None:
    """Finishes the HMI part of an extracted upload: a removal deletes ``<generated_dir>/hmi``."""
    hmi_dir = os.path.join(generated_dir, HMI_DIR)
    if plan.action is HmiAction.REMOVE:
        if os.path.lexists(hmi_dir):
            shutil.rmtree(hmi_dir)
        log("[INFO] HMI: removed by this upload\n")
    elif plan.action is HmiAction.INSTALL:
        log(
            f"[INFO] HMI: installed from upload ({plan.src_files} sources, "
            f"{plan.client_files} client files)\n"
        )
    elif os.path.isdir(hmi_dir):
        log("[INFO] HMI: kept the installed HMI (this upload carries none)\n")
