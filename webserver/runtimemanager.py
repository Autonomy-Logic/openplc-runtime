import json
import subprocess
import socket
import threading
import time
import os
import psutil
from unixserver import UnixLogServer
from unixclient import SyncUnixClient
from logger import get_logger, LogParser

logger, buffer = get_logger(use_buffer=True)


class RuntimeManager:
    def __init__(self, runtime_path, plc_socket, log_socket):
        self.runtime_path = runtime_path
        self.plc_socket = plc_socket
        self.log_socket = log_socket
        self.process = None
        self.log_server = UnixLogServer(log_socket)
        self.runtime_socket = SyncUnixClient(plc_socket)
        self.monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self.running = False


    def find_running_process(self):
        """
        Find the running PLC runtime process
        """
        # Find the running PLC runtime process by executable path
        for proc in psutil.process_iter(['pid', 'exe', 'cmdline']):
            try:
                # First try to match by executable path (most reliable)
                if proc.info['exe'] and os.path.samefile(proc.info['exe'], self.runtime_path):
                    return proc
                
                # Alternatively, match by command line (fallback)
                cmdline = proc.info.get('cmdline')
                if cmdline and isinstance(cmdline, (list, tuple)) and len(cmdline) > 0:
                    cmdline_str = ' '.join(str(arg) for arg in cmdline if arg is not None)
                    if self.runtime_path in cmdline_str:
                        return proc
                      
            except (OSError, psutil.Error, TypeError, ValueError):
                continue
        return None
    
    
    def _safe_start_log_server(self):
        try:
            self.log_server.start()
        except (OSError, socket.error) as e:
            logger.error("Failed to start log server: %s", e)
        except Exception as e:
            logger.error("Failed to start log server (unexpected): %s", e)

    def _safe_connect_runtime_socket(self):
        try:
            self.runtime_socket.connect()
        except (FileNotFoundError, OSError, socket.error) as e:
            logger.error("Failed to connect to runtime socket: %s", e)
        except Exception as e:
            logger.error("Failed to connect to runtime socket (unexpected): %s", e)

    def _safe_stop_log_server(self):
        try:
            self.log_server.stop()
        except (OSError, socket.error) as e:
            logger.error("Failed to stop log server: %s", e)
        except Exception as e:
            logger.error("Failed to stop log server (unexpected): %s", e)

    def _safe_close_runtime_socket(self):
        try:
            self.runtime_socket.close()
        except (OSError, socket.error) as e:
            logger.error("Failed to close runtime socket: %s", e)
        except Exception as e:
            logger.error("Failed to close runtime socket (unexpected): %s", e)

    def start(self):
        """
        Start the runtime manager and the PLC runtime process
        """
        if self.running:
            logger.warning("Runtime manager already running")
            return
        
        self.running = True

        # Ensure UNIX socket paths exist
        plc_socket_dir = os.path.dirname(self.plc_socket)
        log_socket_dir = os.path.dirname(self.log_socket)
        if not os.path.exists(plc_socket_dir):
            try:
                os.makedirs(plc_socket_dir)
                logger.info("Created directory for PLC socket: %s", plc_socket_dir)
            except OSError as e:
                logger.error("Failed to create directory for PLC socket: %s", e)
        if not os.path.exists(log_socket_dir):
            try:
                os.makedirs(log_socket_dir)
                logger.info("Created directory for log socket: %s", log_socket_dir)
            except OSError as e:
                logger.error("Failed to create directory for log socket: %s", e)

        # Start runtime process if not already running
        running_process = self.find_running_process()
        if running_process:
            logger.info("Found existing PLC runtime process with PID %d", running_process.pid)
            self.process = running_process
            self._safe_start_log_server()
            self._safe_connect_runtime_socket()
        else:
            logger.info("Starting PLC runtime core...")
            self._safe_start_log_server()
            try:
                self.process = subprocess.Popen([self.runtime_path])
            except (OSError, subprocess.SubprocessError) as e:
                logger.error("Failed to start PLC runtime process: %s", e)
                self.process = None
            time.sleep(1)  # Give time to start
            self._safe_connect_runtime_socket()

        # Start monitor thread
        if not self.monitor_thread.is_alive():
            self.monitor_thread = threading.Thread(target=self._monitor, daemon=True)
            self.monitor_thread.start()


    def is_runtime_alive(self):
        """
        Check if the PLC runtime process is alive
        """
        if self.process is None:
            return False
        if isinstance(self.process, psutil.Process):
            if self.process.is_running() and self.process.status() != psutil.STATUS_ZOMBIE:
                return True
        elif isinstance(self.process, subprocess.Popen):
            if self.process.poll() is None:
                return True
        return False
    

    def _monitor(self):
        """
        Monitor the PLC runtime process and restart if it dies
        """
        while self.running:
            if not self.is_runtime_alive():
                # Process died, restart
                logger.warning("PLC runtime process died, restarting...")
                self._safe_stop_log_server()
                self._safe_close_runtime_socket()

                self._safe_start_log_server()
                try:
                    self.process = subprocess.Popen([self.runtime_path])
                except (OSError, subprocess.SubprocessError) as e:
                    logger.error("Failed to start PLC runtime process: %s", e)
                    self.process = None
                time.sleep(1)  # Give time to start
                self._safe_connect_runtime_socket()
            else:
                # Make sure log server and socket are connected
                if not self.log_server.running:
                    self._safe_start_log_server()
                if not self.runtime_socket.is_connected():
                    self._safe_connect_runtime_socket()

            time.sleep(2)


    def stop(self):
        """"
        Stop the runtime manager and the PLC runtime process
        """
        try:
            self.runtime_socket.send_message("STOP\n")
        except (OSError, socket.error) as e:
            logger.error("Failed to send STOP to PLC runtime: %s", e)
        except Exception as e:
            logger.error("Failed to send STOP to PLC runtime (unexpected): %s", e)
        self.running = False
        self.monitor_thread.join(timeout=5)
        time.sleep(1)
        if self.process:
            if isinstance(self.process, psutil.Process):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except (psutil.TimeoutExpired, psutil.Error) as e:
                    self.process.kill()
            elif isinstance(self.process, subprocess.Popen):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
                    self.process.kill()
            self.process = None
        self._safe_stop_log_server()
        self._safe_close_runtime_socket()


    def get_logs(self):
        """
        Get current logs from the runtime
        """
        try:
            _logs = buffer.normalize_buffer_logs(buffer.get_logs())
            return _logs
        except AttributeError as e:
            logger.error("Failed to get logs from buffer: %s", e)
            return []

    def ping(self):
        """
        Send PING and wait for PONG
        """
        try:
            self.runtime_socket.send_message("PING\n")
            return self.runtime_socket.recv_message()
        except (OSError, socket.error) as e:
            logger.error("Failed to ping PLC runtime: %s", e)
            return 'PING:ERROR\n'
        except Exception as e:
            logger.error("Failed to ping PLC runtime (unexpected): %s", e)
            return 'PING:ERROR\n'

    def start_plc(self):
        """
        Send START command
        """
        try:
            self.runtime_socket.send_message("START\n")
            return self.runtime_socket.recv_message()
        except (OSError, socket.error) as e:
            logger.error("Failed to start PLC runtime: %s", e)
            return 'START:ERROR\n'
        except Exception as e:
            logger.error("Failed to start PLC runtime (unexpected): %s", e)
            return 'START:ERROR\n'

    def stop_plc(self):
        """
        Send STOP command
        """
        try:
            self.runtime_socket.send_message("STOP\n")
            return self.runtime_socket.recv_message()
        except (OSError, socket.error) as e:
            logger.error("Failed to stop PLC runtime: %s", e)
            return 'STOP:ERROR\n'
        except Exception as e:
            logger.error("Failed to stop PLC runtime (unexpected): %s", e)
        
    def status_plc(self):
        """
        Send STATUS command
        """
        try:
            self.runtime_socket.send_message("STATUS\n")
            return self.runtime_socket.recv_message()
        except (OSError, socket.error) as e:
            logger.error("Failed to get PLC status: %s", e)
            return 'STATUS:ERROR\n'
        except Exception as e:
            logger.error("Failed to get PLC status (unexpected): %s", e)
            return 'STATUS:ERROR\n'
