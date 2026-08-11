import pytest
from .modbus_master_config_model import (
    ERROR_HANDLING_KEEP_LAST,
    ERROR_HANDLING_SET_TO_ZERO,
    parse_iec_address,
    ModbusIoPointConfig,
    ModbusDeviceConfig,
    ModbusMasterConfig,
)

# ---------------------------------------------------------------------
# TEST IECAddress parsing
# ---------------------------------------------------------------------

def test_parse_iec_address_bit():
    addr = parse_iec_address("%IX0.3")
    assert addr.area == "I"
    assert addr.size == "X"
    assert addr.byte == 0
    assert addr.bit == 3
    assert addr.index_bits == 3
    assert addr.width_bits == 1

def test_parse_iec_address_word():
    addr = parse_iec_address("%MW10")
    assert addr.area == "M"
    assert addr.size == "W"
    assert addr.byte == 10
    assert addr.index_bytes == 20
    assert addr.width_bits == 16

def test_parse_iec_address_invalid():
    with pytest.raises(ValueError):
        parse_iec_address("%QZ0")  # Invalid type


# ---------------------------------------------------------------------
# TEST ModbusIoPointConfig
# ---------------------------------------------------------------------

def test_modbus_io_point_from_dict():
    data = {
        "fc": 3,
        "offset": "40001",
        "iec_location": "%IW0",
        "len": 2
    }
    point = ModbusIoPointConfig.from_dict(data)
    assert point.fc == 3
    assert point.length == 2
    assert point.iec_location.area == "I"
    d = point.to_dict()
    assert d["fc"] == 3
    assert d["iec_location"].startswith("%I")

def test_modbus_io_point_error_handling_defaults_to_keep_last():
    # Configs written before the field existed must keep behaving as they did:
    # the runtime left the last value in the buffer when a read failed.
    point = ModbusIoPointConfig.from_dict(
        {"fc": 3, "offset": "40001", "iec_location": "%IW0", "len": 2}
    )
    assert point.error_handling == ERROR_HANDLING_KEEP_LAST


def test_modbus_io_point_parses_set_to_zero():
    point = ModbusIoPointConfig.from_dict(
        {
            "fc": 3,
            "offset": "40001",
            "iec_location": "%IW0",
            "len": 2,
            "error_handling": "set-to-zero",
        }
    )
    assert point.error_handling == ERROR_HANDLING_SET_TO_ZERO
    assert point.to_dict()["error_handling"] == "set-to-zero"


def test_modbus_io_point_unknown_error_handling_falls_back():
    # A config from a newer editor must not take the whole device offline over
    # one unrecognised string.
    point = ModbusIoPointConfig.from_dict(
        {
            "fc": 3,
            "offset": "40001",
            "iec_location": "%IW0",
            "len": 2,
            "error_handling": "explode",
        }
    )
    assert point.error_handling == ERROR_HANDLING_KEEP_LAST


def test_modbus_io_point_missing_field():
    data = {
        "offset": "40001",
        "iec_location": "%IW0",
        "len": 2
    }
    with pytest.raises(ValueError):
        ModbusIoPointConfig.from_dict(data)


# ---------------------------------------------------------------------
# TEST ModbusDeviceConfig
# ---------------------------------------------------------------------

def test_device_from_dict_and_validate():
    data = {
        "name": "Dev1",
        "protocol": "MODBUS",
        "config": {
            "type": "SLAVE",
            "host": "127.0.0.1",
            "port": 502,
            "cycle_time_ms": 100,
            "timeout_ms": 100,
            "io_points": [
                {"fc": 3, "offset": "40001", "iec_location": "%IW0", "len": 2}
            ]
        }
    }
    dev = ModbusDeviceConfig.from_dict(data)
    assert dev.name == "Dev1"
    dev.validate()  # should not raise

def test_device_invalid_fc():
    dev = ModbusDeviceConfig()
    dev.name = "Invalid"
    dev.protocol = "MODBUS"
    dev.io_points = [
        ModbusIoPointConfig(fc=-1, offset="40001", iec_location="%IW0", length=1)
    ]
    with pytest.raises(ValueError):
        dev.validate()


# ---------------------------------------------------------------------
# TEST ModbusMasterConfig
# ---------------------------------------------------------------------

def test_master_import_and_validate(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_data = [
        {
            "name": "Dev1",
            "protocol": "MODBUS",
            "config": {
                "type": "SLAVE",
                "host": "127.0.0.1",
                "port": 502,
                "cycle_time_ms": 100,
                "timeout_ms": 100,
                "io_points": [
                    {"fc": 3, "offset": "40001", "iec_location": "%IW0", "len": 2}
                ]
            }
        }
    ]
    import json
    cfg_path.write_text(json.dumps(cfg_data))

    master = ModbusMasterConfig()
    master.import_config_from_file(cfg_path)
    assert len(master.devices) == 1
    master.validate()  # should not raise