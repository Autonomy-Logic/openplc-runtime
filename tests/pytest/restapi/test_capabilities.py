"""Behavioural tests for GET /api/version and GET /api/capabilities.

Both endpoints exist so an editor can decide, before login, whether it may
talk to this runtime at all (DOPE-448). The contract these tests pin down:

  * both are reachable WITHOUT a token, even once users exist — an editor that
    cannot authenticate must still be able to tell why;
  * ``/api/capabilities`` reports the same version string as ``/api/version``,
    so the two can never disagree about what runtime this is;
  * ``minEditorVersion`` is present and parseable as a version, because the
    editor compares it numerically and a malformed value would either block
    every editor or none.
"""

import re

from webserver.version import MIN_EDITOR_VERSION, RUNTIME_VERSION

from conftest import create_user

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


# --- reachability ---------------------------------------------------------


def test_capabilities_is_reachable_without_a_token(client):
    resp = client.get("/api/capabilities")
    assert resp.status_code == 200


def test_capabilities_stays_unauthenticated_once_users_exist(client):
    # The editor needs the compatibility answer even when it holds no
    # credentials for this device, so creating a user must not close the door.
    create_user(client, "admin", "admin-pass")
    assert client.get("/api/capabilities").status_code == 200


def test_version_is_reachable_without_a_token(client):
    assert client.get("/api/version").status_code == 200


# --- payload --------------------------------------------------------------


def test_capabilities_reports_runtime_version_and_editor_floor(client):
    body = client.get("/api/capabilities").get_json()
    assert body == {
        "runtimeVersion": RUNTIME_VERSION,
        "minEditorVersion": MIN_EDITOR_VERSION,
        "projectSnapshot": True,
    }


def test_capabilities_and_version_agree_on_the_runtime_version(client):
    capabilities = client.get("/api/capabilities").get_json()
    version = client.get("/api/version").get_json()
    assert capabilities["runtimeVersion"] == version["version"]


def test_min_editor_version_is_a_plain_three_part_version():
    # The editor parses this and compares it against its own APP_VERSION. A
    # tag-style value ("v4.1.0") or a partial one ("4.1") would make that
    # comparison ambiguous, so the published floor stays a bare x.y.z.
    assert _VERSION_RE.match(MIN_EDITOR_VERSION), MIN_EDITOR_VERSION


# --- headers --------------------------------------------------------------


def test_runtime_version_header_is_present_on_capabilities(client):
    # Older editors read the version off this header rather than a body; the
    # after_request hook must cover the new route too.
    resp = client.get("/api/capabilities")
    assert resp.headers["X-OpenPLC-Runtime-Version"] == RUNTIME_VERSION
