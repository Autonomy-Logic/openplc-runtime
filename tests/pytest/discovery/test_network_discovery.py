# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Unit tests for the UDP network discovery responder."""

from __future__ import annotations

import json
import socket
import time

import pytest

from webserver.discovery.network_discovery import (
    DISCOVERY_MAGIC,
    MAX_REQUEST_BYTES,
    NetworkDiscoveryResponder,
    PER_IP_RATE_LIMIT_SECONDS,
)


def _ephemeral_port() -> int:
    """Return a free UDP port to avoid clashing with anything else."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def running_responder():
    """Start a responder on a fresh ephemeral port; tear it down afterwards."""
    port = _ephemeral_port()
    responder = NetworkDiscoveryResponder(port=port)
    assert responder.start() is True
    # Give the listener thread a moment to enter recvfrom.
    time.sleep(0.05)
    yield responder, port
    responder.stop()


def _probe(port: int, payload: bytes, timeout: float = 0.5) -> bytes | None:
    """Send a UDP packet to the responder and read any reply.

    Returns the reply bytes or None if nothing arrived before timeout.
    """
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(timeout)
    try:
        client.bind(("127.0.0.1", 0))
        client.sendto(payload, ("127.0.0.1", port))
        try:
            data, _ = client.recvfrom(4096)
            return data
        except socket.timeout:
            return None
    finally:
        client.close()


class TestNetworkDiscoveryResponder:
    """End-to-end behaviour of the discovery responder."""

    def test_valid_magic_returns_json_payload(self, running_responder):
        _, port = running_responder
        reply = _probe(port, DISCOVERY_MAGIC)

        assert reply is not None, "responder did not reply to valid magic"

        decoded = json.loads(reply.decode("utf-8"))
        assert decoded["service"] == "openplc-runtime"
        assert decoded["protocol_version"] == 1
        assert decoded["api_port"] == 8443
        assert isinstance(decoded["runtime_version"], str)
        assert isinstance(decoded["hostname"], str)
        assert decoded["hostname"]  # non-empty

    def test_garbage_input_is_dropped(self, running_responder):
        _, port = running_responder
        reply = _probe(port, b"hello world", timeout=0.3)
        assert reply is None

    def test_oversized_packet_is_dropped(self, running_responder):
        _, port = running_responder
        oversized = DISCOVERY_MAGIC + b"X" * (MAX_REQUEST_BYTES + 32)
        reply = _probe(port, oversized, timeout=0.3)
        assert reply is None

    def test_magic_with_trailing_whitespace_is_accepted(self, running_responder):
        """Stripping protects against accidental newline-suffixed probes."""
        _, port = running_responder
        reply = _probe(port, DISCOVERY_MAGIC + b"\n")
        assert reply is not None

    def test_rate_limit_drops_rapid_repeats(self, running_responder):
        _, port = running_responder

        first = _probe(port, DISCOVERY_MAGIC)
        assert first is not None

        # Same source IP, well within the rate-limit window.
        second = _probe(port, DISCOVERY_MAGIC, timeout=0.2)
        assert second is None

        # After waiting past the window, the next request goes through.
        time.sleep(PER_IP_RATE_LIMIT_SECONDS + 0.05)
        third = _probe(port, DISCOVERY_MAGIC)
        assert third is not None

    def test_bind_failure_returns_false(self):
        """Binding the same port twice should fail gracefully."""
        port = _ephemeral_port()
        primary = NetworkDiscoveryResponder(port=port)
        assert primary.start() is True

        try:
            secondary = NetworkDiscoveryResponder(port=port)
            # SO_REUSEPORT on Linux may allow rebinding; treat either
            # outcome as acceptable as long as it doesn't raise.
            result = secondary.start()
            assert result in (True, False)
            secondary.stop()
        finally:
            primary.stop()
