"""Behavioural tests for the stored source-project snapshot.

The device stores an optional archive of the project a program was built from
so an admin can retrieve it later. Two properties carry the whole design and
are what these tests pin down:

  * **The stored project never outlives the program it belongs to.** An upload
    clears it, a successful build promotes the new one, and anything else
    leaves the device with none. A device that advertised a project it was not
    running would be worse than one that advertises nothing.

  * **The runtime never opens the archive.** Everything it reports about the
    stored project comes from metadata sent alongside the bytes, so these tests
    deliberately stage archives that are not valid ZIPs at all.

Retrieval is admin-only, and that role check is the *whole* of the access
control -- the archive is not encrypted on disk -- so the refusal paths matter
as much as the happy path.
"""

import base64
import json

import pytest
from conftest import auth, create_user, token_for

from webserver import project_snapshot


@pytest.fixture(autouse=True)
def clean_snapshot_store():
    """No test may see another's stored project."""
    project_snapshot.clear()
    yield
    project_snapshot.clear()


def _metadata(**overrides):
    record = {
        "formatVersion": 1,
        "projectName": "Traffic Light",
        "editorVersion": "4.2.0",
        "uploadedBy": "admin",
        "timestamp": "2026-08-31T12:00:00Z",
        "libraries": [{"name": "Motion", "version": "1.2.0", "hash": "abc123"}],
    }
    record.update(overrides)
    return project_snapshot.normalize_metadata(record)


def _store(blob=b"not-a-real-zip-on-purpose", **overrides):
    """Stage and promote, i.e. the state after a successful build."""
    project_snapshot.stage(blob, _metadata(**overrides))
    assert project_snapshot.promote() is True


# --- the stage / promote / discard cycle ---------------------------------


def test_nothing_is_stored_on_a_fresh_device():
    assert project_snapshot.has_snapshot() is False
    assert project_snapshot.read_metadata() is None
    assert project_snapshot.read_blob() is None


def test_a_staged_snapshot_is_not_yet_readable():
    # Until the build succeeds the device has no business claiming a project:
    # the program it would describe does not exist yet.
    project_snapshot.stage(b"payload", _metadata())
    assert project_snapshot.has_snapshot() is False


def test_promote_makes_the_staged_snapshot_the_stored_one():
    project_snapshot.stage(b"payload", _metadata())
    assert project_snapshot.promote() is True
    assert project_snapshot.read_blob() == b"payload"
    assert project_snapshot.read_metadata()["projectName"] == "Traffic Light"


def test_discard_leaves_the_device_with_nothing():
    # The failed-build path. compile-clean.sh has already removed the program's
    # .so by this point, so "no program, no stored project" is the honest state.
    project_snapshot.stage(b"payload", _metadata())
    project_snapshot.discard_staged()
    assert project_snapshot.promote() is False
    assert project_snapshot.has_snapshot() is False


def test_promote_without_anything_staged_is_a_no_op():
    # An upload that sent no snapshot still reaches promote() when its build
    # succeeds. It must not resurrect anything.
    assert project_snapshot.promote() is False
    assert project_snapshot.has_snapshot() is False


def test_clear_then_no_stage_erases_the_previous_project():
    # This is exactly what an upload from an older editor does, and it is the
    # point: the device stops advertising a project it is no longer running.
    _store()
    project_snapshot.clear()
    assert project_snapshot.has_snapshot() is False


def test_a_second_upload_replaces_the_first():
    _store(b"first", projectName="First")
    project_snapshot.clear()
    project_snapshot.stage(b"second", _metadata(projectName="Second"))
    project_snapshot.promote()
    assert project_snapshot.read_blob() == b"second"
    assert project_snapshot.read_metadata()["projectName"] == "Second"


def test_a_failed_build_after_a_successful_one_leaves_nothing():
    # The dangerous middle state: an old snapshot must not survive an upload
    # whose build failed, because the old PROGRAM did not survive it either.
    _store(b"first", projectName="First")
    project_snapshot.clear()
    project_snapshot.stage(b"second", _metadata(projectName="Second"))
    project_snapshot.discard_staged()
    assert project_snapshot.has_snapshot() is False


def test_an_oversized_snapshot_is_refused():
    oversized = b"x" * (project_snapshot.MAX_SNAPSHOT_BYTES + 1)
    with pytest.raises(project_snapshot.SnapshotError):
        project_snapshot.stage(oversized, _metadata())
    assert project_snapshot.has_snapshot() is False


def test_metadata_without_its_blob_describes_nothing():
    # Defence against a half-written store: metadata alone is not a snapshot,
    # because there is nothing to hand back.
    _store()
    project_snapshot._PROMOTED_BLOB.unlink()
    assert project_snapshot.read_metadata() is None
    assert project_snapshot.has_snapshot() is False


# --- metadata normalisation ----------------------------------------------


def test_metadata_requires_a_project_name():
    with pytest.raises(project_snapshot.SnapshotError):
        project_snapshot.normalize_metadata({"formatVersion": 1, "projectName": "  "})


def test_metadata_requires_a_format_version():
    with pytest.raises(project_snapshot.SnapshotError):
        project_snapshot.normalize_metadata({"projectName": "P"})


def test_metadata_rejects_a_non_object():
    with pytest.raises(project_snapshot.SnapshotError):
        project_snapshot.normalize_metadata(["not", "an", "object"])


def test_unknown_metadata_keys_are_dropped():
    # The device echoes this record to other clients, so the shape is the
    # device's, not the uploader's.
    record = project_snapshot.normalize_metadata(
        {"formatVersion": 1, "projectName": "P", "somethingElse": "ignored"}
    )
    assert "somethingElse" not in record


def test_control_characters_are_stripped_from_metadata_strings():
    # These values land in a JSON discovery reply and in client UI; a newline in
    # a project name should not be able to reshape either.
    record = project_snapshot.normalize_metadata(
        {"formatVersion": 1, "projectName": "Line\nBreak\tProject"}
    )
    assert "\n" not in record["projectName"]
    assert "\t" not in record["projectName"]


def test_malformed_library_entries_are_dropped_not_fatal():
    record = project_snapshot.normalize_metadata(
        {
            "formatVersion": 1,
            "projectName": "P",
            "libraries": ["not-an-object", {"version": "1.0"}, {"name": "Real"}],
        }
    )
    assert [lib["name"] for lib in record["libraries"]] == ["Real"]


# --- discovery advertisement ---------------------------------------------


def test_discovery_advertises_nothing_when_no_project_is_stored():
    # Absence of the keys is how a client tells there is nothing to retrieve;
    # there is deliberately no separate boolean.
    assert project_snapshot.advertised_fields() == {}


def test_discovery_advertises_only_name_and_timestamp():
    _store()
    fields = project_snapshot.advertised_fields()
    assert fields == {
        "project_name": "Traffic Light",
        "project_timestamp": "2026-08-31T12:00:00Z",
    }


# --- GET /api/project-snapshot/info --------------------------------------


def test_info_requires_authentication(client):
    assert client.get("/api/project-snapshot/info").status_code == 401


def test_info_reports_absence_on_a_fresh_device(client, admin_token):
    body = client.get("/api/project-snapshot/info", headers=auth(admin_token)).get_json()
    assert body == {"present": False}


def test_info_reports_the_stored_project(client, admin_token):
    _store(b"payload")
    body = client.get("/api/project-snapshot/info", headers=auth(admin_token)).get_json()
    assert body["present"] is True
    assert body["projectName"] == "Traffic Light"
    assert body["editorVersion"] == "4.2.0"
    assert body["uploadedBy"] == "admin"
    assert body["sizeBytes"] == len(b"payload")
    assert body["libraries"][0]["name"] == "Motion"


def test_info_is_open_to_non_admins(client, admin_token):
    # Deciding whether to OFFER retrieval is not privileged; retrieving is.
    _store()
    create_user(client, "operator", "operator-pass", token=admin_token, role="user")
    operator = token_for(client, "operator", "operator-pass")
    resp = client.get("/api/project-snapshot/info", headers=auth(operator))
    assert resp.status_code == 200
    assert resp.get_json()["present"] is True


# --- GET /api/project-snapshot -------------------------------------------


def test_retrieval_requires_authentication(client):
    assert client.get("/api/project-snapshot").status_code == 401


def test_retrieval_is_refused_to_non_admins(client, admin_token):
    # The stored project is not encrypted on the device, so this role check is
    # the entire access control. It is not a second layer behind one.
    _store()
    create_user(client, "operator", "operator-pass", token=admin_token, role="user")
    operator = token_for(client, "operator", "operator-pass")
    assert client.get("/api/project-snapshot", headers=auth(operator)).status_code == 403


def test_retrieval_returns_the_stored_archive_verbatim(client, admin_token):
    blob = b"PK\x03\x04 pretend archive \x00\xff"
    _store(blob)
    body = client.get("/api/project-snapshot", headers=auth(admin_token)).get_json()
    assert base64.b64decode(body["contentBase64"]) == blob
    assert body["projectName"] == "Traffic Light"
    assert body["filename"] == "project.zip"


def test_retrieval_404s_when_nothing_is_stored(client, admin_token):
    assert client.get("/api/project-snapshot", headers=auth(admin_token)).status_code == 404


def test_retrieval_404s_while_a_snapshot_is_only_staged(client, admin_token):
    # Mid-build: the program is not in place yet, so neither is the project.
    project_snapshot.stage(b"payload", _metadata())
    assert client.get("/api/project-snapshot", headers=auth(admin_token)).status_code == 404


def test_the_runtime_never_parses_the_archive(client, admin_token):
    # A blob that is definitively not a ZIP round-trips untouched. If this ever
    # fails, something started inspecting the archive and the format is no
    # longer free to change without touching the device.
    blob = b"\x00\x01\x02 definitely not a zip \xfe\xff"
    _store(blob)
    body = client.get("/api/project-snapshot", headers=auth(admin_token)).get_json()
    assert base64.b64decode(body["contentBase64"]) == blob


def test_a_new_admin_can_retrieve_a_project_stored_before_the_account_existed(client, admin_token):
    # The snapshot deliberately survives changes to the user list, including a
    # credentials reset that wipes the database: retrieval is gated on holding
    # admin credentials at request time, not on who uploaded.
    _store()
    create_user(client, "second-admin", "second-pass", token=admin_token, role="admin")
    second = token_for(client, "second-admin", "second-pass")
    resp = client.get("/api/project-snapshot", headers=auth(second))
    assert resp.status_code == 200
    assert resp.get_json()["projectName"] == "Traffic Light"


# --- capabilities ---------------------------------------------------------


def test_capabilities_advertises_snapshot_support(client):
    # Unauthenticated on purpose: a client decides whether to build and send a
    # snapshot before it has logged in to the device.
    assert client.get("/api/capabilities").get_json()["projectSnapshot"] is True
