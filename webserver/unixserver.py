import socket
import threading
import logging
import os
from logger import get_logger, LogParser

logger = get_logger("logger", use_buffer=True)


class UnixLogServer:
    def __init__(self, socket_path="/run/runtime/log_runtime.socket"):
        self.socket_path = socket_path
        self.server_socket = None
        self.clients = []
        self.lock = threading.Lock()
        self.running = False

    def start(self):
        """Start the Unix socket server"""
        if self.running:
            logger.warning("Server already running")
            return

        try:
            # Ensure the socket does not already exist
            try:
                os.unlink(self.socket_path)
            except OSError:
                if os.path.exists(self.socket_path):
                    raise

            self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server_socket.bind(self.socket_path)
            self.server_socket.listen(1)
            self.running = True
            threading.Thread(target=self._accept_clients, daemon=True).start()
            logger.info("Log server started at %s", self.socket_path)
        except (OSError, socket.error) as e:
            logger.error("Failed to start server: %s", e)
        except Exception as e:
            logger.error("Failed to start server (unexpected): %s", e)
            raise

    def _accept_clients(self):
        """Accept incoming client connections"""
        while self.running:
            try:
                client_sock, _ = self.server_socket.accept()
                with self.lock:
                    self.clients.append(client_sock)
                threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
                logger.info("Client connected")
            except (OSError, socket.error) as e:
                if self.running:
                    logger.error("Socket error: %s", e)
            except Exception as e:
                logger.error("Error accepting client: %s", e)

    def _handle_client(self, client_sock: socket.socket):
        """Handle communication with a connected client"""
        try:
            with client_sock.makefile('r') as f:
                for line in f:
                    # self.parse_and_log(line)
                    logger.parse_and_log(line)
        except (OSError, socket.error) as e:
            logger.error("Socket error: %s", e)
        except Exception as e:
            logger.error("Error handling client: %s", e)
        finally:
            with self.lock:
                self.clients.remove(client_sock)
            client_sock.close()
            logger.info("Client disconnected")

    # def parse_and_log(self, line: str):
    #     sline = line.strip()
    #     if not sline:
    #         return
        
    #     match = LOG_PATTERN.match(line.strip())
    #     if match:
    #         level = LEVEL_MAP.get(match["level"], logging.INFO)
    #         message = match["message"]

    #         # Re-log into Python logging system
    #         record = collector_logger.makeRecord(
    #             name="external",
    #             level=level,
    #             fn="",
    #             lno=0,
    #             msg=message,
    #             args=(),
    #             exc_info=None
    #         )
    #         record.source = "external"  # mark as external
    #         collector_logger.handle(record)
    #     else:
    #         record = collector_logger.makeRecord(
    #             name="external",
    #             level=logging.INFO,
    #             fn="",
    #             lno=0,
    #             msg=f"RAW: {line.strip()}",
    #             args=(),
    #             exc_info=None
    #         )
    #         record.source = "external"
    #         collector_logger.handle(record)

    def stop(self):
        """Stop the Unix socket server"""
        if not self.running:
            logger.warning("Server not running")
            return

        self.running = False
        if self.server_socket:
            self.server_socket.close()
            self.server_socket = None
        with self.lock:
            for client in self.clients:
                client.close()
            self.clients.clear()
        try:
            os.unlink(self.socket_path)
        except OSError:
            if os.path.exists(self.socket_path):
                logger.error("Failed to remove socket file")
        logger.info("Log server stopped")
