# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Runtime version exposed at /api/version and via the
X-OpenPLC-Runtime-Version response header.

Three resolution sources, in priority order:

  1. ``RUNTIME_VERSION`` env var — set by the CI Docker build via
     ``ARG RUNTIME_VERSION=${tag}`` → ``ENV RUNTIME_VERSION``.  Wins
     when present and non-empty (and not the Dockerfile's ``dev``
     placeholder).
  2. ``VERSION`` file at the repo root — committed and bumped per
     release branch, so a plain ``git clone … && ./install.sh`` on a
     user's target reports a real version instead of ``dev``.  This
     is the common-case fallback for users who don't go through CI.
  3. ``dev`` — last resort.  Reaching here means somebody removed
     the VERSION file AND skipped the Docker build-arg — likely
     working out of a stripped tree.

Editors gate firmware uploads on this string — a ``dev`` response
gets rejected because the editor can't tell whether the target
speaks the STruC++ wire format.  The VERSION file fallback closes
that hole for local installers.

=============================================================================
RELEASE STEP — DO THIS EVERY TIME YOU CUT A NEW VERSION TAG:
=============================================================================
Bump the repo-root ``VERSION`` file to match the new git tag BEFORE you
tag and push.  The Docker image gets its version from the tag (build-arg
above), but the Windows installer payload and every manual
``git clone … && ./install.sh`` install read the ``VERSION`` file directly.
If you forget, those installs keep advertising the OLD version — the
discovery scan and ``/api/version`` will be wrong even though the tag is
right.  This is a manual step on purpose: a CI-only fix (e.g. writing the
tag into the payload) would not cover manual source installs, which are
common.  Keep ``VERSION`` in sync with the tag by hand.

NOTE: the ``VERSION`` file is read verbatim (stripped of surrounding
whitespace only) — it must contain ONLY the version string and nothing
else (no comments). Match the tag format exactly, e.g. ``v4.1.3``.
=============================================================================
"""

import os
from pathlib import Path

# Advisory floor advertised at /api/capabilities as minEditorVersion. The
# editor enforces it; the runtime only advertises. Raise ONLY when an older
# editor would produce a bundle this runtime mis-compiles.
MIN_EDITOR_VERSION = "4.1.0"


def _resolve_runtime_version() -> str:
    # 1. Env var (CI Docker build-arg path).
    env = os.getenv("RUNTIME_VERSION", "").strip()
    if env and env != "dev":
        return env

    # 2. Repo-root VERSION file (local-install path).  __file__ is
    #    webserver/version.py; .parent.parent points at the repo root
    #    regardless of where the systemd service was started from, so
    #    the lookup doesn't depend on the process working directory.
    repo_version_file = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        text = repo_version_file.read_text(encoding="utf-8").strip()
        if text:
            return text
    except (FileNotFoundError, OSError):
        pass

    # 3. Last-resort fallback.
    return "dev"


RUNTIME_VERSION = _resolve_runtime_version()
