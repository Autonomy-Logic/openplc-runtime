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

from conftest import auth, create_user

from webserver import update_policy
from webserver.update_policy import BOOTLOADER_PORT, UPDATE_POLICY
from webserver.version import MIN_EDITOR_VERSION, RUNTIME_VERSION

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
        "updatePolicy": UPDATE_POLICY,
        "bootloaderPort": BOOTLOADER_PORT,
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


# --- update policy --------------------------------------------------------
#
# The policy tells a client WHO may change this runtime's version (RTOP-283).
# It is resolved once at import, so the resolver is exercised directly rather
# than by reloading the module: what matters is the decision, not the caching.


def test_update_policy_is_one_of_the_published_values(client):
    body = client.get("/api/capabilities").get_json()
    assert body["updatePolicy"] in update_policy.VALID_POLICIES


def test_explicit_override_wins_over_detection(monkeypatch):
    # The bootloader sets this when it creates the runtime container. It has to
    # beat detection, because a bootloader-managed runtime IS containerized and
    # would otherwise be mistaken for somebody else's vPLC.
    monkeypatch.setenv("OPENPLC_UPDATE_POLICY", "self")
    monkeypatch.setattr(update_policy, "is_running_in_container", lambda: True)
    assert update_policy.resolve_update_policy() == update_policy.POLICY_SELF


def test_override_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("OPENPLC_UPDATE_POLICY", "  NONE  ")
    assert update_policy.resolve_update_policy() == update_policy.POLICY_NONE


def test_container_without_an_override_is_managed(monkeypatch):
    # An orchestrator vPLC: something else created the container and therefore
    # chose the image tag, which is the version. We must not offer to update it.
    monkeypatch.delenv("OPENPLC_UPDATE_POLICY", raising=False)
    monkeypatch.setattr(update_policy, "is_running_in_container", lambda: True)
    assert update_policy.resolve_update_policy() == update_policy.POLICY_MANAGED


def test_native_install_without_an_override_is_manual(monkeypatch):
    monkeypatch.delenv("OPENPLC_UPDATE_POLICY", raising=False)
    monkeypatch.setattr(update_policy, "is_running_in_container", lambda: False)
    assert update_policy.resolve_update_policy() == update_policy.POLICY_MANUAL


def test_an_unrecognised_override_falls_through_to_detection(monkeypatch):
    # A typo must not be read as permission. Falling through means the worst
    # case is "we refuse an update that was actually allowed", never the
    # reverse.
    monkeypatch.setenv("OPENPLC_UPDATE_POLICY", "yes-please")
    monkeypatch.setattr(update_policy, "is_running_in_container", lambda: True)
    assert update_policy.resolve_update_policy() == update_policy.POLICY_MANAGED


# --- bootloader port ---------------------------------------------------------


def test_bootloader_port_is_absent_unless_the_policy_is_self():
    # Publishing a port nothing answers on would send clients somewhere
    # useless, so every non-self policy reports None.
    for policy in (
        update_policy.POLICY_MANAGED,
        update_policy.POLICY_MANUAL,
        update_policy.POLICY_NONE,
    ):
        assert update_policy.resolve_bootloader_port(policy) is None, policy


def test_bootloader_port_defaults_when_the_policy_is_self(monkeypatch):
    monkeypatch.delenv("OPENPLC_BOOTLOADER_PORT", raising=False)
    assert (
        update_policy.resolve_bootloader_port(update_policy.POLICY_SELF)
        == update_policy.DEFAULT_BOOTLOADER_PORT
    )


def test_bootloader_port_honours_an_explicit_value(monkeypatch):
    monkeypatch.setenv("OPENPLC_BOOTLOADER_PORT", "9445")
    assert update_policy.resolve_bootloader_port(update_policy.POLICY_SELF) == 9445


def test_an_unusable_bootloader_port_falls_back_to_the_default(monkeypatch):
    # Garbage or an out-of-range port means the bootloader is still there on the
    # port it almost certainly used; refusing to report one at all would hide
    # a working bootloader behind a config typo.
    for raw in ("not-a-port", "0", "70000", "-1"):
        monkeypatch.setenv("OPENPLC_BOOTLOADER_PORT", raw)
        assert (
            update_policy.resolve_bootloader_port(update_policy.POLICY_SELF)
            == update_policy.DEFAULT_BOOTLOADER_PORT
        ), raw


# --- device info ----------------------------------------------------------


def test_device_info_requires_a_token(client):
    # Unlike /capabilities: the policy has to be readable before login, but
    # kernel and architecture are only for somebody already authenticated.
    assert client.get("/api/device-info").status_code == 401


def test_device_info_reports_host_facts(client, admin_token):
    body = client.get("/api/device-info", headers=auth(admin_token)).get_json()
    assert set(body) == {
        "hostname",
        "architecture",
        "kernel",
        "system",
        "containerized",
        "updatePolicy",
        "bootloaderPort",
    }
    assert body["hostname"]
    assert body["architecture"]
    assert isinstance(body["containerized"], bool)


def test_device_info_agrees_with_capabilities_on_the_policy(client, admin_token):
    # Two routes, one answer -- a client that reads either must not be able to
    # reach a different conclusion about whether an update is possible.
    capabilities = client.get("/api/capabilities").get_json()
    info = client.get("/api/device-info", headers=auth(admin_token)).get_json()
    assert info["updatePolicy"] == capabilities["updatePolicy"]
    assert info["bootloaderPort"] == capabilities["bootloaderPort"]


def test_device_info_is_not_swallowed_by_the_command_catch_all(client, admin_token):
    # restapi_bp owns a GET /api/<command> catch-all that forwards to the PLC
    # command handler. device-info lives on a different blueprint, so this
    # pins the routing precedence: a static rule must win over the converter,
    # or the editor's header request would be dispatched as a PLC command.
    resp = client.get("/api/device-info", headers=auth(admin_token))
    assert resp.status_code == 200
    assert resp.get_json()["hostname"]
