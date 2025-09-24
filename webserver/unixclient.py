import socket
import os
import logging
import re
from typing import Optional
from threading import Lock

logger = logging.getLogger(__name__)
mutex = Lock()


class SyncUnixClient:
    def __init__(self, socket_path="/run/runtime/plc_runtime.socket"):
        self.socket_path = socket_path
        self.sock: Optional[socket.socket] = None

    def validate_message(self, message: str) -> bool:
        """Validate message format"""
        if not message or len(message) > 100:
            return False
        if not re.match(r"^[\w\s.,!?\-]+$", message):
            return False
        return True

    def connect(self):
        """Connect to the Unix socket server"""
        if not os.path.exists(self.socket_path):
            raise FileNotFoundError(f"Socket not found: {self.socket_path}")

        try:
            logger.info("Connecting to socket %s", self.socket_path)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(1.0)  # 1s timeout on blocking calls
            self.sock.connect(self.socket_path)
            logger.info("Connected to server socket %s", self.socket_path)
        except Exception as e:
            logger.error("Failed to connect: %s", e)
            raise

    def send_message(self, msg: str):
        if not self.sock:
            raise RuntimeError("Socket not connected")

        with mutex:
            data = msg.encode()
            try:
                self.sock.sendall(data)
                logger.info("Sent message: %s", data)
            except Exception as e:
                logger.error("Error sending message: %s", e)
                raise

    def recv_message(self, timeout: float = 0.5) -> Optional[str]:
        """Receive message from the server"""
        if not self.sock:
            raise RuntimeError("Socket not connected")

        with mutex:
            self.sock.settimeout(timeout)
            try:
                data = self.sock.recv(1024)
                if not data:
                    logger.warning("Connection closed by server")
                    return None
                message = data.decode("utf-8").strip()
                logger.info("Received message: %s", message)
                return message
            except socket.timeout:
                logger.debug("Timeout waiting for message")
                return None
            except Exception as e:
                logger.error("Error receiving message: %s", e)
                return None

    def close(self):
        if self.sock:
            logger.info("Closing connection")
            try:
                self.sock.close()
            finally:
                self.sock = None
