"""
Tests for Modbus TCP gateway support: multiple slaves behind one IP:port.

A Modbus gateway (TCP-to-RTU converter) fronts several serial slaves on one
IP:port, distinguished by slave/unit ID. The runtime must (a) accept multiple
TCP devices that share a host:port as long as their slave IDs differ, and
(b) group them onto a single shared connection (one ModbusBusHandler), routing
each by slave_id.
"""

import pytest

from core.src.drivers.plugins.python.shared.plugin_config_decode.modbus_master_config_model import (
    ModbusMasterConfig,
    ModbusDeviceConfig,
)
from core.src.drivers.plugins.python.modbus_master.modbus_master_plugin import (
    group_tcp_devices_by_endpoint,
)


def _tcp_device(name, host="192.168.1.50", port=502, slave_id=1):
    return ModbusDeviceConfig.from_dict(
        {
            "name": name,
            "protocol": "MODBUS",
            "config": {
                "transport": "tcp",
                "host": host,
                "port": port,
                "slave_id": slave_id,
                "timeout_ms": 1000,
                "io_points": [],
            },
        }
    )


def _config(devices):
    cfg = ModbusMasterConfig()
    cfg.devices = devices
    return cfg


# --- Validation ----------------------------------------------------------

def test_gateway_same_ip_different_slave_ids_is_valid():
    """Multiple TCP devices on one IP:port with distinct slave IDs (a gateway)."""
    cfg = _config([
        _tcp_device("G1", slave_id=1),
        _tcp_device("G2", slave_id=2),
        _tcp_device("G3", slave_id=10),
    ])
    cfg.validate()  # must not raise


def test_same_ip_same_slave_id_is_rejected():
    """Same host:port AND slave_id is a true duplicate -> rejected."""
    cfg = _config([_tcp_device("A", slave_id=5), _tcp_device("B", slave_id=5)])
    with pytest.raises(ValueError, match="slave"):
        cfg.validate()


def test_different_ip_same_slave_id_is_valid():
    """Different gateways may each expose the same slave ID."""
    cfg = _config([
        _tcp_device("A", host="10.0.0.1", slave_id=1),
        _tcp_device("B", host="10.0.0.2", slave_id=1),
    ])
    cfg.validate()  # must not raise


def test_same_ip_different_port_is_valid():
    cfg = _config([
        _tcp_device("A", port=502, slave_id=1),
        _tcp_device("B", port=503, slave_id=1),
    ])
    cfg.validate()  # must not raise


# --- Endpoint grouping ----------------------------------------------------

def test_group_tcp_collapses_same_endpoint():
    """Devices sharing host:port land in one group (one shared connection)."""
    devs = [_tcp_device("G1", slave_id=1), _tcp_device("G2", slave_id=2)]
    groups = group_tcp_devices_by_endpoint(devs)
    assert list(groups.keys()) == ["192.168.1.50:502"]
    info = groups["192.168.1.50:502"]
    assert len(info["devices"]) == 2
    assert info["config"] == {"host": "192.168.1.50", "port": 502, "timeout_ms": 1000}
    assert sorted(d.slave_id for d in info["devices"]) == [1, 2]


def test_group_tcp_separates_distinct_endpoints():
    devs = [
        _tcp_device("A", host="10.0.0.1", port=502),
        _tcp_device("B", host="10.0.0.1", port=503),
        _tcp_device("C", host="10.0.0.2", port=502),
    ]
    groups = group_tcp_devices_by_endpoint(devs)
    assert len(groups) == 3


def test_group_tcp_uses_minimum_timeout():
    devs = [
        _tcp_device("G1", slave_id=1),
        ModbusDeviceConfig.from_dict({
            "name": "G2", "protocol": "MODBUS",
            "config": {"transport": "tcp", "host": "192.168.1.50", "port": 502,
                       "slave_id": 2, "timeout_ms": 250, "io_points": []},
        }),
    ]
    groups = group_tcp_devices_by_endpoint(devs)
    assert groups["192.168.1.50:502"]["config"]["timeout_ms"] == 250
