import pytest

def test_login_success(client):
    resp = client.post("/api/login", json={"username": "lucas", "password": "lucas"})
    assert resp.status_code == 200
    assert "access_token" in resp.get_json()

def test_login_failure(client):
    resp = client.post("/api/login", json={"username": "wrong", "password": "bad"})
    assert resp.status_code == 401
    assert resp.get_json()["msg"] == "Bad credentials"

def test_protected_requires_auth(client):
    resp = client.get("/api/protected")
    assert resp.status_code == 401  # No token provided

# 🚀 Parametrize multiple protected endpoints
@pytest.mark.parametrize("endpoint,method", [
    ("/api/protected", "get"),
    # Add more protected routes here
    # ("/user/profile", "get"),
    # ("/admin/data", "post"),
])
def test_protected_with_auth(auth_client, endpoint, method):
    # getattr lets us call get/post/put dynamically
    func = getattr(auth_client, method)
    resp = func(endpoint)
    assert resp.status_code == 200
    data = resp.get_json()
    assert "msg" in data
