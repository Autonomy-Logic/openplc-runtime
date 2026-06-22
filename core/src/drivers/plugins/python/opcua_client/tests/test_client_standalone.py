"""
End-to-end test for the OPC-UA Client plugin.

Spins up a local asyncua server, points an OpcuaClientManager at it through a
fake PLC debug surface, and exercises the full bidirectional bridge:

  - remote -> PLC: a remote INT node's value lands in a PLC leaf (via the
    prime-on-load read and via subscription on subsequent changes).
  - PLC -> remote: a changed PLC REAL leaf is pushed to a remote node.
  - no-PLC gate: while no program is loaded, neither direction touches state;
    on resume, values are re-primed/re-pushed.

Skipped automatically where asyncua is not installed (e.g. the CI test-env),
so it runs against the opcua_client plugin venv but never breaks the suite.
"""

import asyncio
import os
import struct
import sys

import pytest

asyncua = pytest.importorskip("asyncua")
from asyncua import Server, ua  # noqa: E402

# Make `shared` and the plugin modules importable (mirror the runtime).
_client_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_python_dir = os.path.dirname(_client_dir)
for _p in (_client_dir, _python_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from client import OpcuaClientManager  # noqa: E402
from shared.plugin_config_decode.opcua_client_config_model import (  # noqa: E402
    NodeMapping,
    RemoteEndpointSecurity,
    RemoteServerConfig,
)

ENDPOINT = "opc.tcp://127.0.0.1:48477/openplc/test"


class FakePLC:
    """Minimal stand-in for plugin_runtime_args_t's debug surface.

    Backs a dict of (arr, elem) -> raw bytes and honors the ctypes calling
    convention used by shared.opcua_common.opcua_memory.
    """

    STATUS_OK = 0x7E

    def __init__(self):
        self.mem = {}
        self.loaded = True

    def debug_array_count(self):
        return 1 if self.loaded else 0

    def debug_size(self, arr, elem):
        return len(self.mem.get((arr.value, elem.value), b""))

    def debug_read(self, arr, elem, dest):
        data = self.mem.get((arr.value, elem.value))
        if not data:
            return 0
        for i, b in enumerate(data):
            dest[i] = b
        return len(data)

    def debug_write(self, arr, elem, data_ptr, length):
        n = length.value
        self.mem[(arr.value, elem.value)] = bytes(data_ptr[i] for i in range(n))
        return self.STATUS_OK

    def debug_set(self, arr, elem, forcing, data_ptr, length):
        if forcing.value:
            n = length.value
            self.mem[(arr.value, elem.value)] = bytes(data_ptr[i] for i in range(n))
        return self.STATUS_OK


def _i16(b):
    return struct.unpack("<h", b[:2])[0] if b else None


def _f32(v):
    return struct.pack("<f", v)


async def _scenario():
    # --- local asyncua server -------------------------------------------------
    server = Server()
    await server.init()
    server.set_endpoint(ENDPOINT)
    idx = await server.register_namespace("urn:openplc:test")

    objects = server.nodes.objects
    remote_int = await objects.add_variable(
        ua.NodeId("RemoteInt", idx), "RemoteInt", ua.Variant(42, ua.VariantType.Int16)
    )
    await remote_int.set_writable()
    remote_sp = await objects.add_variable(
        ua.NodeId("RemoteSetpoint", idx), "RemoteSetpoint", ua.Variant(0.0, ua.VariantType.Float)
    )
    await remote_sp.set_writable()

    int_node_id = f"ns={idx};s=RemoteInt"
    sp_node_id = f"ns={idx};s=RemoteSetpoint"

    await server.start()
    try:
        # --- fake PLC + client config -----------------------------------------
        plc = FakePLC()
        plc.mem[(0, 1)] = _f32(7.5)  # PLC value to push to RemoteSetpoint

        cfg = RemoteServerConfig(
            name="TestServer",
            endpoint_url=ENDPOINT,
            security=RemoteEndpointSecurity(),  # None/None/anonymous
            mappings=[
                NodeMapping(int_node_id, 0, 0, "INT", 2, "remote_to_plc", 100),
                NodeMapping(sp_node_id, 0, 1, "REAL", 4, "plc_to_remote", 100),
            ],
        )

        plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        manager = OpcuaClientManager(cfg, plc, plugin_dir)

        stop = {"v": False}
        task = asyncio.create_task(manager.run(lambda: not stop["v"]))
        try:
            # 1) connect + subscribe + prime + first write
            await asyncio.sleep(1.0)
            assert _i16(plc.mem.get((0, 0))) == 42, "remote->PLC prime/subscribe failed"
            assert abs((await remote_sp.read_value()) - 7.5) < 1e-3, "PLC->remote push failed"

            # 2) remote change -> PLC via subscription
            await remote_int.write_value(ua.Variant(99, ua.VariantType.Int16))
            await asyncio.sleep(0.6)
            assert _i16(plc.mem.get((0, 0))) == 99, "subscription update failed"

            # 3) PLC change -> remote via change-detect
            plc.mem[(0, 1)] = _f32(12.25)
            await asyncio.sleep(0.6)
            assert (
                abs((await remote_sp.read_value()) - 12.25) < 1e-3
            ), "PLC->remote change-detect failed"

            # 4) no-PLC gate: nothing moves while unloaded
            plc.loaded = False
            await asyncio.sleep(0.3)
            await remote_int.write_value(ua.Variant(7, ua.VariantType.Int16))
            plc.mem[(0, 1)] = _f32(1.0)
            await asyncio.sleep(0.6)
            assert _i16(plc.mem.get((0, 0))) == 99, "subscription should be gated while no PLC"
            assert (
                abs((await remote_sp.read_value()) - 12.25) < 1e-3
            ), "writer should pause while no PLC"

            # 5) resume: prime re-reads remote, writer re-pushes
            plc.loaded = True
            await asyncio.sleep(0.8)
            assert _i16(plc.mem.get((0, 0))) == 7, "resume prime read failed"
            assert abs((await remote_sp.read_value()) - 1.0) < 1e-3, "resume re-push failed"
        finally:
            stop["v"] = True
            await asyncio.wait_for(task, timeout=5.0)
    finally:
        await server.stop()


def test_client_bidirectional_bridge():
    asyncio.run(_scenario())
