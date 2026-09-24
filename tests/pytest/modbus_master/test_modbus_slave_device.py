# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# tests/test_modbus_slave_device.py
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, call

from conftest import fake_sba

def test_get_sba_access_details_boolean(modbus_slave):
    iec_addr = SimpleNamespace(area="I", size="X", index_bytes=0, bit=3)
    result = modbus_slave._get_sba_access_details(iec_addr)
    assert result["buffer_type_str"] == "bool_input"
    assert result["buffer_idx"] == 0
    assert result["bit_idx"] == 3
    assert result["is_boolean"]

def test_get_sba_access_details_word_output(modbus_slave):
    iec_addr = SimpleNamespace(area="Q", size="W", index_bytes=4, bit=None)
    result = modbus_slave._get_sba_access_details(iec_addr, is_write_op=True)
    assert result["buffer_type_str"] == "int_output"
    assert result["buffer_idx"] == 2
    assert result["element_size_bytes"] == 2

def test_connect_with_retry_success(modbus_slave, fake_modbus_client):
    result = modbus_slave._connect_with_retry()
    assert result is True
    assert modbus_slave.is_connected is True
    fake_modbus_client.connect.assert_called()

def test_connect_with_retry_stops(monkeypatch, modbus_slave, fake_modbus_client):
    modbus_slave._stop_event.set()  # simulate stop before start
    result = modbus_slave._connect_with_retry()
    assert result is False

def test_update_iec_buffer_from_modbus_data_boolean(modbus_slave, fake_sba):
    iec_addr = SimpleNamespace(area="I", size="X", index_bytes=0, bit=0)
    data = [True, False, True]
    modbus_slave._update_iec_buffer_from_modbus_data(iec_addr, data, len(data))
    fake_sba.write_bool_input.assert_called()

def test_update_iec_buffer_from_modbus_data_word(modbus_slave, fake_sba):
    iec_addr = SimpleNamespace(area="Q", size="W", index_bytes=0, bit=None)
    data = [10, 20]
    modbus_slave._update_iec_buffer_from_modbus_data(iec_addr, data, len(data))
    print("fake_sba.write_int_output.call_args_list:", fake_sba.write_int_output.call_args_list)
    assert fake_sba.write_int_output.call_args_list == [
        call(0, 10, thread_safe=False),
        call(1, 20, thread_safe=False),
    ]

def test_read_data_for_modbus_write_boolean(modbus_slave, fake_sba):
    modbus_slave.sba = fake_sba  # <-- Inject here
    fake_sba.read_bool_output.side_effect = lambda *a, **kw: print("READ_BOOL_OUTPUT CALLED") or (123, "Success")
    iec_addr = SimpleNamespace(area="Q", size="X", index_bytes=0, bit=0)
    values = modbus_slave._read_data_for_modbus_write(iec_addr, 3)
    assert all(v == 123 for v in values)
    fake_sba.read_bool_output.assert_called()


def test_read_data_for_modbus_write_word(modbus_slave, fake_sba):
    modbus_slave.sba = fake_sba  # <-- Inject here
    fake_sba.read_int_output.side_effect = lambda *a, **kw: print("READ_INT_OUTPUT CALLED") or (123, "Success")
    iec_addr = SimpleNamespace(area="Q", size="W", index_bytes=0, bit=None)
    values = modbus_slave._read_data_for_modbus_write(iec_addr, 2)
    assert values == [123, 123]
    fake_sba.read_int_output.assert_called()

def test_ensure_connection_uses_existing(modbus_slave, fake_modbus_client):
    modbus_slave.client = fake_modbus_client
    modbus_slave.client.connected = True
    assert modbus_slave._ensure_connection() is True

def test_ensure_connection_reconnects(modbus_slave, fake_modbus_client):
    modbus_slave.client = fake_modbus_client
    modbus_slave.client.connected = False
    assert modbus_slave._ensure_connection() is True
