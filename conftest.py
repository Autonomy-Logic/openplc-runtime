import pytest
from webserver.app import app
from webserver.restapi import restapi_bp, db, JWTManager


@pytest.fixture(scope="session", autouse=True)
def configure_app():
    """
    Configure app once for all tests.
    Registers blueprints and initializes DB.
    """
    # Configure test database
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Bind db to app
    db.init_app(app)
    jwt = JWTManager(app)

    # Register blueprints (only once)
    if "restapi" not in app.blueprints:
        app.register_blueprint(restapi_bp, url_prefix="/api")

    # Create tables
    with app.app_context():
        db.create_all()

    yield app

    # Teardown
    with app.app_context():
        db.drop_all()


@pytest.fixture
def client(configure_app):
    """Basic unauthenticated Flask test client."""
    with configure_app.test_client() as client:
        yield client


@pytest.fixture
def auth_token(client):
    """Get a valid JWT token by logging in with test credentials."""
    response = client.post("/api/create-user", json={
        "username": "lucas",
        "password": "lucas"
    })
    data = response.get_json()
    print(data)
    response = client.post("/api/login", json={
        "username": "lucas",
        "password": "lucas"
    })
    data = response.get_json()
    print(data)
    assert response.status_code == 200
    return data["access_token"]


@pytest.fixture
def auth_client(client, auth_token):
    """Client that automatically attaches JWT auth headers."""

    class AuthClient:
        def __init__(self, client, token):
            self._client = client
            self._headers = {"Authorization": f"Bearer {token}"}

        def _inject_headers(self, kwargs):
            headers = kwargs.pop("headers", {})
            headers.update(self._headers)
            kwargs["headers"] = headers
            return kwargs

        def get(self, *args, **kwargs):
            return self._client.get(*args, **self._inject_headers(kwargs))

        def post(self, *args, **kwargs):
            return self._client.post(*args, **self._inject_headers(kwargs))

        def put(self, *args, **kwargs):
            return self._client.put(*args, **self._inject_headers(kwargs))

        def delete(self, *args, **kwargs):
            return self._client.delete(*args, **self._inject_headers(kwargs))

    return AuthClient(client, auth_token)
