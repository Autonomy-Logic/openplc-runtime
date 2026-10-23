# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""
An all-interfaces endpoint (opc.tcp://0.0.0.0:...) must listen on every interface.

normalize_endpoint_url() replaces 0.0.0.0 with one reachable address so the
endpoint URL handed to clients is usable. asyncua listens on the endpoint host,
so without an explicit socket address the server ends up listening on that one
address only (issue #210).
"""

import asyncio
import os
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_PLUGINS_PYTHON = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "core",
        "src",
        "drivers",
        "plugins",
        "python",
    )
)
_OPCUA_DIR = os.path.join(_PLUGINS_PYTHON, "opcua")
for _path in (_PLUGINS_PYTHON, _OPCUA_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import opcua_endpoints_config  # noqa: E402
from opcua_endpoints_config import bind_address_for  # noqa: E402


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("opc.tcp://0.0.0.0:4840", ("0.0.0.0", 4840)),
        ("opc.tcp://0.0.0.0:4841/openplc/opcua", ("0.0.0.0", 4841)),
        ("opc.tcp://192.168.1.10:4840/openplc/opcua", None),
        ("opc.tcp://127.0.0.1:4840", None),
        ("opc.tcp://localhost:4840/openplc/opcua", None),
        ("opc.tcp://plc-host:4840", None),
        ("opc.tcp://0.0.0.0/openplc/opcua", None),
    ],
)
def test_bind_address_for(endpoint, expected):
    assert bind_address_for(endpoint) == expected


class _NoSecurity:
    """Stands in for OpcuaSecurityManager: no certificates, no policies to set."""

    async def setup_server_security(self, server, profiles, app_uri=None):
        return None

    async def setup_certificate_validation(self, server, certificates):
        return None


def _manager(endpoint_url):
    from server import OpcuaServerManager

    config = SimpleNamespace(
        server=SimpleNamespace(
            endpoint_url=endpoint_url,
            name="OpenPLC bind test",
            application_uri="urn:openplc:test:bind",
            product_uri="urn:openplc:test",
            security_profiles=[],
        ),
        security=SimpleNamespace(trusted_client_certificates=[]),
        address_space=SimpleNamespace(namespace_uri="urn:openplc:test:ns"),
    )
    manager = OpcuaServerManager.__new__(OpcuaServerManager)
    manager.config = config
    manager.server = None
    manager.running = False
    manager.namespace_idx = None
    manager.security_manager = _NoSecurity()
    manager.user_manager = None
    manager._client_endpoints = {}
    return manager


async def _listening_hosts(endpoint_url, advertised_ip):
    manager = _manager(endpoint_url)
    with patch.object(opcua_endpoints_config, "get_local_ip", return_value=advertised_ip):
        assert await manager._setup_server()
    await manager.server.start()
    try:
        sockets = manager.server.bserver._server.sockets
        return manager.server.endpoint.hostname, {s.getsockname()[0] for s in sockets}
    finally:
        await manager.server.stop()


def test_all_interfaces_endpoint_listens_on_every_interface():
    advertised, hosts = asyncio.run(
        _listening_hosts("opc.tcp://0.0.0.0:0/openplc/opcua", "127.0.0.1")
    )
    # Clients are still told one concrete address...
    assert advertised == "127.0.0.1"
    # ...but the listener is not limited to it.
    assert hosts == {"0.0.0.0"}


def test_specific_address_endpoint_listens_on_that_address_only():
    advertised, hosts = asyncio.run(
        _listening_hosts("opc.tcp://127.0.0.1:0/openplc/opcua", "192.0.2.1")
    )
    assert advertised == "127.0.0.1"
    assert hosts == {"127.0.0.1"}


def _non_loopback_ipv4():
    """A local IPv4 address other than loopback, or None when the host has none."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1; UDP connect sends nothing
        ip = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if ip.startswith("127.") else ip


async def _connect_on_both(other_ip):
    manager = _manager("opc.tcp://0.0.0.0:0/openplc/opcua")
    with patch.object(opcua_endpoints_config, "get_local_ip", return_value=other_ip):
        assert await manager._setup_server()
    await manager.server.start()
    try:
        port = manager.server.bserver.port
        reached = set()
        for host in ("127.0.0.1", other_ip):
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2)
            writer.close()
            await writer.wait_closed()
            reached.add(host)
        return reached
    finally:
        await manager.server.stop()


def test_all_interfaces_endpoint_accepts_connections_on_two_addresses():
    other_ip = _non_loopback_ipv4()
    if other_ip is None:
        pytest.skip("host has no non-loopback IPv4 address")
    # The endpoint advertises other_ip; loopback must be reachable too.
    assert asyncio.run(_connect_on_both(other_ip)) == {"127.0.0.1", other_ip}
