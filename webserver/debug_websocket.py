"""
WebSocket debug endpoint for OpenPLC Runtime v4

This module provides a secure WebSocket interface for debugger communication.
It receives debug commands in hex format, forwards them to the Unix socket,
and returns responses through the WebSocket connection.
"""

from typing import Any

from flask import request
from flask_jwt_extended import decode_token, verify_jwt_in_request
from flask_socketio import SocketIO, emit

from webserver.logger import get_logger

# The debug socket is mounted on app_restapi and shares its JWT manager, so it
# also shares the two decisions that outlive a token: the logout blacklist and
# who the token's subject actually is. Importing the loaders rather than reaching
# into `jwt_blacklist` keeps one definition of each. Safe direction: restapi does
# not import this module.
from webserver.restapi import check_if_token_revoked, user_lookup_callback
from webserver.vpp_license_debug import handle_license_command

logger, _ = get_logger("debug_ws", use_buffer=True)

_socketio = None  # pylint: disable=invalid-name
_unix_client = None  # pylint: disable=invalid-name

# Token captured per connection, so every COMMAND can be re-checked against the
# decisions that outlive it (see _reverify_session_token) rather than inheriting
# the connect-time verdict forever. Keyed by socket id; dropped on disconnect.
_session_tokens: dict = {}


def _reverify_session_token() -> bool:
    """Re-check this connection's token before serving a command.

    WHAT THIS ENFORCES
    ------------------
    The connect handler authenticates once, in full. Two things can still change
    afterwards, and both must land on an already-open socket:

    * **Revocation.** ``/logout`` adds the token's ``jti`` to the blacklist, and
      that blacklist is only ever consulted during verification. Without a
      re-check, logging out left the session still answering the license FCs --
      which read the hardware anchor and write the license blob, so this channel
      is a trust boundary and "authenticated once, ever" is not enough.
    * **Identity.** The account behind the token can be deleted while the socket
      is open. A token whose subject no longer exists authorizes nobody.

    WHAT THIS DELIBERATELY DOES NOT ENFORCE
    ---------------------------------------
    Token EXPIRY, which is not a revocation. A debug session is a long-lived
    connection authenticated at the handshake -- the model every WebSocket
    client assumes, and the one this endpoint had before v4.2.0. The
    access-token lifetime (15 minutes, the flask-jwt-extended default, since no
    config sets JWT_ACCESS_TOKEN_EXPIRES) bounds how long a bearer token may be
    presented to obtain NEW access, and the connect handler still enforces it in
    full: an expired token cannot open a socket, and cannot be installed by
    ``reauth`` either.

    Refusing COMMANDS on expiry bought nothing beyond that, because the holder
    of an expired token on an open socket is the same party that was
    authenticated on it minutes earlier, and it broke every client with no
    renewal path. Editor v4.2.11 and older have none -- a debug session that
    used to last as long as it stayed open began dying about 15 minutes in, with
    the raw ``token_expired`` string surfaced to the user and no way back short
    of reconnecting. The ``reauth`` handler below is still the supported renewal
    path for clients that do implement it; it is now an optimisation rather than
    the only thing keeping a session alive.
    """
    token = _session_tokens.get(request.sid)
    if not token:
        logger.warning("Debug command on a session with no captured token")
        return False

    try:
        # allow_expired relaxes the `exp` claim and NOTHING else: the signature,
        # the algorithm and the claim structure are all still verified, so a
        # forged or tampered token raises here exactly as before.
        payload: dict[str, Any] = decode_token(token, allow_expired=True)
    except Exception as e:
        logger.warning("Debug command rejected, token did not verify: %s", e)
        return False

    # Parity with verify_jwt_in_request, which refuses a refresh token where an
    # access token is required. Only access tokens are ever issued here, so
    # anything else is a client bug at best.
    if payload.get("type") != "access":
        logger.warning("Debug command rejected, token is not an access token")
        return False

    if check_if_token_revoked({}, payload):
        logger.info("Debug command rejected: this session's token was revoked by logout")
        return False

    if user_lookup_callback({}, payload) is None:
        logger.info("Debug command rejected: the account behind this session no longer exists")
        return False

    return True


def init_debug_websocket(app, unix_client_instance):
    """
    Initialize the WebSocket server for debug communication.

    Args:
        app: Flask application instance
        unix_client_instance: SyncUnixClient instance for communicating with C core
    """
    global _socketio, _unix_client

    _unix_client = unix_client_instance

    try:
        from werkzeug import serving  # pylint: disable=import-outside-toplevel

        _original_server_log = serving.BaseWSGIServer.log

        def _filtered_server_log(self, log_type, message, *args):
            """Filter out specific error messages from server logs"""
            if (
                log_type == "error"
                and "Error on request" in message
                and "write() before start_response" in message
            ):
                logger.debug("Suppressed WSGI disconnect error from server log")
                return None
            return _original_server_log(self, log_type, message, *args)

        serving.BaseWSGIServer.log = _filtered_server_log
        logger.debug("Patched werkzeug server logging to suppress disconnect errors")
    except Exception as e:
        logger.warning("Failed to patch error suppression: %s", e)

    _socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode="threading",
        logger=False,
        engineio_logger=False,
        ping_timeout=60,
        ping_interval=25,
        allow_upgrades=False,
    )

    @_socketio.on("connect", namespace="/api/debug")
    def handle_connect(auth):
        """Handle WebSocket connection with JWT authentication"""
        try:
            token = None
            if auth and isinstance(auth, dict):
                token = auth.get("token")

            if not token:
                token = request.args.get("token")

            if not token:
                logger.warning("Debug WebSocket connection attempt without token")
                return False

            # Inject token into the request so verify_jwt_in_request() uses
            # the same authentication pipeline as @jwt_required() -- including
            # blacklist checks and user identity validation.
            request.environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
            verify_jwt_in_request()

            # Kept so every command can re-check revocation and identity against
            # it, instead of the session inheriting this one verdict for good.
            _session_tokens[request.sid] = token

            logger.info("Debug WebSocket connected")
            emit("connected", {"status": "ok"})
            return True

        except Exception as e:
            logger.warning("Debug WebSocket auth failed: %s", e)
            return False

    @_socketio.on("reauth", namespace="/api/debug")
    def handle_reauth(data):
        """Swap this session's token for a fresh one, after FULL verification.

        Optional for the client. Since expiry alone no longer refuses a command
        (see _reverify_session_token), a session survives without ever calling
        this -- which is what keeps Editor v4.2.11 and older working. What it
        still buys a client that does call it: the session stops depending on a
        token that can no longer be renewed anywhere else, so a later logout
        revokes the token the user is actually holding.

        The new token goes through the FULL pipeline -- signature, expiry,
        revocation, user lookup -- so reauth can never LOWER the bar, only
        replace a live session's credential with an equally valid one.
        """
        token = data.get("token", "") if isinstance(data, dict) else ""
        if not token:
            emit("reauth_result", {"success": False, "error": "no token"})
            return
        try:
            request.environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
            verify_jwt_in_request()
        except Exception as e:
            logger.warning("reauth rejected: %s", e)
            emit("reauth_result", {"success": False, "error": "Unauthorized"})
            return
        _session_tokens[request.sid] = token
        logger.info("Debug session token renewed via reauth")
        emit("reauth_result", {"success": True})

    @_socketio.on("disconnect", namespace="/api/debug")
    def handle_disconnect():
        """Handle WebSocket disconnection"""
        _session_tokens.pop(request.sid, None)
        logger.info("Debug WebSocket disconnected")

    @_socketio.on("debug_command", namespace="/api/debug")
    def handle_debug_command(data):
        """
        Handle debug command from the client.

        Expected data format:
        {
            'command': 'hex string of debug data (e.g., "41 00 00")'
        }

        Returns debug response in same hex format
        """
        try:
            command_hex = data.get("command", "")
            if not command_hex:
                logger.warning("Empty debug command received")
                emit("debug_response", {"success": False, "error": "Empty command"})
                return

            # Re-check EVERY command, not just the connect. See
            # _reverify_session_token: a revoked token, or one whose account is
            # gone, must stop working on a socket that is already open. Plain
            # expiry does not, which is why this is a re-CHECK and not a full
            # re-authentication.
            if not _reverify_session_token():
                emit("debug_response", {"success": False, "error": "Unauthorized"})
                return

            # The license FCs are open to any AUTHENTICATED role, not just admin
            # (decision 2026-08-25). They were admin-gated on the theory that the
            # anchor read (0x48) and the blob write (0x49) were a trust boundary,
            # but that gate protected the wrong thing: the PURCHASE is authorized
            # by the Edge account on the /buy page, never by the runtime role, so
            # requiring admin here only stopped an operator from activating a
            # licence they had already paid for. What stays open is low-risk: the
            # anchor is the board's serial (baremetal exposes it with no auth at
            # all), the blob is node-locked and useless on another device, and a
            # bad write is recoverable (the entitlement lives in the backend; a
            # refresh rewrites the correct blob). JWT re-verification above still
            # applies, so "any role" means any logged-in user, never anonymous.

            # License function codes (0x48/0x49/0x4A) operate on host files
            # (/proc anchor + conf/<plugin>.license) and are resolved here in
            # Python (D70a) BEFORE the unix-socket gate below, so device
            # activation works even while the PLC/core is stopped.
            license_response = handle_license_command(command_hex)
            if license_response is not None:
                logger.debug("License FC handled locally: %s -> %s", command_hex, license_response)
                emit("debug_response", {"success": True, "data": license_response})
                return

            if not _unix_client or not _unix_client.is_connected():
                logger.error("Unix socket not connected")
                emit(
                    "debug_response",
                    {"success": False, "error": "Runtime not connected"},
                )
                return

            logger.debug("Debug command received: %s", command_hex)

            unix_command = f"DEBUG:{command_hex}\n"
            response = _unix_client.send_and_receive(unix_command, timeout=2.0)

            if response is None:
                logger.warning("No response from runtime")
                emit(
                    "debug_response",
                    {"success": False, "error": "No response from runtime"},
                )
                return

            if response.startswith("DEBUG:"):
                response_hex = response[6:].strip()
                logger.debug("Debug response: %s", response_hex)
                emit("debug_response", {"success": True, "data": response_hex})
            elif response.startswith("DEBUG:ERROR"):
                error_msg = (
                    response.split(":", 2)[2] if len(response.split(":")) > 2 else "Unknown error"
                )
                logger.warning("Debug error from runtime: %s", error_msg)
                emit("debug_response", {"success": False, "error": error_msg})
            else:
                logger.warning("Unexpected response format: %s", response)
                emit(
                    "debug_response",
                    {"success": False, "error": "Unexpected response format"},
                )

        except Exception as e:
            logger.error("Error processing debug command: %s", e)
            emit("debug_response", {"success": False, "error": str(e)})

    logger.info("Debug WebSocket endpoint initialized at /api/debug")
    return _socketio


def get_socketio():
    """Get the SocketIO instance"""
    return _socketio
