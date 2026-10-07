# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

import pytest
from unittest.mock import patch, MagicMock

import core.src.drivers.plugins.python.modbus_master.modbus_master_plugin as plugin


MODULE = "core.src.drivers.plugins.python.modbus_master.modbus_master_plugin"

@pytest.fixture(autouse=True)
def reset_globals():
    """Ensure clean globals before and after each test."""
    plugin.runtime_args = None
    plugin.modbus_master_config = None
    plugin.safe_buffer_accessor = None
    plugin.slave_threads = []
    yield
    plugin.runtime_args = None
    plugin.modbus_master_config = None
    plugin.safe_buffer_accessor = None
    plugin.slave_threads = []


# ---------------------------------------------------------------------
# INIT TESTS
# ---------------------------------------------------------------------
@patch(f"{MODULE}.safe_extract_runtime_args_from_capsule")
@patch(f"{MODULE}.SafeBufferAccess")
@patch(f"{MODULE}.ModbusMasterConfig")
def test_init_success(mock_cfg, mock_buf, mock_extract):
    # --- mock extract ---
    mock_extract.return_value = ({"arg": 123}, None)

    # --- mock SafeBufferAccess ---
    mock_accessor = MagicMock()
    mock_accessor.is_valid = True
    mock_accessor.get_config_path.return_value = ("/fake/config.json", None)
    mock_buf.return_value = mock_accessor

    # --- mock ModbusMasterConfig ---
    mock_cfg_instance = MagicMock()
    mock_cfg_instance.devices = ["dev1"]
    mock_cfg.return_value = mock_cfg_instance

    ok = plugin.init("capsule")

    assert ok is True
    mock_extract.assert_called_once()
    mock_buf.assert_called_once()
    mock_cfg_instance.import_config_from_file.assert_called_with("/fake/config.json")
    mock_cfg_instance.validate.assert_called_once()
    assert plugin.safe_buffer_accessor == mock_accessor
    assert plugin.modbus_master_config == mock_cfg_instance


@patch(f"{MODULE}.safe_extract_runtime_args_from_capsule", return_value=(None, "bad"))
def test_init_fail_on_capsule(mock_extract):
    ok = plugin.init("capsule")
    assert ok is False
    assert plugin.runtime_args is None


# ---------------------------------------------------------------------
# START LOOP TESTS
# ---------------------------------------------------------------------
@patch(f"{MODULE}.ModbusSlaveDevice")
def test_start_loop_success(mock_device_class):
    # Prepare mock config with 2 fake devices
    dev1 = MagicMock(name="Device1")
    dev1.name = "A"
    dev1.host = "127.0.0.1"
    dev1.port = 1502
    dev2 = MagicMock(name="Device2")
    dev2.name = "B"
    dev2.host = "127.0.0.1"
    dev2.port = 1503

    plugin.modbus_master_config = MagicMock()
    plugin.modbus_master_config.devices = [dev1, dev2]
    plugin.safe_buffer_accessor = MagicMock()

    # Return a new mock per call
    fake_threads = [MagicMock(name="Thread1"), MagicMock(name="Thread2")]
    mock_device_class.side_effect = fake_threads  # one per call

    ok = plugin.start_loop()

    assert ok is True
    assert mock_device_class.call_count == 2
    assert len(plugin.slave_threads) == 2

    for t in fake_threads:
        t.start.assert_called_once()



def test_start_loop_without_init():
    plugin.modbus_master_config = None
    plugin.safe_buffer_accessor = None
    ok = plugin.start_loop()
    assert ok is False


# ---------------------------------------------------------------------
# STOP LOOP TESTS
# ---------------------------------------------------------------------
def test_stop_loop_success():
    t1 = MagicMock(name="T1")
    t2 = MagicMock(name="T2")
    plugin.slave_threads = [t1, t2]

    ok = plugin.stop_loop()

    assert ok is True
    for t in [t1, t2]:
        t.stop.assert_called_once()
        t.join.assert_called_once()


def test_stop_loop_no_threads():
    plugin.slave_threads = []
    ok = plugin.stop_loop()
    assert ok is True


# ---------------------------------------------------------------------
# CLEANUP TESTS
# ---------------------------------------------------------------------
@patch(f"{MODULE}.stop_loop")
def test_cleanup_success(mock_stop):
    plugin.runtime_args = {"arg": 1}
    plugin.modbus_master_config = MagicMock()
    plugin.safe_buffer_accessor = MagicMock()
    plugin.slave_threads = [MagicMock()]

    ok = plugin.cleanup()

    assert ok is True
    mock_stop.assert_called_once()
    assert plugin.runtime_args is None
    assert plugin.modbus_master_config is None
    assert plugin.safe_buffer_accessor is None
    assert plugin.slave_threads == []
