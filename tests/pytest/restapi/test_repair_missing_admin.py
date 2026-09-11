"""A device left with accounts but no administrator.

That state is a dead end rather than an inconvenience: once any user exists,
``create-user`` refuses a non-admin caller and ``update-user`` refuses a role
change from one, so no sequence of API calls produces an admin. Everything
admin-gated stays unreachable, and nothing surfaces it until someone tries.

Devices reach it by upgrading through an early build of the roles feature,
whose ``role`` column was nullable with no default. ``apply_user_schema_
migrations`` does not cover it -- that only runs when the column is ABSENT, so
a database that already has one keeps whatever it holds.

The repair promotes a single account and refuses to choose among several. Both
halves matter: the first rescues the devices that exist, and the second keeps
the runtime from silently handing administrator rights to whichever account
happens to sort first.
"""

from conftest import auth, create_user, token_for
from sqlalchemy import text as sa_text

from webserver.restapi import ADMIN_ROLE, USER_ROLE, User, admin_count, db, repair_missing_admin


def _add_user(username, role):
    """Insert an account directly, bypassing the role rules the API enforces.

    The states under test cannot be produced through the API at all -- that is
    the whole problem -- so they have to be built underneath it.
    """
    user = User(username=username, role=role)
    user.set_password("pass-for-" + username)
    db.session.add(user)
    db.session.commit()
    return user


def _roles():
    return {user.username: user.role for user in User.query.all()}


# --- the repair ----------------------------------------------------------


def test_a_lone_non_admin_account_is_promoted(app):
    _add_user("op", USER_ROLE)
    assert repair_missing_admin() is True
    assert _roles() == {"op": ADMIN_ROLE}


def test_a_null_role_counts_as_no_admin_rather_than_crashing(app):
    # Rebuilt as the schema an affected device actually carries, copied from
    # the hardware unit this was found on:
    #
    #     role VARCHAR(20)      -- nullable, no default
    #
    # The current model declares NOT NULL DEFAULT 'admin', so a NULL role
    # cannot be created through it or even inserted into the table it builds.
    # Reproducing the old shape is the only way to check the repair copes with
    # what is out there rather than with what we would write today.
    db.session.execute(sa_text("DROP TABLE users"))
    db.session.execute(
        sa_text(
            "CREATE TABLE users ("
            " id INTEGER NOT NULL,"
            " username TEXT NOT NULL,"
            " password_hash TEXT NOT NULL,"
            " role VARCHAR(20),"
            " PRIMARY KEY (id),"
            " UNIQUE (username))"
        )
    )
    db.session.execute(
        sa_text("INSERT INTO users (username, password_hash, role) VALUES ('op', 'x', NULL)")
    )
    db.session.commit()

    assert admin_count() == 0
    assert repair_missing_admin() is True
    assert _roles() == {"op": ADMIN_ROLE}


def test_the_repair_is_a_no_op_on_every_later_boot(app):
    _add_user("op", USER_ROLE)
    assert repair_missing_admin() is True
    # Second boot: an admin now exists, so there is nothing to do. This has to
    # be a real no-op, not merely idempotent by luck.
    assert repair_missing_admin() is False
    assert _roles() == {"op": ADMIN_ROLE}


def test_several_accounts_with_no_admin_are_left_alone(app):
    # The runtime has no basis for choosing which account becomes the
    # administrator, so it refuses rather than picking one silently.
    _add_user("alice", USER_ROLE)
    _add_user("bob", USER_ROLE)
    assert repair_missing_admin() is False
    assert _roles() == {"alice": USER_ROLE, "bob": USER_ROLE}


def test_several_accounts_with_no_admin_are_reported(app, caplog):
    # Nothing else will ever surface this state -- an operator only finds it by
    # hitting a 403 on something that should have worked.
    _add_user("alice", USER_ROLE)
    _add_user("bob", USER_ROLE)
    with caplog.at_level("ERROR"):
        repair_missing_admin()
    logged = caplog.text
    assert "alice" in logged and "bob" in logged


def test_an_existing_admin_is_untouched(app):
    _add_user("admin", ADMIN_ROLE)
    _add_user("op", USER_ROLE)
    assert repair_missing_admin() is False
    assert _roles() == {"admin": ADMIN_ROLE, "op": USER_ROLE}


def test_a_device_with_no_accounts_is_left_to_bootstrap(app):
    # A fresh device is not broken, it is empty. The bootstrap path already
    # makes the first account an admin.
    assert repair_missing_admin() is False
    assert User.query.count() == 0


def test_bootstrap_still_produces_an_admin_after_the_repair_runs(client):
    assert repair_missing_admin() is False
    resp = create_user(client, "first", "first-pass")
    assert resp.status_code == 201
    assert resp.get_json()["role"] == ADMIN_ROLE


# --- what the repair actually restores -----------------------------------


def test_the_promoted_account_can_use_admin_only_endpoints(client, app):
    # The point of the repair, end to end: before it, this account cannot reach
    # anything admin-gated and cannot be granted access by any API call.
    _add_user("op", USER_ROLE)
    token = token_for(client, "op", "pass-for-op")
    assert client.get("/api/project-snapshot", headers=auth(token)).status_code == 403

    repair_missing_admin()

    # A fresh token, because the role is read from the database per request but
    # the identity was established before the promotion.
    token = token_for(client, "op", "pass-for-op")
    assert client.get("/api/whoami", headers=auth(token)).get_json()["role"] == ADMIN_ROLE
    # 404 rather than 403: reachable now, with nothing stored to hand back.
    assert client.get("/api/project-snapshot", headers=auth(token)).status_code == 404


def test_without_the_repair_there_is_no_way_back(client, app):
    # Documents why this cannot be left to the operator: with no admin, neither
    # of the two endpoints that could create one will act.
    _add_user("op", USER_ROLE)
    token = token_for(client, "op", "pass-for-op")

    created = create_user(client, "new-admin", "pw", token=token, role=ADMIN_ROLE)
    assert created.status_code == 403

    promoted = client.put(
        f"/api/update-user/{User.query.filter_by(username='op').one().id}",
        json={"role": ADMIN_ROLE},
        headers=auth(token),
    )
    assert promoted.status_code == 403
