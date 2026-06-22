"""Unit tests for the OPC-UA Client configuration model (pure Python)."""

import json
import os
import sys

import pytest

_python_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _python_dir not in sys.path:
    sys.path.insert(0, _python_dir)

from shared.plugin_config_decode.opcua_client_config_model import (  # noqa: E402
    OPCUA_CLIENT_CONFIG_MIN_FORMAT_VERSION,
    OpcuaClientMasterConfig,
)


def _good():
    return [
        {
            "name": "opcua_client",
            "protocol": "OPC-UA-Client",
            "config": {
                "format_version": OPCUA_CLIENT_CONFIG_MIN_FORMAT_VERSION,
                "servers": [
                    {
                        "name": "RemotePLC_A",
                        "endpoint_url": "opc.tcp://192.168.0.50:4840/x",
                        "security": {
                            "security_policy": "Basic256Sha256",
                            "security_mode": "SignAndEncrypt",
                            "auth_mode": "username",
                            "username": "op",
                            "password": "p",
                        },
                        "session_timeout_ms": 60000,
                        "reconnect": True,
                        "mappings": [
                            {
                                "remote_node_id": "ns=2;i=5",
                                "arr": 0,
                                "elem": 3,
                                "datatype": "INT",
                                "size": 2,
                                "direction": "remote_to_plc",
                                "cycle_time_ms": 100,
                            },
                            {
                                "remote_node_id": "ns=2;s=Setpoint",
                                "arr": 0,
                                "elem": 7,
                                "datatype": "REAL",
                                "size": 4,
                                "direction": "plc_to_remote",
                                "cycle_time_ms": 200,
                            },
                        ],
                    }
                ],
            },
        }
    ]


def _load(tmp_path, obj):
    p = tmp_path / "opcua_client.json"
    p.write_text(json.dumps(obj))
    cfg = OpcuaClientMasterConfig()
    cfg.import_config_from_file(str(p))
    cfg.validate()
    return cfg


def test_valid_config(tmp_path):
    cfg = _load(tmp_path, _good())
    assert len(cfg.plugins) == 1
    server = cfg.plugins[0].config.servers[0]
    assert server.name == "RemotePLC_A"
    assert len(server.mappings) == 2
    assert server.security.auth_mode == "username"
    assert cfg.plugins[0].config.format_version == OPCUA_CLIENT_CONFIG_MIN_FORMAT_VERSION


def test_missing_format_version_rejected(tmp_path):
    obj = _good()
    del obj[0]["config"]["format_version"]
    with pytest.raises(ValueError):
        _load(tmp_path, obj)


def test_old_format_version_rejected(tmp_path):
    obj = _good()
    obj[0]["config"]["format_version"] = OPCUA_CLIENT_CONFIG_MIN_FORMAT_VERSION - 1
    with pytest.raises(ValueError):
        _load(tmp_path, obj)


def test_leaf_missing_size_rejected(tmp_path):
    obj = _good()
    del obj[0]["config"]["servers"][0]["mappings"][0]["size"]
    with pytest.raises(ValueError):
        _load(tmp_path, obj)


def test_invalid_datatype_rejected(tmp_path):
    obj = _good()
    obj[0]["config"]["servers"][0]["mappings"][0]["datatype"] = "FOO"
    with pytest.raises(ValueError):
        _load(tmp_path, obj)


def test_invalid_direction_rejected(tmp_path):
    obj = _good()
    obj[0]["config"]["servers"][0]["mappings"][0]["direction"] = "sideways"
    with pytest.raises(ValueError):
        _load(tmp_path, obj)


def test_duplicate_remote_to_plc_addr_rejected(tmp_path):
    obj = _good()
    obj[0]["config"]["servers"][0]["mappings"][1] = {
        "remote_node_id": "ns=2;i=9",
        "arr": 0,
        "elem": 3,
        "datatype": "INT",
        "size": 2,
        "direction": "remote_to_plc",
        "cycle_time_ms": 100,
    }
    with pytest.raises(ValueError):
        _load(tmp_path, obj)


def test_username_auth_requires_username(tmp_path):
    obj = _good()
    obj[0]["config"]["servers"][0]["security"]["username"] = None
    with pytest.raises(ValueError):
        _load(tmp_path, obj)


def test_certificate_auth_requires_cert(tmp_path):
    obj = _good()
    sec = obj[0]["config"]["servers"][0]["security"]
    sec["auth_mode"] = "certificate"
    sec["client_cert_pem"] = None
    with pytest.raises(ValueError):
        _load(tmp_path, obj)


def test_wrong_protocol_rejected(tmp_path):
    obj = _good()
    obj[0]["protocol"] = "OPC-UA"
    with pytest.raises(ValueError):
        _load(tmp_path, obj)


def test_no_servers_rejected(tmp_path):
    obj = _good()
    obj[0]["config"]["servers"] = []
    with pytest.raises(ValueError):
        _load(tmp_path, obj)


def test_plc_to_remote_addr_may_repeat(tmp_path):
    """Two plc_to_remote mappings sharing a local leaf is allowed (one PLC
    value fanned out to multiple remote nodes)."""
    obj = _good()
    obj[0]["config"]["servers"][0]["mappings"] = [
        {
            "remote_node_id": "ns=2;s=A",
            "arr": 0,
            "elem": 7,
            "datatype": "REAL",
            "size": 4,
            "direction": "plc_to_remote",
            "cycle_time_ms": 100,
        },
        {
            "remote_node_id": "ns=2;s=B",
            "arr": 0,
            "elem": 7,
            "datatype": "REAL",
            "size": 4,
            "direction": "plc_to_remote",
            "cycle_time_ms": 100,
        },
    ]
    cfg = _load(tmp_path, obj)  # should not raise
    assert len(cfg.plugins[0].config.servers[0].mappings) == 2
