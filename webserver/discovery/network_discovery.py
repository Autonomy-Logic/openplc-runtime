"""UDP network discovery responder.

Listens on UDP port 33333 for the magic string ``OPENPLC_DISCOVER_V1``
and answers each requester with a JSON record describing this runtime
instance.  Designed so the OpenPLC Editor can find headless runtimes
on a LAN without the user having to know their IP addresses.

The protocol is intentionally tiny and stateless:

    Request   : "OPENPLC_DISCOVER_V1"          (UTF-8, exact)
    Response  : JSON with {service, protocol_version, runtime_version,
                           hostname, api_port} plus {project_name,
                           project_timestamp} when a source project is
                           stored on the device

Only the magic string is accepted; anything else is dropped silently.
Replies are unicast to the source address of the request, which is
the authoritative way to learn the runtime's reachable IP (the
runtime itself may not know its outward-facing address on hosts with
multiple interfaces).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Optional

from webserver import project_snapshot
from webserver.logger import get_logger
from webserver.version import RUNTIME_VERSION

DISCOVERY_PORT: int = 33333
DISCOVERY_MAGIC: bytes = b"OPENPLC_DISCOVER_V1"
PROTOCOL_VERSION: int = 1
API_PORT: int = 8443

# Drop oversized packets without parsing them.  The magic string is
# 19 bytes; 64 bytes is plenty of slack for future protocol revisions
# without inviting amplification probes.
MAX_REQUEST_BYTES: int = 64

# Per-source-IP rate limit, in seconds.  Each remote IP can only
# trigger a reply this often; spammier probes get silently dropped.
# Discovery is a manual, user-driven action — a generous floor here
# still feels instant from the editor side.
PER_IP_RATE_LIMIT_SECONDS: float = 0.1

logger, _ = get_logger("discovery")


class NetworkDiscoveryResponder:
    """UDP responder that advertises this runtime on the LAN.

    Run as a daemon thread alongside the Flask server.  Bind failures
    (e.g. port already in use) are logged and swallowed so a misconfig
    cannot block runtime startup.
    """

    def __init__(self, port: int = DISCOVERY_PORT) -> None:
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_seen: dict[str, float] = {}

    def start(self) -> bool:
        """Start the responder thread.  Returns True on success."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # SO_REUSEPORT is Linux/BSD only — best-effort on other OSes.
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except (AttributeError, OSError):
                    pass
            sock.bind(("0.0.0.0", self.port))
            # Short recv timeout so the stop event is observed promptly.
            sock.settimeout(0.5)
        except OSError as exc:
            logger.warning(
                "Discovery responder could not bind UDP %d: %s. "
                "Editor LAN search will not find this runtime.",
                self.port,
                exc,
            )
            return False

        self._sock = sock
        self._thread = threading.Thread(
            target=self._run, name="network-discovery", daemon=True
        )
        self._thread.start()
        logger.info("Discovery responder listening on UDP %d", self.port)
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(MAX_REQUEST_BYTES + 1)
            except socket.timeout:
                continue
            except OSError:
                # Socket closed during shutdown.
                break

            self._handle(data, addr)

    def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        # Strict input filter: exact magic only, anything else dropped.
        if len(data) > MAX_REQUEST_BYTES or data.strip() != DISCOVERY_MAGIC:
            return

        src_ip = addr[0]
        now = time.monotonic()
        last = self._last_seen.get(src_ip, 0.0)
        if now - last < PER_IP_RATE_LIMIT_SECONDS:
            return
        self._last_seen[src_ip] = now

        # Garbage-collect the rate-limit cache occasionally so a long-
        # running runtime doesn't accumulate state from drive-by probes.
        if len(self._last_seen) > 1024:
            cutoff = now - 60.0
            self._last_seen = {
                ip: ts for ip, ts in self._last_seen.items() if ts >= cutoff
            }

        # Name and timestamp of the stored source project, when there is one.
        # This is what lets a client populate its "Retrieve Project from PLC"
        # picker without logging in to every device on the LAN first. Absent
        # keys mean no stored project, so there is no separate flag.
        #
        # Deliberately just these two: they are exactly what the picker shows.
        # Everything else about the stored project needs authentication.
        payload = json.dumps(
            {
                "service": "openplc-runtime",
                "protocol_version": PROTOCOL_VERSION,
                "runtime_version": RUNTIME_VERSION,
                "hostname": socket.gethostname(),
                "api_port": API_PORT,
                **project_snapshot.advertised_fields(),
            },
            separators=(",", ":"),
        ).encode("utf-8")

        try:
            assert self._sock is not None
            self._sock.sendto(payload, addr)
        except OSError as exc:
            logger.debug("Discovery reply to %s failed: %s", src_ip, exc)


# Module-level singleton used by webserver/app.py.
responder = NetworkDiscoveryResponder()
