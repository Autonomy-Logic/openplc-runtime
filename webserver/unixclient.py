import os
import socket
from threading import Lock
from typing import Optional
from webserver.logger import get_logger

logger, _ = get_logger(use_buffer=True)
mutex = Lock()


class SyncUnixClient:
    def __init__(self, socket_path="/run/runtime/plc_runtime.socket"):
        self.socket_path = socket_path
        self.sock: Optional[socket.socket] = None

    def is_connected(self):
        with mutex:
            if self.sock is None:
                return False
            return True

    def connect(self):
        """Connect to the Unix socket server"""
        if not os.path.exists(self.socket_path):
            raise FileNotFoundError(f"Socket not found: {self.socket_path}")

        try:
            logger.debug("Connecting to socket %s", self.socket_path)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(1.0)  # 1s timeout on blocking calls
            self.sock.connect(self.socket_path)
            logger.debug("Connected to server socket %s", self.socket_path)
        except Exception as e:
            logger.error("Failed to connect: %s", e)

    def send_message(self, msg: str):
        if not self.sock:
            raise RuntimeError("Socket not connected")

        with mutex:
            data = msg.encode()
            try:
                self.sock.sendall(data)
                # logger.info("Sent message: %s", data)
            except Exception as e:
                logger.error("Error sending message: %s", e)

    def recv_message(self, timeout: float = 0.5) -> Optional[str]:
        """Receive message from the server. Reads until newline to ensure complete message."""
        if not self.sock:
            raise RuntimeError("Socket not connected")

        with mutex:
            self.sock.settimeout(timeout)
            try:
                buffer = bytearray()
                max_size = 8192 * 2 + 256

                while len(buffer) < max_size:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        if buffer:
                            break
                        return None

                    buffer.extend(chunk)

                    if b"\n" in buffer:
                        break

                if not buffer:
                    return None

                message = buffer.decode("utf-8").strip()
                logger.debug(
                    "Received message: %s",
                    message[:200] + "..." if len(message) > 200 else message,
                )
                return message
            except socket.timeout:
                logger.warning("Timeout waiting for message")
                return None
            except Exception:
                return None

    def send_and_receive(self, msg: str, timeout: float = 0.5) -> Optional[str]:
        """
        Send a message and receive response atomically with mutex held.
        This ensures no other thread can interleave send/recv operations.
        """
        if not self.sock:
            raise RuntimeError("Socket not connected")

        with mutex:
            data = msg.encode()
            try:
                self.sock.sendall(data)
            except Exception as e:
                logger.error("Error sending message: %s", e)
                return None

            self.sock.settimeout(timeout)
            try:
                buffer = bytearray()
                max_size = 8192 * 2 + 256

                while len(buffer) < max_size:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        if buffer:
                            break
                        return None

                    buffer.extend(chunk)

                    if b"\n" in buffer:
                        break

                if not buffer:
                    return None

                message = buffer.decode("utf-8").strip()
                logger.debug(
                    "Received message: %s",
                    message[:200] + "..." if len(message) > 200 else message,
                )
                return message
            except socket.timeout:
                logger.warning("Timeout waiting for message")
                return None
            except Exception:
                return None

    def close(self):
        if self.sock:
            logger.debug("Closing connection")
            try:
                self.sock.close()
            finally:
                self.sock = None
