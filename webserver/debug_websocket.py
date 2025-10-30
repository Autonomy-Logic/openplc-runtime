"""
WebSocket debug endpoint for OpenPLC Runtime v4

This module provides a secure WebSocket interface for debugger communication.
It receives debug commands in hex format, forwards them to the Unix socket,
and returns responses through the WebSocket connection.
"""

from flask import request
from flask_jwt_extended import decode_token
from flask_socketio import SocketIO, emit
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError
from webserver.logger import get_logger

logger, _ = get_logger("debug_ws", use_buffer=True)

_socketio = None  # pylint: disable=invalid-name
_unix_client = None  # pylint: disable=invalid-name


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

            try:
                decoded = decode_token(token)
                logger.info("Debug WebSocket connected for user %s", decoded.get("sub"))
                emit("connected", {"status": "ok"})
                return True

            except (ExpiredSignatureError, InvalidTokenError) as e:
                logger.warning("Debug WebSocket auth failed: %s", e)
                return False

        except Exception as e:
            logger.error("Error during debug WebSocket connection: %s", e)
            return False

    @_socketio.on("disconnect", namespace="/api/debug")
    def handle_disconnect():
        """Handle WebSocket disconnection"""
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
            if not _unix_client or not _unix_client.is_connected():
                logger.error("Unix socket not connected")
                emit(
                    "debug_response",
                    {"success": False, "error": "Runtime not connected"},
                )
                return

            command_hex = data.get("command", "")
            if not command_hex:
                logger.warning("Empty debug command received")
                emit("debug_response", {"success": False, "error": "Empty command"})
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
                    response.split(":", 2)[2]
                    if len(response.split(":")) > 2
                    else "Unknown error"
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
