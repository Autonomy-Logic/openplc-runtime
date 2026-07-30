"""
WebSocket debug endpoint for OpenPLC Runtime v4

This module provides a secure WebSocket interface for debugger communication.
It receives debug commands in hex format, forwards them to the Unix socket,
and returns responses through the WebSocket connection.
"""

from flask import request
from flask_jwt_extended import current_user, verify_jwt_in_request
from flask_socketio import SocketIO, emit

from webserver.logger import get_logger
from webserver.vpp_license_debug import handle_license_command, is_license_command

logger, _ = get_logger("debug_ws", use_buffer=True)

_socketio = None  # pylint: disable=invalid-name
_unix_client = None  # pylint: disable=invalid-name

# Token captured per connection, so every COMMAND can be re-authenticated rather
# than trusting the one check done at connect time. Keyed by socket id; dropped
# on disconnect.
_session_tokens: dict = {}


def _reverify_session_token() -> bool:
    """Re-run the FULL authentication pipeline for the current command.

    The connect handler authenticates once. Without this, an open socket keeps
    answering commands after its token expires (15 minutes by default -- the
    config sets no JWT_ACCESS_TOKEN_EXPIRES) and after /logout revokes it (the
    blacklist is only consulted by verify_jwt_in_request). That matters here
    because the license FCs read the hardware anchor and write the license blob:
    this channel is a trust boundary, so "authenticated once, ever" is not
    enough. Re-verifying per command is cheap -- an HMAC and a set lookup.
    """
    token = _session_tokens.get(request.sid)
    if not token:
        logger.warning("Debug command on a session with no captured token")
        return False
    try:
        # Same pipeline as @jwt_required(): expiry, signature, blacklist and the
        # user lookup that current_user depends on.
        request.environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        verify_jwt_in_request()
        return True
    except Exception as e:
        logger.warning("Debug command rejected, token no longer valid: %s", e)
        return False


def _current_user_is_admin() -> bool:
    """True when the re-verified token belongs to an admin account.

    Uses the role mechanism that already exists in the REST API (the User model's
    ``is_admin()``, resolved through the JWT user_lookup_loader) -- @jwt_required
    alone does not look at the role, so a plain ``user`` account could read the
    anchor of any board and overwrite its license. Must be called AFTER
    _reverify_session_token(), which is what populates ``current_user``.
    """
    user = current_user
    if not user:
        return False
    checker = getattr(user, "is_admin", None)
    return bool(checker and checker())


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

            # Kept so every command can re-run the same check (expiry, revocation
            # and role), instead of the session inheriting this one verdict.
            _session_tokens[request.sid] = token

            logger.info("Debug WebSocket connected")
            emit("connected", {"status": "ok"})
            return True

        except Exception as e:
            logger.warning("Debug WebSocket auth failed: %s", e)
            return False

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

            # Re-authenticate EVERY command, not just the connect. See
            # _reverify_session_token: an expired or revoked token must stop
            # working on a socket that is already open.
            if not _reverify_session_token():
                emit("debug_response", {"success": False, "error": "Unauthorized"})
                return

            # The license FCs are a trust boundary of their own: 0x48 hands out
            # the raw anchor (from which the licensing identity and the
            # possession key are derived, offline and forever) and 0x49 writes
            # the license blob. Require the admin role for them -- @jwt_required
            # alone never looks at the role.
            if is_license_command(command_hex) and not _current_user_is_admin():
                logger.warning("License FC refused for a non-admin account: %s", command_hex)
                emit(
                    "debug_response",
                    {"success": False, "error": "Admin privileges required"},
                )
                return

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
