# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# tests/conftest.py
import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace

from core.src.drivers.plugins.python.modbus_master.modbus_master_plugin import ModbusSlaveDevice  # adjust import


MODULE = "core.src.drivers.plugins.python.modbus_master.modbus_master_plugin"

@pytest.fixture
def fake_device_config():
    """Fake device configuration with minimal required fields."""
    return SimpleNamespace(
        name="TestDevice",
        host="127.0.0.1",
        port=1502,
        timeout_ms=2000,
        cycle_time_ms=100,
        io_points=[]
    )

@pytest.fixture
def fake_sba():
    """Fake SafeBufferAccess object with read/write mocks."""
    sba = MagicMock()
    # Return only values (no status tuple)
    for prefix in ["bool", "byte", "int", "dint", "lint"]:
        for direction in ["input", "output", "memory"]:
            setattr(sba, f"write_{prefix}_{direction}", MagicMock(return_value=(True, "OK")))
            setattr(sba, f"read_{prefix}_{direction}", MagicMock(return_value=(123, "OK")))
    sba.acquire_mutex.return_value = True
    sba.release_mutex.return_value = None
    return sba

@pytest.fixture
def fake_modbus_client(monkeypatch):
    """Patch ModbusTcpClient to avoid real network activity."""
    mock_client = MagicMock()
    mock_client.connected = True
    mock_client.connect.return_value = True
    mock_client.close.return_value = None
    monkeypatch.setattr(f"{MODULE}.ModbusTcpClient", lambda *a, **kw: mock_client)
    return mock_client

@pytest.fixture
def modbus_slave(fake_device_config, fake_sba):
    """Fixture returning a ModbusSlaveDevice instance."""
    return ModbusSlaveDevice(fake_device_config, fake_sba)
