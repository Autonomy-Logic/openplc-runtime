"""Pytest fixtures for the REST API user-management endpoints.

``webserver.config`` has import-time side effects (it resolves a persistent
data dir, writes a ``.env`` and reads secrets). We point those at a throwaway
temp dir and inject deterministic secrets *before* importing the module, so the
tests are hermetic and never touch a real runtime install or a read-only path.
"""

import os
import secrets
import tempfile

# Must be set before importing webserver.config / webserver.restapi.
_TMP = tempfile.mkdtemp(prefix="openplc-restapi-tests-")
os.environ.setdefault("OPENPLC_RUNTIME_DIR", os.path.join(_TMP, "run"))
os.environ.setdefault("OPENPLC_PERSISTENT_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("SQLALCHEMY_DATABASE_URI", f"sqlite:///{os.path.join(_TMP, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("PEPPER", secrets.token_hex(32))
os.environ.setdefault("FLASK_ENV", "development")

import pytest  # noqa: E402

from webserver import restapi  # noqa: E402

# The Flask app is a module-level singleton, so register the blueprint exactly
# once (registering twice raises). Subsequent fixtures only reset the DB.
if "restapi_blueprint" not in restapi.app_restapi.blueprints:
    restapi.app_restapi.register_blueprint(restapi.restapi_bp, url_prefix="/api")
restapi.app_restapi.config.update(TESTING=True)


@pytest.fixture()
def app():
    """The REST API Flask app with the user routes registered and a fresh DB."""
    app = restapi.app_restapi
    with app.app_context():
        restapi.db.drop_all()
        restapi.db.create_all()
        restapi.apply_user_schema_migrations()
        restapi.jwt_blacklist.clear()
        yield app
        restapi.db.session.remove()
        restapi.db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


# --- helpers -------------------------------------------------------------


def create_user(client, username, password, token=None, role=None):
    body = {"username": username, "password": password}
    if role is not None:
        body["role"] = role
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/create-user", json=body, headers=headers)


def login(client, username, password):
    resp = client.post("/api/login", json={"username": username, "password": password})
    return resp


def token_for(client, username, password):
    resp = login(client, username, password)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_token(client):
    """Bootstrap the first (admin) account and return a valid token for it."""
    resp = create_user(client, "admin", "admin-pass")
    assert resp.status_code == 201, resp.get_json()
    return token_for(client, "admin", "admin-pass")
