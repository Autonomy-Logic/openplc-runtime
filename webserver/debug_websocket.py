"""
WebSocket debug endpoint for OpenPLC Runtime v4

This module provides a secure WebSocket interface for debugger communication.
It receives debug commands in hex format, forwards them to the Unix socket,
and returns responses through the WebSocket connection.
"""

from flask import request
from flask_jwt_extended import verify_jwt_in_request
from jwt import ExpiredSignatureError
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
    except ExpiredSignatureError:
        # Distinguishable on purpose (review 2026-08-20, R1): expiry is the ONE
        # failure the client can repair -- its token manager re-logins and
        # pushes the fresh token through the 'reauth' event below. Everything
        # else (revocation, tamper) stays a generic refusal.
        logger.info("Debug command deferred: session token expired (client may reauth)")
        return "expired"
    except Exception as e:
        logger.warning("Debug command rejected, token no longer valid: %s", e)
        return False


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

    @_socketio.on("reauth", namespace="/api/debug")
    def handle_reauth(data):
        """Swap this session's token for a fresh one, after FULL verification.

        The per-command re-verification (above) makes the connect-time token a
        15-minute fuse; this is the renewal path (review 2026-08-20, R1). The
        new token goes through the same pipeline as every command -- signature,
        expiry, revocation, user lookup -- so reauth can never LOWER the bar,
        only extend a session that could re-login anyway.
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

            # Re-authenticate EVERY command, not just the connect. See
            # _reverify_session_token: an expired or revoked token must stop
            # working on a socket that is already open.
            verdict = _reverify_session_token()
            if verdict is not True:
                emit(
                    "debug_response",
                    {"success": False, "error": "token_expired" if verdict == "expired" else "Unauthorized"},
                )
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
