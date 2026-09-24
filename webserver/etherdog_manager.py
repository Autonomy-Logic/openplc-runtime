# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Supervises EtherDOG, the EtherCAT master service, and talks to it.

Starts it, hands it the bus configuration from the upload, and forwards commands. plc_main
reaches it through the session file written here (control endpoint + token).
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from webserver.logger import get_logger

logger, _ = get_logger("logger", use_buffer=True)

IS_WINDOWS = platform.system() != "Linux"

DEFAULT_BINARY = "./build/etherdog"
DEFAULT_RUN_DIR = Path("/run/runtime")
BUSCONFIG_NAME = "ethercat_busconfig.json"
DEFAULT_BUSCONFIG_PATH = Path("./build/plugins") / BUSCONFIG_NAME

# Same policy as the PLC runtime: this many exits within the window disables EtherDOG
MAX_RAPID_EXITS = 3
RAPID_EXIT_WINDOW_S = 30.0
READY_TIMEOUT_S = 10.0
STALE_STOP_TIMEOUT_S = 5.0
OUTPUT_TAIL_LINES = 20

USAGE_ERROR_EXIT = 2
MISSING_LIBRARY_EXITS = (127, 0xC0000135)  # Cygwin loader, Windows STATUS_DLL_NOT_FOUND
NPCAP_MARKERS = ("wpcap", "packet.dll", "npcap")
NPCAP_REASON = (
    "Npcap is not installed; EtherCAT requires Npcap (https://npcap.com) to access the "
    "network interface. Install it and restart the runtime"
)


class EtherDogUnavailable(RuntimeError):
    """EtherDOG is not installed, not running, or refused the request."""


@dataclass
class EtherDogPaths:
    binary: str
    run_dir: Path
    busconfig: Path

    @property
    def control(self) -> str:
        if IS_WINDOWS:
            return "tcp:127.0.0.1:18444"
        return f"unix:{self.run_dir / 'etherdog.socket'}"

    @property
    def state_dir(self) -> Path:
        return self.run_dir / "etherdog"

    @property
    def session_file(self) -> Path:
        return self.run_dir / "etherdog.json"

    @property
    def token_file(self) -> Path:
        return self.state_dir / "token"


def _connect(control: str, timeout: float) -> socket.socket:
    if control.startswith("unix:"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(control[len("unix:") :])
        return sock
    host, _, port = control[len("tcp:") :].rpartition(":")
    return socket.create_connection((host, int(port)), timeout=timeout)


class EtherDogManager:
    """Start, supervise and command one EtherDOG process."""

    def __init__(
        self,
        binary: str = DEFAULT_BINARY,
        run_dir: Path = DEFAULT_RUN_DIR,
        busconfig: Path = DEFAULT_BUSCONFIG_PATH,
        log_socket: str | None = None,
    ) -> None:
        self.paths = EtherDogPaths(binary=binary, run_dir=run_dir, busconfig=busconfig)
        # Same log server plc_main writes to, so EtherDOG's lines reach the runtime log stream.
        self.log_socket = log_socket or f"unix:{run_dir / 'log_runtime.socket'}"
        self._token = ""
        self._process: subprocess.Popen[str] | None = None
        self._pump: threading.Thread | None = None
        self._output: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)
        self._lock = threading.Lock()
        self._running = False
        self._exit_times: list[float] = []
        self._disabled_reason: str | None = None
        self._monitor: threading.Thread | None = None

    # --- process lifecycle -------------------------------------------------------------

    @property
    def installed(self) -> bool:
        return os.path.isfile(self.paths.binary) or os.path.isfile(self.paths.binary + ".exe")

    @property
    def disabled_reason(self) -> str | None:
        """Why EtherDOG is not running, or None while it is supervised."""
        return self._disabled_reason

    def start(self) -> None:
        """Start EtherDOG (if installed) and keep it running. Never raises: without EtherDOG
        the runtime still runs, only EtherCAT is unavailable."""
        if not self.installed:
            self._disable(f"EtherDOG is not installed at {self.paths.binary}")
            return
        self._disabled_reason = None
        self._exit_times.clear()
        self._running = True
        self._monitor = threading.Thread(target=self._supervise, daemon=True)
        self._monitor.start()

    def stop(self) -> None:
        self._running = False
        proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                self.command({"command": "shutdown"}, timeout=5.0)
            except EtherDogUnavailable:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _disable(self, reason: str) -> None:
        self._running = False
        self._disabled_reason = reason
        logger.warning("%s. EtherCAT is disabled; the runtime continues without it.", reason)
        try:
            _write_private(self.paths.session_file, json.dumps({"disabled": reason}) + "\n")
        except OSError as e:
            logger.error("Could not write the EtherDOG session file: %s", e)

    def _saved_token(self) -> str | None:
        try:
            token = self.paths.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token if len(token) == 64 and all(c in "0123456789abcdef" for c in token) else None

    def _write_session(self) -> None:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        # Kept across restarts so a leftover EtherDOG can still be told to shut down
        self._token = self._saved_token() or secrets.token_hex(32)
        _write_private(self.paths.token_file, self._token + "\n")
        session = {
            "control": self.paths.control,
            "token": self._token,
            "data": "udp" if IS_WINDOWS else "unix",
        }
        _write_private(self.paths.session_file, json.dumps(session) + "\n")

    def _spawn(self) -> bool:
        """Start the process. False when it cannot be executed at all."""
        cmd = [
            self.paths.binary,
            "--control",
            self.paths.control,
            "--token-file",
            str(self.paths.token_file),
            "--state-dir",
            str(self.paths.state_dir),
            "--log-socket",
            self.log_socket,
        ]
        self._output.clear()
        try:
            self._write_session()
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            self._process = None
            self._disable(f"EtherDOG cannot be started: {e}")
            return False
        self._pump = threading.Thread(target=self._pump_logs, args=(self._process,), daemon=True)
        self._pump.start()
        if self._wait_ready(timeout=READY_TIMEOUT_S):
            logger.info("EtherDOG started (pid %d)", self._process.pid)
            self._reapply_busconfig()
        elif self._process.poll() is None:
            logger.error("EtherDOG did not open its control socket in time")
        return True

    def _pump_logs(self, proc: subprocess.Popen[str]) -> None:
        """Drain EtherDOG's stdout/stderr and keep the tail for exit diagnosis. Its log lines
        already reach the log stream through the log socket."""
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self._output.append(line)

    def _wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process is None or self._process.poll() is not None:
                return False
            try:
                self.command({"command": "status"}, timeout=2.0)
                return True
            except EtherDogUnavailable:
                time.sleep(0.2)
        return False

    def _record_exit(self) -> bool:
        """Record an exit; True when it is one too many within the window."""
        now = time.monotonic()
        self._exit_times = [t for t in self._exit_times if now - t < RAPID_EXIT_WINDOW_S]
        self._exit_times.append(now)
        return len(self._exit_times) >= MAX_RAPID_EXITS

    def _stop_stale(self) -> None:
        """Shut down an EtherDOG left running by an earlier webserver, which would otherwise hold
        the control endpoint. Its token is still in the token file."""
        self._token = self._saved_token() or ""
        if not self._token:
            return
        try:
            self.command({"command": "shutdown"}, timeout=2.0)
        except EtherDogUnavailable:
            return
        logger.warning("Stopped an EtherDOG left running by a previous webserver")
        deadline = time.monotonic() + STALE_STOP_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                with _connect(self.paths.control, 0.5):
                    pass
            except OSError:
                return
            time.sleep(0.2)
        logger.error("The previous EtherDOG did not exit; the new one may fail to start")

    def _supervise(self) -> None:
        self._stop_stale()
        if not self._spawn():
            return
        while self._running:
            proc = self._process
            if proc is None:
                return
            code = proc.wait()
            if self._pump is not None:
                self._pump.join(timeout=2.0)
            if not self._running:
                return
            # EtherDOG exits 0 only when asked to stop (SIGINT/SIGTERM or "shutdown")
            if code == 0:
                self._running = False
                logger.info("EtherDOG stopped; not restarting it")
                return
            output = list(self._output)
            reason = _fatal_exit_reason(code, output)
            if reason is not None:
                for line in output[-5:]:
                    logger.error("[ETHERDOG] %s", line)
                self._disable(reason)
                return
            if self._record_exit():
                self._disable(
                    f"EtherDOG exited {MAX_RAPID_EXITS} times within {RAPID_EXIT_WINDOW_S:.0f} s "
                    f"(last exit code {code})"
                )
                return
            logger.warning("EtherDOG exited (code %s); restarting", code)
            if not self._spawn():
                return

    # --- commands ------------------------------------------------------------------------

    def command(self, request: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        """Send one command and return EtherDOG's JSON reply."""
        if self._disabled_reason is not None:
            raise EtherDogUnavailable(self._disabled_reason)
        if not self.installed:
            raise EtherDogUnavailable("EtherDOG is not installed")
        try:
            with _connect(self.paths.control, timeout) as sock:
                sock.settimeout(timeout)
                reader = sock.makefile("r", encoding="utf-8")
                hello = {"command": "hello", "params": {"token": self._token}}
                sock.sendall(json.dumps(hello).encode() + b"\n")
                reply = json.loads(reader.readline() or "{}")
                if "error" in reply:
                    raise EtherDogUnavailable(reply["error"])
                sock.sendall(json.dumps(request).encode() + b"\n")
                line = reader.readline()
        except (OSError, ValueError) as e:
            raise EtherDogUnavailable(f"EtherDOG unreachable: {e}") from e
        if not line:
            raise EtherDogUnavailable("EtherDOG closed the connection")
        try:
            result = json.loads(line)
        except ValueError as e:
            raise EtherDogUnavailable(f"invalid reply from EtherDOG: {e}") from e
        return result if isinstance(result, dict) else {"error": "invalid reply from EtherDOG"}

    def plugin_style_command(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Same contract as RuntimeManager.send_plugin_command: errors come back as {"error"}."""
        try:
            return self.command(request, timeout=timeout)
        except EtherDogUnavailable as e:
            return {"error": str(e)}

    # --- bus configuration -----------------------------------------------------------------

    def apply_busconfig(self, source: Path | None) -> None:
        """Install the bus configuration from an upload (None removes it) and load it."""
        with self._lock:
            if source is not None:
                self.paths.busconfig.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, self.paths.busconfig)
            elif self.paths.busconfig.exists():
                self.paths.busconfig.unlink()
            if self._running and self._process is not None and self._process.poll() is None:
                self._configure_locked()
                return
        # A new program gets a fresh start, as the PLC runtime does after safe mode
        if not self._running and self.installed:
            logger.info("Retrying EtherDOG for the new program")
            self.start()

    def _reapply_busconfig(self) -> None:
        with self._lock:
            self._configure_locked()

    def _configure_locked(self) -> None:
        path = self.paths.busconfig
        params = {"path": str(path.resolve())} if path.exists() else {}
        try:
            # The bus belongs to the program being replaced; stop it before reconfiguring.
            self.command({"command": "stop"}, timeout=15.0)
            result = self.command({"command": "configure", "params": params}, timeout=15.0)
        except EtherDogUnavailable as e:
            logger.error("Could not configure EtherDOG: %s", e)
            return
        if "error" in result:
            logger.error("EtherDOG rejected the bus configuration: %s", result["error"])
        elif params:
            logger.info("EtherDOG bus configuration loaded from %s", path)


def _fatal_exit_reason(code: int, output: list[str]) -> str | None:
    """Why EtherDOG can never start as installed, or None when a restart may help."""
    text = "\n".join(output).lower()
    missing_library = (
        code in MISSING_LIBRARY_EXITS or "error while loading shared libraries" in text
    )
    if missing_library:
        if IS_WINDOWS and (any(m in text for m in NPCAP_MARKERS) or not output):
            return NPCAP_REASON
        detail = output[-1] if output else f"exit code {code}"
        return f"EtherDOG cannot load a required library ({detail})"
    if code == USAGE_ERROR_EXIT:
        return "EtherDOG rejected its command line (exit code 2)"
    return None


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def legacy_ethercat_config_in_use(conf_dir: Path) -> bool:
    """True when an upload carries the pre-split ethercat.json with real masters in it.

    Editors before the split always wrote conf/ethercat.json, empty when the project has no
    EtherCAT, so only a file that actually describes masters is a problem.
    """
    legacy = conf_dir / "ethercat.json"
    if not legacy.exists():
        return False
    try:
        data = json.loads(legacy.read_text(encoding="utf-8") or "null")
    except (OSError, ValueError):
        return False
    return isinstance(data, list) and any(
        isinstance(m, dict) and str(m.get("protocol", "")).upper() == "ETHERCAT" for m in data
    )
