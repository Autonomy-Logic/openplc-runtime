# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Transport contract the supervisor drives. Standard library only.

A ``Connection`` is one broker session: it is opened once, serviced from the
supervisor thread only and then closed or aborted. It is never reused, so
nothing queued on a lost session is replayed on the next one.
"""

from __future__ import annotations

import ssl
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Protocol

from contract.constants import KEEPALIVE_S

from .strategy import InboundMessage, OutboundMessage

TRANSPORT_MESSAGES: Final[dict[str, str]] = {
    "network_dns_failure": "the broker host name could not be resolved",
    "network_connect_timeout": "the broker did not accept the connection within the timeout",
    "network_connection_refused": "the broker refused the TCP connection",
    "network_unreachable": "the broker network is unreachable",
    "network_connection_lost": "the connection to the broker was lost",
    "network_keepalive_timeout": "the broker stopped answering keepalive requests",
    "broker_certificate_expired": "the broker certificate has expired",
    "broker_certificate_hostname_mismatch": "the broker certificate does not match the host",
    "broker_certificate_untrusted": "the broker certificate is not issued by a trusted CA",
    "broker_certificate_invalid": "the broker certificate is not valid",
    "tls_protocol_unsupported": "TLS 1.2 or later could not be negotiated with the broker",
    "client_certificate_rejected": "the broker rejected the client certificate",
    "client_certificate_expired": "the broker reported the client certificate as expired",
    "auth_rejected": "the broker rejected the credentials",
    "auth_client_id_rejected": "the broker rejected the client ID",
    "auth_broker_unavailable": "the broker refused the session",
    "inbound_packet_too_large": "the broker sent a packet above the inbound size limit",
    "internal_error": "unexpected transport failure",
}


class TransportError(Exception):
    """A connection attempt or session failed; ``code`` is a catalog code, ``message`` is fixed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def transport_error(code: str) -> TransportError:
    return TransportError(code, TRANSPORT_MESSAGES[code])


class TransportCancelled(Exception):
    """The attempt was abandoned because the integration is stopping."""


class PublishResult(Enum):
    ACCEPTED = "accepted"
    BACKLOG_FULL = "backlog_full"
    TOO_LARGE = "too_large"
    NOT_CONNECTED = "not_connected"


@dataclass(frozen=True)
class ConnectSpec:
    host: str
    port: int
    client_id: str
    tls_context: ssl.SSLContext | None
    username: str | None = field(default=None, repr=False)
    password: bytes | None = field(default=None, repr=False)
    will: OutboundMessage | None = None
    keepalive_s: int = KEEPALIVE_S


@dataclass(frozen=True)
class Drained:
    forwarded: int = 0
    inbound: tuple[InboundMessage, ...] = ()
    inbound_dropped: int = 0


class Connection(Protocol):
    def open(self, cancel: threading.Event, timeout_s: float) -> None:
        """Connect and wait for the broker's acceptance within ``timeout_s``."""

    def subscribe(self, topic: str, qos: int) -> None: ...

    def publish(self, message: OutboundMessage) -> PublishResult: ...

    def service(self, timeout_s: float) -> None:
        """Run network I/O for at most ``timeout_s``; raise ``TransportError`` on session loss."""

    def backlog_full(self) -> bool: ...

    def drain(self) -> Drained:
        """Messages confirmed as forwarded and inbound messages received since the last call."""

    def close(self, final: Sequence[OutboundMessage], timeout_s: float) -> int:
        """Send ``final`` and disconnect within ``timeout_s``; return the count never forwarded."""

    def abort(self) -> int:
        """Drop the session at once; returns accepted messages never forwarded."""


ConnectionFactory = Callable[[ConnectSpec], Connection]
