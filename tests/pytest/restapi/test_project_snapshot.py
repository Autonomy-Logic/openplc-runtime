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


def test_a_stranded_staged_snapshot_is_never_promoted_by_a_later_build():
    # If an upload dies after staging but before the compile thread starts
    # (a failed extract, say), nothing discards what it staged. The next
    # upload's clear() has to be what removes it -- otherwise that build would
    # promote a snapshot belonging to an upload that never landed, and the
    # device would advertise a project it is definitely not running.
    project_snapshot.stage(b"stranded", _metadata(projectName="Never Landed"))
    project_snapshot.clear()  # the next upload
    assert project_snapshot.promote() is False
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


# --- the size guard at the boundary it defends ---------------------------
#
# `test_an_oversized_snapshot_is_refused` exercises `stage()` directly, which
# proves the cap fires once the bytes already exist. The point of the guard is
# that they never do: the route is authenticated but not admin-gated, so any
# account could otherwise have an arbitrarily large part spooled to disk and
# pulled into memory before anything refused it.


def _post_snapshot(client, admin_token, blob, *, metadata=None):
    """Post a snapshot part through the upload endpoint's staging helper."""
    import io

    from webserver import app as app_module

    payload = {
        "snapshot": (io.BytesIO(blob), "project.zip"),
        "snapshot_metadata": json.dumps(
            metadata
            if metadata is not None
            else {"formatVersion": 1, "projectName": "Traffic Light"}
        ),
    }
    with app_module.app.test_request_context(
        "/api/upload-file", method="POST", data=payload, content_type="multipart/form-data"
    ):
        return app_module.stage_project_snapshot()


def test_an_oversized_part_is_refused_without_being_read_into_memory(client):
    """The refusal must happen during the read, not after it.

    Asserting only on the message would pass against the old code too, because
    `stage()` rejected an oversized archive with the same words -- after the
    whole thing was already in memory. So the stream itself is the assertion: it
    refuses to hand over more than the guard is allowed to ask for.
    """
    from werkzeug.datastructures import FileStorage
    from werkzeug.datastructures import MultiDict

    from webserver import app as app_module

    cap = project_snapshot.MAX_SNAPSHOT_BYTES

    class Tripwire:
        """Behaves like a huge upload, and objects to being swallowed whole."""

        def __init__(self):
            self.served = 0

        def read(self, size=-1):
            if size is None or size < 0:
                raise AssertionError("unbounded read of the snapshot part")
            self.served += size
            if self.served > cap + 1:
                raise AssertionError(f"read {self.served} bytes past the {cap} cap")
            return b"x" * size

    stream = Tripwire()
    with app_module.app.test_request_context("/api/upload-file", method="POST"):
        import flask

        flask.request.files = MultiDict(
            {"snapshot": FileStorage(stream=stream, filename="project.zip")}
        )
        flask.request.form = MultiDict(
            {"snapshot_metadata": json.dumps({"formatVersion": 1, "projectName": "Big"})}
        )
        reason = app_module.stage_project_snapshot()

    assert "too large" in reason
    assert project_snapshot.has_snapshot() is False
    # Nothing staged either: a refused archive must not sit in the store waiting
    # for the next successful build to promote it.
    assert project_snapshot._STAGED_BLOB.exists() is False


def test_an_archive_at_the_limit_is_still_accepted(client, admin_token):
    # The guard reads one byte past the cap to decide, so the boundary itself
    # has to keep working.
    at_limit = b"y" * project_snapshot.MAX_SNAPSHOT_BYTES

    reason = _post_snapshot(client, admin_token, at_limit)

    assert reason == ""
    assert project_snapshot.promote() is True
    assert len(project_snapshot.read_blob()) == project_snapshot.MAX_SNAPSHOT_BYTES


def test_the_whole_request_is_bounded_at_the_http_layer(client):
    # A backstop under the per-part checks: those run only after Werkzeug has
    # parsed and spooled the body, so the body itself is capped first.
    from webserver import app as app_module

    limit = app_module.app.config["MAX_CONTENT_LENGTH"]
    assert limit is not None
    assert limit > project_snapshot.MAX_SNAPSHOT_BYTES


# --- an interrupted promote is diagnosable -------------------------------


def test_a_blob_left_without_metadata_says_so_in_the_log(caplog):
    # `promote()` moves two files, so a power cut between them can leave a blob
    # with no metadata. Reporting "nothing stored" is correct; doing it in
    # silence leaves no way to tell that from a device that never had one.
    _store()
    project_snapshot._PROMOTED_META.unlink()

    with caplog.at_level("WARNING"):
        assert project_snapshot.read_metadata() is None

    assert any("no metadata beside it" in record.message for record in caplog.records)


# --- the advertised fields cache -----------------------------------------
#
# The discovery responder reads these on every probe, so they are held in
# memory. A cache that can go stale would make the device advertise a project it
# is no longer running, which is the one thing this whole design refuses to do.


def test_what_the_device_advertises_follows_every_change_to_the_store():
    assert project_snapshot.advertised_fields() == {}

    _store(projectName="First")
    assert project_snapshot.advertised_fields()["project_name"] == "First"

    # A second upload's build succeeding replaces it.
    project_snapshot.clear()
    _store(projectName="Second")
    assert project_snapshot.advertised_fields()["project_name"] == "Second"

    # And an upload that carries nothing erases it.
    project_snapshot.clear()
    assert project_snapshot.advertised_fields() == {}


def test_a_staged_project_is_never_advertised():
    # By the time anything is staged, the upload has already passed its point of
    # no return and cleared the old project -- the program it described is being
    # replaced. So the device advertises nothing at all until a build succeeds,
    # which is the honest answer: naming the staged project would name one the
    # device is not running and may never run.
    _store(projectName="Running")
    project_snapshot.stage(b"new-archive", _metadata(projectName="Building"))

    assert project_snapshot.advertised_fields() == {}

    # A failed build leaves it that way rather than resurrecting the old name.
    project_snapshot.discard_staged()
    assert project_snapshot.advertised_fields() == {}

    # A successful one names the new project.
    project_snapshot.stage(b"new-archive", _metadata(projectName="Built"))
    assert project_snapshot.promote() is True
    assert project_snapshot.advertised_fields()["project_name"] == "Built"


def test_the_caller_cannot_mutate_the_cache_through_the_value_it_gets():
    _store(projectName="Traffic Light")
    first = project_snapshot.advertised_fields()
    first["project_name"] = "tampered"
    assert project_snapshot.advertised_fields()["project_name"] == "Traffic Light"
