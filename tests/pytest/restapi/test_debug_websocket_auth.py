"""Authentication and authorization on the debug WebSocket.

Why this file exists
--------------------
Nothing used to touch ``webserver/debug_websocket.py`` -- deleting the
``if not token: return False`` guard left the ENTIRE suite green while handing
the hardware anchor to anonymous callers. That guard is load-bearing: the anchor
is the pre-image the licensing identity and the possession key are derived from,
so whoever reads it can prove possession of that board forever, offline, and the
anchor never rotates. This channel is a trust boundary, and these tests are what
say so in executable form.

Three properties are pinned here:

1. **Connect requires a valid token** -- the deleted-guard case.
2. **Every COMMAND is re-authenticated**, not just the connect. An already-open
   socket must stop working once its token is revoked or expires; the connect-time
   verdict is not inherited for the life of the connection.
3. **The license FCs require the admin role.** ``@jwt_required()`` never looks at
   the role, so a plain ``user`` account could read the anchor of any board and
   overwrite its license.

Plus the D70a ordering invariant, which nothing asserted: the license FCs are
resolved BEFORE the ``is_connected`` gate, so activation works while the PLC is
stopped -- which is the whole reason they live in the webserver.

It lives next to the REST API fixtures because the debug socket is mounted on the
same Flask app (``app_restapi``) and depends on its JWT manager, ``User`` model
and roles. Those are exactly what ``conftest.py`` here builds.
"""

import pytest

flask_socketio = pytest.importorskip(
    "flask_socketio", reason="flask_socketio not installed (no venv)"
)

from conftest import auth, create_user, token_for  # noqa: E402

from webserver import debug_websocket as dws  # noqa: E402
from webserver import vpp_license_debug as lic  # noqa: E402

_NAMESPACE = "/api/debug"
_ANCHOR = b"8625807b0a83ae7d"


class _FakeUnixClient:
    """Stands in for the C core's unix socket."""

    def __init__(self, connected=True, response="DEBUG:41 00 01"):
        self.connected = connected
        self.response = response
        self.sent = []

    def is_connected(self):
        return self.connected

    def send_and_receive(self, command, timeout=None):
        self.sent.append(command)
        return self.response


@pytest.fixture()
def socketio(app):
    """The debug WebSocket wired onto the REST API app.

    ``init_debug_websocket`` is called per test because it is what installs the
    handlers on a fresh SocketIO instance; the module-level ``_unix_client`` and
    ``_session_tokens`` it owns are reset here so tests cannot leak into each
    other.
    """
    dws._session_tokens.clear()
    sio = dws.init_debug_websocket(app, _FakeUnixClient())
    yield sio
    dws._session_tokens.clear()


@pytest.fixture()
def anchor(monkeypatch, tmp_path):
    """A fake /proc/device-tree/serial-number, so 0x48 has something to leak."""
    anchor_file = tmp_path / "serial-number"
    anchor_file.write_bytes(_ANCHOR + b"\x00")
    monkeypatch.setattr(lic, "ANCHOR_PATH", str(anchor_file))
    return _ANCHOR


def _connect(socketio, app, token=None):
    kwargs = {"namespace": _NAMESPACE}
    if token is not None:
        kwargs["auth"] = {"token": token}
    return socketio.test_client(app, **kwargs)


def _command(client, command_hex):
    client.get_received(_NAMESPACE)  # drain the "connected" event
    client.emit("debug_command", {"command": command_hex}, namespace=_NAMESPACE)
    received = client.get_received(_NAMESPACE)
    assert received, "no debug_response emitted"
    return received[-1]["args"][0]


def _user_token(client_http, admin_token, username="tech", password="tech-pass"):
    resp = create_user(client_http, username, password, token=admin_token, role="user")
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["role"] == "user"
    return token_for(client_http, username, password)


# ---------------------------------------------------------------------------
# 1. Connect
# ---------------------------------------------------------------------------


def test_connect_without_a_token_is_refused(socketio, app, admin_token):
    ws = _connect(socketio, app, token=None)
    assert not ws.is_connected(_NAMESPACE)


def test_connect_with_a_garbage_token_is_refused(socketio, app, admin_token):
    ws = _connect(socketio, app, token="not-a-jwt")
    assert not ws.is_connected(_NAMESPACE)


def test_connect_with_a_valid_admin_token_is_accepted(socketio, app, admin_token):
    ws = _connect(socketio, app, token=admin_token)
    assert ws.is_connected(_NAMESPACE)


# ---------------------------------------------------------------------------
# 2. Every command is re-authenticated
# ---------------------------------------------------------------------------


def test_a_revoked_token_stops_working_on_an_already_open_socket(
    socketio, app, client, admin_token, anchor
):
    """/logout must actually end the session's access to the anchor.

    The blacklist is only consulted by verify_jwt_in_request, so a socket that
    authenticated once and never again kept serving 0x48 after logout. Expiry
    goes through the very same call.
    """
    ws = _connect(socketio, app, token=admin_token)
    assert _command(ws, "48")["success"] is True

    assert client.post("/api/logout", headers=auth(admin_token)).status_code == 200

    refused = _command(ws, "48")
    assert refused["success"] is False
    assert refused["error"] == "Unauthorized"
    assert "data" not in refused


def test_a_command_on_a_session_with_no_captured_token_is_refused(
    socketio, app, admin_token, anchor
):
    """Belt and braces: if the per-connection token is gone, so is access.

    Nothing may fall back to "the socket is open, therefore it is authorized".
    """
    ws = _connect(socketio, app, token=admin_token)
    dws._session_tokens.clear()

    refused = _command(ws, "48")
    assert refused["success"] is False
    assert refused["error"] == "Unauthorized"


def test_disconnect_drops_the_captured_token(socketio, app, admin_token):
    ws = _connect(socketio, app, token=admin_token)
    assert len(dws._session_tokens) == 1
    ws.disconnect(namespace=_NAMESPACE)
    assert dws._session_tokens == {}


def test_a_non_license_command_also_requires_a_live_token(socketio, app, client, admin_token):
    ws = _connect(socketio, app, token=admin_token)
    assert _command(ws, "41 00 00")["success"] is True

    assert client.post("/api/logout", headers=auth(admin_token)).status_code == 200

    assert _command(ws, "41 00 00")["error"] == "Unauthorized"


# ---------------------------------------------------------------------------
# 3. The license FCs are open to any authenticated role
# ---------------------------------------------------------------------------


def test_an_admin_can_read_the_anchor(socketio, app, admin_token, anchor):
    ws = _connect(socketio, app, token=admin_token)
    response = _command(ws, "48")

    assert response["success"] is True
    parts = response["data"].split()
    assert parts[:3] == ["48", "7E", "10"]
    assert bytes(int(p, 16) for p in parts[3:]) == anchor


@pytest.mark.parametrize("command_hex", ["48", "49 00 62" + " 00" * 98, "4A"])
def test_a_non_admin_can_use_the_license_fcs(
    socketio, app, client, admin_token, anchor, command_hex
):
    """Role `user` may run every license FC (decision 2026-08-25).

    The purchase is authorized by the Edge account on the /buy page, never by the
    runtime role, so admin-gating these only stopped an operator from activating a
    licence they had already paid for. Any authenticated role now reads the anchor
    (0x48), writes (0x49) and reads back (0x4A) the blob. The old refusal
    ("Admin privileges required") must never come back.
    """
    user_token = _user_token(client, admin_token)
    ws = _connect(socketio, app, token=user_token)
    assert ws.is_connected(_NAMESPACE)

    response = _command(ws, command_hex)

    assert response.get("error") != "Admin privileges required"
    # 0x48 must actually hand the `user` the real anchor, same as the admin path.
    if command_hex == "48":
        assert response["success"] is True
        parts = response["data"].split()
        assert parts[:3] == ["48", "7E", "10"]
        assert bytes(int(p, 16) for p in parts[3:]) == anchor


def test_a_non_admin_can_still_run_ordinary_debug_commands(socketio, app, client, admin_token):
    """A `user` role debugs variables just like an admin -- there is no role
    lockout anywhere on this channel."""
    user_token = _user_token(client, admin_token)
    ws = _connect(socketio, app, token=user_token)

    response = _command(ws, "41 00 00")
    assert response["success"] is True
    assert response["data"] == "41 00 01"


# ---------------------------------------------------------------------------
# 4. D70a: the license FCs resolve BEFORE the is_connected gate
# ---------------------------------------------------------------------------


def test_license_fcs_are_answered_while_the_core_is_stopped(socketio, app, admin_token, anchor):
    """The whole point of resolving 0x48/0x49/0x4A in the webserver.

    Activation has to work before any program runs (the chicken-and-egg the
    docstring in vpp_license_debug describes), so a license FC must not go
    through the unix-socket gate. Nothing asserted this ordering before, and it
    is one `return` away from silently regressing to "connect the PLC first".
    """
    dws._unix_client = _FakeUnixClient(connected=False)
    ws = _connect(socketio, app, token=admin_token)

    response = _command(ws, "48")
    assert response["success"] is True
    assert response["data"].startswith("48 7E")
    # ...and it never reached the core.
    assert dws._unix_client.sent == []


def test_a_non_license_command_still_needs_the_core(socketio, app, admin_token):
    """The other half of the ordering: only the license FCs skip the gate."""
    dws._unix_client = _FakeUnixClient(connected=False)
    ws = _connect(socketio, app, token=admin_token)

    response = _command(ws, "41 00 00")
    assert response["success"] is False
    assert response["error"] == "Runtime not connected"
