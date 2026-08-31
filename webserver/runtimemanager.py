import json
import os
import socket
import subprocess
import threading
import time

# psutil is optional - not available on MSYS2/Cygwin platforms
try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

from webserver.logger import get_logger
from webserver.unixclient import SyncUnixClient
from webserver.unixserver import UnixLogServer

logger, buffer = get_logger("logger", use_buffer=True)

# Log once if psutil is not available
if not HAS_PSUTIL:
    logger.info("psutil not available - process detection features disabled")


MAX_RAPID_CRASHES = 3
RAPID_CRASH_WINDOW = 30  # seconds

# How long to let the runtime shut down gracefully after SIGTERM before killing
# it. Has to exceed the worst-case graceful stop: the runtime waits for a state
# change already in flight to land (a boot start with plugin bring-up is ~4 s on
# an SLM-RP4) and then tears the program and plugins down.
RUNTIME_SHUTDOWN_TIMEOUT_S = 15


class RuntimeManager:
    def __init__(self, runtime_path, plc_socket, log_socket, print_debug=False):
        self.runtime_path = runtime_path
        self.plc_socket = plc_socket
        self.log_socket = log_socket
        self.print_debug = print_debug
        self.process = None
        self.log_server = UnixLogServer(log_socket)
        self.runtime_socket = SyncUnixClient(plc_socket)
        self.monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self.running = False
        self._crash_lock = threading.Lock()
        self._crash_times: list[float] = []
        self._safe_mode = False

    def find_running_process(self):
        """
        Find the running PLC runtime process.
        Returns None if psutil is not available (MSYS2/Cygwin).
        """
        if not HAS_PSUTIL:
            # Cannot detect existing processes without psutil
            return None

        # Find the running PLC runtime process by executable path
        for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                # First try to match by executable path (most reliable)
                if proc.info["exe"] and os.path.samefile(proc.info["exe"], self.runtime_path):
                    return proc

                # Alternatively, match by command line (fallback)
                cmdline = proc.info.get("cmdline")
                if cmdline and isinstance(cmdline, (list, tuple)) and len(cmdline) > 0:
                    cmdline_str = " ".join(str(arg) for arg in cmdline if arg is not None)
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
                cmd = [self.runtime_path]
                if self.print_debug:
                    cmd.append("--print-debug")
                self.process = subprocess.Popen(cmd)
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
        if HAS_PSUTIL and isinstance(self.process, psutil.Process):
            if self.process.is_running() and self.process.status() != psutil.STATUS_ZOMBIE:
                return True
        elif isinstance(self.process, subprocess.Popen):
            if self.process.poll() is None:
                return True
        return False

    def _start_runtime_process(self, safe_mode=False):
        """Start the runtime process, optionally in safe mode."""
        self._safe_start_log_server()
        try:
            cmd = [self.runtime_path]
            if self.print_debug:
                cmd.append("--print-debug")
            if safe_mode:
                cmd.append("--safe-mode")
            self.process = subprocess.Popen(cmd)
        except (OSError, subprocess.SubprocessError) as e:
            logger.error("Failed to start PLC runtime process: %s", e)
            self.process = None
        time.sleep(1)  # Give time to start
        self._safe_connect_runtime_socket()

    def _record_crash_and_check_safe_mode(self):
        """Record a crash timestamp and check if safe mode should be entered."""
        with self._crash_lock:
            now = time.time()
            # Keep only crashes within the time window
            self._crash_times = [t for t in self._crash_times if now - t < RAPID_CRASH_WINDOW]
            self._crash_times.append(now)
            return len(self._crash_times) >= MAX_RAPID_CRASHES

    def _monitor(self):
        """
        Monitor the PLC runtime process and restart if it dies.
        Tracks crash frequency and enters safe mode after repeated failures.
        """
        while self.running:
            if not self.is_runtime_alive():
                logger.warning("PLC runtime process died unexpectedly")
                self._safe_stop_log_server()
                self._safe_close_runtime_socket()

                if self._record_crash_and_check_safe_mode():
                    with self._crash_lock:
                        if not self._safe_mode:
                            logger.error(
                                "PLC program caused %d crashes within %d seconds. "
                                "Restarting runtime in SAFE MODE - "
                                "PLC program will NOT be loaded. "
                                "Upload a corrected program to recover.",
                                MAX_RAPID_CRASHES,
                                RAPID_CRASH_WINDOW,
                            )
                            self._safe_mode = True
                    self._start_runtime_process(safe_mode=True)
                else:
                    logger.warning("Restarting PLC runtime...")
                    self._start_runtime_process(safe_mode=False)
            else:
                # Make sure log server and socket are connected
                if not self.log_server.running:
                    self._safe_start_log_server()
                if not self.runtime_socket.is_connected():
                    self._safe_connect_runtime_socket()

            time.sleep(2)

    def stop(self):
        """ "
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
            # SIGTERM now reaches a handler in the runtime, so terminate() starts a
            # real shutdown: it stops the PLC program, waits out any state change
            # already in flight (a boot start is ~4 s on an SLM-RP4), stops the
            # plugins and unloads the program. Wait long enough for that to finish,
            # or the SIGKILL below would preempt the very cleanup the signal asked
            # for -- which is what happened for every stop while SIGTERM had no
            # handler at all. The kill stays as the backstop for a hung teardown.
            if HAS_PSUTIL and isinstance(self.process, psutil.Process):
                self.process.terminate()
                try:
                    self.process.wait(timeout=RUNTIME_SHUTDOWN_TIMEOUT_S)
                except (psutil.TimeoutExpired, psutil.Error):
                    logger.warning(
                        "PLC runtime did not exit within %d s of SIGTERM; killing it",
                        RUNTIME_SHUTDOWN_TIMEOUT_S,
                    )
                    self.process.kill()
            elif isinstance(self.process, subprocess.Popen):
                self.process.terminate()
                try:
                    self.process.wait(timeout=RUNTIME_SHUTDOWN_TIMEOUT_S)
                except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                    logger.warning(
                        "PLC runtime did not exit within %d s of SIGTERM; killing it",
                        RUNTIME_SHUTDOWN_TIMEOUT_S,
                    )
                    self.process.kill()
            self.process = None
        self._safe_stop_log_server()
        self._safe_close_runtime_socket()

    def reset_crash_tracking(self):
        """Reset crash tracking state after a successful program upload."""
        with self._crash_lock:
            self._crash_times.clear()
            self._safe_mode = False

    def get_logs(self, min_id=None, level=None):
        """
        Get current logs from the runtime
        """
        try:
            _logs = buffer.normalize_logs(buffer.get_logs(min_id=min_id, level=level))
            return _logs
        except AttributeError as e:
            logger.error("Failed to get logs from buffer: %s", e)
            return []

    def ping(self):
        """
        Send PING and wait for PONG
        """
        try:
            return self.runtime_socket.send_and_receive("PING\n")
        except (OSError, socket.error) as e:
            logger.error("Failed to ping PLC runtime: %s", e)
            return "PING:ERROR\n"
        except Exception as e:
            logger.error("Failed to ping PLC runtime (unexpected): %s", e)
            return "PING:ERROR\n"

    def start_plc(self):
        """
        Send START command
        """
        try:
            return self.runtime_socket.send_and_receive("START\n")
        except (OSError, socket.error) as e:
            logger.error("Failed to start PLC runtime: %s", e)
            return "START:ERROR\n"
        except Exception as e:
            logger.error("Failed to start PLC runtime (unexpected): %s", e)
            return "START:ERROR\n"

    def stop_plc(self):
        """
        Send STOP command
        """
        try:
            return self.runtime_socket.send_and_receive("STOP\n")
        except (OSError, socket.error) as e:
            logger.error("Failed to stop PLC runtime: %s", e)
            return "STOP:ERROR\n"
        except Exception as e:
            logger.error("Failed to stop PLC runtime (unexpected): %s", e)
            return "STOP:ERROR\n"

    def clear_retained(self):
        """
        Discard the runtime's stored retained values.

        Called on program upload. CODESYS clears retained memory on download,
        and a new program's retained values have no business surviving into it
        — the layout hash would refuse a genuinely different program, but two
        programs that happen to share a retain layout would otherwise inherit
        each other's values silently.

        Failure is logged and swallowed: the upload itself is what matters, and
        a runtime that is not currently reachable has nothing stored that this
        program will read anyway.
        """
        try:
            return self.runtime_socket.send_and_receive("RETAIN:CLEAR\n")
        except (OSError, socket.error) as e:
            logger.warning("Could not clear retained values: %s", e)
            return "RETAIN:ERROR\n"
        except Exception as e:
            logger.warning("Could not clear retained values (unexpected): %s", e)
            return "RETAIN:ERROR\n"

    def retain_status(self):
        """Ask the core which backend is actually holding the retained bytes.

        Not the same question as what ``retain.conf`` says. A VPP plugin
        overrides the built-in file store, so a device can have the file store
        switched on in settings while a plugin does the work — and the
        Persistent Storage screen has to be able to say so rather than report
        a file that will never grow.

        Returns ``{"active", "backend", "detail"}``; ``backend`` is one of
        ``none`` / ``plugin`` / ``file``. A runtime that is not reachable, or
        one too old to answer, reports ``unknown`` rather than guessing.
        """
        unknown = {"active": False, "backend": "unknown", "detail": ""}
        try:
            reply = self.runtime_socket.send_and_receive("RETAIN:STATUS\n")
        except (OSError, socket.error) as e:
            logger.warning("Could not read retain status: %s", e)
            return unknown
        except Exception as e:
            logger.warning("Could not read retain status (unexpected): %s", e)
            return unknown

        parts = (reply or "").strip().split()
        # "RETAIN:STATUS <active|inactive> <backend> [detail]"
        if len(parts) < 3 or parts[0] != "RETAIN:STATUS":
            return unknown
        return {
            "active": parts[1] == "active",
            "backend": parts[2],
            "detail": parts[3] if len(parts) > 3 else "",
        }

    def status_plc(self):
        """
        Send STATUS command
        """
        try:
            return self.runtime_socket.send_and_receive("STATUS\n")
        except (OSError, socket.error) as e:
            logger.error("Failed to get PLC status: %s", e)
            return "STATUS:ERROR\n"
        except Exception as e:
            logger.error("Failed to get PLC status (unexpected): %s", e)
            return "STATUS:ERROR\n"

    def switch_plc(self) -> str:
        """
        Send SWITCH command to read the run/stop mode-switch position.

        Answers ``SWITCH:RUN`` or ``SWITCH:STOP``. Devices with no switch-aware
        VPP plugin always report RUN, so callers need no special case for them.
        """
        try:
            return self.runtime_socket.send_and_receive("SWITCH\n")
        except (OSError, socket.error) as e:
            logger.error("Failed to get mode switch position: %s", e)
            return "SWITCH:ERROR\n"
        except Exception as e:
            logger.error("Failed to get mode switch position (unexpected): %s", e)
            return "SWITCH:ERROR\n"

    def stats_plc(self):
        """
        Send STATS command to get timing statistics
        """
        try:
            return self.runtime_socket.send_and_receive("STATS\n")
        except (OSError, socket.error) as e:
            logger.error("Failed to get PLC stats: %s", e)
            return None
        except Exception as e:
            logger.error("Failed to get PLC stats (unexpected): %s", e)
            return None

    def send_plugin_command(self, plugin_name: str, command_json: str, timeout: float = 10.0):
        """Send a command to a plugin via the C runtime unix socket."""
        try:
            msg = f"PLUGIN_CMD:{plugin_name}:{command_json}\n"
            response = self.runtime_socket.send_and_receive(msg, timeout=timeout)
            if response is None:
                return {"error": "No response from runtime (timeout)"}

            # Parse: "PLUGIN_CMD:OK:{json}" or "PLUGIN_CMD:ERROR:{json}"
            if response.startswith("PLUGIN_CMD:OK:"):
                json_str = response[len("PLUGIN_CMD:OK:"):]
                return json.loads(json_str)
            elif response.startswith("PLUGIN_CMD:ERROR:"):
                json_str = response[len("PLUGIN_CMD:ERROR:"):]
                return json.loads(json_str)
            else:
                return {"error": f"Unexpected response: {response[:200]}"}
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON in response: {response[:200]}"}
        except (OSError, socket.error) as e:
            logger.error("Failed to send plugin command: %s", e)
            return {"error": str(e)}
        except Exception as e:
            logger.error("Failed to send plugin command (unexpected): %s", e)
            return {"error": str(e)}
