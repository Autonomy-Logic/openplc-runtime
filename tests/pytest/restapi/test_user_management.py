"""Behavioural tests for the runtime user-management REST API.

Covers RBAC (admin vs user), the bootstrap first-user flow, the unified
update-user endpoint (rename / password / role), self-vs-admin password rules,
and the delete guards.
"""

from conftest import auth, create_user, login, token_for


# --- bootstrap / create ---------------------------------------------------


def test_first_user_bootstraps_as_admin_without_auth(client):
    resp = create_user(client, "admin", "admin-pass")
    assert resp.status_code == 201
    assert resp.get_json()["role"] == "admin"


def test_get_users_info_reports_existence_without_token(client):
    # No users yet -> 404 (drives the editor's "create first user" dialog).
    assert client.get("/api/get-users-info").status_code == 404
    create_user(client, "admin", "admin-pass")
    resp = client.get("/api/get-users-info")
    assert resp.status_code == 200
    assert resp.get_json() == {"msg": "Users found"}


def test_create_user_requires_auth_once_a_user_exists(client, admin_token):
    # No token now that a user exists.
    assert create_user(client, "bob", "bob-pass").status_code == 401


def test_non_admin_cannot_create_user(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    bob_token = token_for(client, "bob", "bob-pass")
    resp = create_user(client, "carol", "carol-pass", token=bob_token, role="user")
    assert resp.status_code == 403


def test_admin_creates_user_with_role(client, admin_token):
    resp = create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    assert resp.status_code == 201
    assert resp.get_json()["role"] == "user"


def test_admin_create_user_rejects_invalid_role(client, admin_token):
    resp = create_user(client, "bob", "bob-pass", token=admin_token, role="superuser")
    assert resp.status_code == 400


def test_create_user_defaults_to_user_role_when_omitted(client, admin_token):
    resp = create_user(client, "bob", "bob-pass", token=admin_token)
    assert resp.status_code == 201
    assert resp.get_json()["role"] == "user"


# --- login / whoami -------------------------------------------------------


def test_login_wrong_password(client, admin_token):
    assert login(client, "admin", "nope").status_code == 401


def test_whoami_returns_identity_and_role(client, admin_token):
    resp = client.get("/api/whoami", headers=auth(admin_token))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    assert "id" in body


def test_get_users_info_with_token_lists_roles(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    resp = client.get("/api/get-users-info", headers=auth(admin_token))
    assert resp.status_code == 200
    roles = {u["username"]: u["role"] for u in resp.get_json()}
    assert roles == {"admin": "admin", "bob": "user"}


# --- update-user: rename --------------------------------------------------


def _id_of(client, token, username):
    users = client.get("/api/get-users-info", headers=auth(token)).get_json()
    return next(u["id"] for u in users if u["username"] == username)


def test_admin_renames_another_user(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    bob_id = _id_of(client, admin_token, "bob")
    resp = client.put(
        f"/api/update-user/{bob_id}", json={"username": "bobby"}, headers=auth(admin_token)
    )
    assert resp.status_code == 200
    assert resp.get_json()["user"]["username"] == "bobby"


def test_rename_to_existing_username_conflicts(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    bob_id = _id_of(client, admin_token, "bob")
    resp = client.put(
        f"/api/update-user/{bob_id}", json={"username": "admin"}, headers=auth(admin_token)
    )
    assert resp.status_code == 409


def test_rename_to_empty_username_rejected(client, admin_token):
    admin_id = _id_of(client, admin_token, "admin")
    resp = client.put(
        f"/api/update-user/{admin_id}", json={"username": "   "}, headers=auth(admin_token)
    )
    assert resp.status_code == 400


def test_update_user_no_fields(client, admin_token):
    admin_id = _id_of(client, admin_token, "admin")
    resp = client.put(f"/api/update-user/{admin_id}", json={}, headers=auth(admin_token))
    assert resp.status_code == 400


def test_update_user_not_found(client, admin_token):
    resp = client.put("/api/update-user/9999", json={"username": "x"}, headers=auth(admin_token))
    assert resp.status_code == 404


# --- update-user: passwords ----------------------------------------------


def test_admin_resets_other_user_password_without_current(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    bob_id = _id_of(client, admin_token, "bob")
    resp = client.put(
        f"/api/update-user/{bob_id}", json={"password": "new-pass"}, headers=auth(admin_token)
    )
    assert resp.status_code == 200
    assert login(client, "bob", "new-pass").status_code == 200
    assert login(client, "bob", "bob-pass").status_code == 401


def test_self_password_change_requires_current_password(client, admin_token):
    admin_id = _id_of(client, admin_token, "admin")
    # Missing current password.
    resp = client.put(
        f"/api/update-user/{admin_id}", json={"password": "new-pass"}, headers=auth(admin_token)
    )
    assert resp.status_code == 400


def test_self_password_change_wrong_current_password(client, admin_token):
    admin_id = _id_of(client, admin_token, "admin")
    resp = client.put(
        f"/api/update-user/{admin_id}",
        json={"password": "new-pass", "current_password": "wrong"},
        headers=auth(admin_token),
    )
    assert resp.status_code == 403


def test_self_password_change_succeeds_with_current_password(client, admin_token):
    admin_id = _id_of(client, admin_token, "admin")
    resp = client.put(
        f"/api/update-user/{admin_id}",
        json={"password": "new-pass", "current_password": "admin-pass"},
        headers=auth(admin_token),
    )
    assert resp.status_code == 200
    assert login(client, "admin", "new-pass").status_code == 200


def test_self_password_change_revokes_current_token(client, admin_token):
    admin_id = _id_of(client, admin_token, "admin")
    # Token works before the change.
    assert client.get("/api/whoami", headers=auth(admin_token)).status_code == 200
    resp = client.put(
        f"/api/update-user/{admin_id}",
        json={"password": "new-pass", "current_password": "admin-pass"},
        headers=auth(admin_token),
    )
    assert resp.status_code == 200
    # The old token is now revoked — the client must log in again.
    assert client.get("/api/whoami", headers=auth(admin_token)).status_code == 401


def test_admin_reset_of_other_user_does_not_revoke_admin_token(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    bob_id = _id_of(client, admin_token, "bob")
    resp = client.put(f"/api/update-user/{bob_id}", json={"password": "reset"}, headers=auth(admin_token))
    assert resp.status_code == 200
    # Admin's own token stays valid after resetting someone else's password.
    assert client.get("/api/whoami", headers=auth(admin_token)).status_code == 200


# --- update-user: authorization & roles -----------------------------------


def test_non_admin_cannot_edit_other_user(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    create_user(client, "carol", "carol-pass", token=admin_token, role="user")
    bob_token = token_for(client, "bob", "bob-pass")
    carol_id = _id_of(client, admin_token, "carol")
    resp = client.put(
        f"/api/update-user/{carol_id}", json={"username": "x"}, headers=auth(bob_token)
    )
    assert resp.status_code == 403


def test_non_admin_can_edit_self(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    bob_token = token_for(client, "bob", "bob-pass")
    bob_id = _id_of(client, admin_token, "bob")
    resp = client.put(
        f"/api/update-user/{bob_id}", json={"username": "bobby"}, headers=auth(bob_token)
    )
    assert resp.status_code == 200


def test_non_admin_cannot_change_own_role(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    bob_token = token_for(client, "bob", "bob-pass")
    bob_id = _id_of(client, admin_token, "bob")
    resp = client.put(f"/api/update-user/{bob_id}", json={"role": "admin"}, headers=auth(bob_token))
    assert resp.status_code == 403


def test_admin_promotes_and_demotes(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    bob_id = _id_of(client, admin_token, "bob")
    # Promote bob to admin.
    resp = client.put(
        f"/api/update-user/{bob_id}", json={"role": "admin"}, headers=auth(admin_token)
    )
    assert resp.status_code == 200 and resp.get_json()["user"]["role"] == "admin"
    # Now two admins -> demoting the original admin is allowed.
    admin_id = _id_of(client, admin_token, "admin")
    resp = client.put(
        f"/api/update-user/{admin_id}", json={"role": "user"}, headers=auth(admin_token)
    )
    assert resp.status_code == 200


def test_cannot_demote_last_admin(client, admin_token):
    admin_id = _id_of(client, admin_token, "admin")
    resp = client.put(
        f"/api/update-user/{admin_id}", json={"role": "user"}, headers=auth(admin_token)
    )
    assert resp.status_code == 409


def test_invalid_role_on_update_rejected(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    bob_id = _id_of(client, admin_token, "bob")
    resp = client.put(
        f"/api/update-user/{bob_id}", json={"role": "root"}, headers=auth(admin_token)
    )
    assert resp.status_code == 400


# --- delete ---------------------------------------------------------------


def test_non_admin_cannot_delete(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    create_user(client, "carol", "carol-pass", token=admin_token, role="user")
    bob_token = token_for(client, "bob", "bob-pass")
    carol_id = _id_of(client, admin_token, "carol")
    resp = client.delete(f"/api/delete-user/{carol_id}", headers=auth(bob_token))
    assert resp.status_code == 403


def test_admin_cannot_delete_self(client, admin_token):
    admin_id = _id_of(client, admin_token, "admin")
    resp = client.delete(f"/api/delete-user/{admin_id}", headers=auth(admin_token))
    assert resp.status_code == 403


def test_admin_deletes_other_user(client, admin_token):
    create_user(client, "bob", "bob-pass", token=admin_token, role="user")
    bob_id = _id_of(client, admin_token, "bob")
    resp = client.delete(f"/api/delete-user/{bob_id}", headers=auth(admin_token))
    assert resp.status_code == 200
    assert login(client, "bob", "bob-pass").status_code == 401


def test_delete_missing_user(client, admin_token):
    resp = client.delete("/api/delete-user/9999", headers=auth(admin_token))
    assert resp.status_code == 404


# --- migration ------------------------------------------------------------


def test_migration_backfills_role_column(app):
    """A pre-RBAC users table (no role column) is migrated and rows default to admin."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    from webserver import restapi

    db = restapi.db
    # Simulate a legacy schema: drop the table and recreate it without `role`.
    with db.engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS users"))
        conn.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL)"
            )
        )
        conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('legacy', 'x')"))

    assert "role" not in {c["name"] for c in sa_inspect(db.engine).get_columns("users")}
    restapi.apply_user_schema_migrations()
    cols = {c["name"] for c in sa_inspect(db.engine).get_columns("users")}
    assert "role" in cols
    with db.engine.begin() as conn:
        role = conn.execute(text("SELECT role FROM users WHERE username='legacy'")).scalar()
    assert role == "admin"
