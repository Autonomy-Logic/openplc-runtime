"""
Contract-version gate + canonical per-leaf size for the OPC-UA config.

These exercise only the pure config model (shared/plugin_config_decode),
so they need no asyncua / OPC-UA server and run on any machine.

The gate is what lets a NEW runtime safely refuse an OLD editor's
conf/opcua.json (which omits per-variable byte sizes): OpcuaConfig.from_dict
raises, load_config() turns that into a None return, and the plugin simply
doesn't start the OPC-UA server while the rest of the PLC keeps running.
"""

import os
import sys

import pytest

# core/src/drivers/plugins/python on sys.path so `shared.…` resolves the
# same way the runtime sets it up (see opcua/config.py).
_PLUGINS_PYTHON = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "core", "src", "drivers", "plugins", "python")
)
if _PLUGINS_PYTHON not in sys.path:
    sys.path.insert(0, _PLUGINS_PYTHON)

from shared.plugin_config_decode.opcua_config_model import (  # noqa: E402
    OPCUA_CONFIG_MIN_FORMAT_VERSION,
    OpcuaConfig,
)


def _config_dict(format_version=OPCUA_CONFIG_MIN_FORMAT_VERSION, with_size=True):
    var = {
        "node_id": "X",
        "browse_name": "X",
        "display_name": "X",
        "datatype": "DINT",
        "description": "",
        "arr": 0,
        "elem": 1,
        "permissions": {"viewer": "r", "operator": "r", "engineer": "rw"},
    }
    if with_size:
        var["size"] = 4
    cfg = {
        "server": {
            "name": "s",
            "application_uri": "u",
            "product_uri": "p",
            "endpoint_url": "opc.tcp://x",
            "security_profiles": [
                {
                    "name": "n",
                    "enabled": True,
                    "security_policy": "None",
                    "security_mode": "None",
                    "auth_methods": ["Anonymous"],
                }
            ],
        },
        "security": {"server_certificate_strategy": "auto_self_signed", "trusted_client_certificates": []},
        "users": [],
        "cycle_time_ms": 100,
        "address_space": {"namespace_uri": "urn:x", "variables": [var], "structures": [], "arrays": []},
    }
    if format_version is not None:
        cfg["format_version"] = format_version
    return cfg


def test_current_format_parses_and_keeps_canonical_size():
    cfg = OpcuaConfig.from_dict(_config_dict())
    assert cfg.format_version == OPCUA_CONFIG_MIN_FORMAT_VERSION
    assert cfg.address_space.variables[0].size == 4


def test_missing_format_version_is_rejected():
    # An older editor's config has no format_version — refuse it.
    with pytest.raises(ValueError) as exc:
        OpcuaConfig.from_dict(_config_dict(format_version=None, with_size=False))
    assert "format_version" in str(exc.value)


def test_older_format_version_is_rejected():
    with pytest.raises(ValueError):
        OpcuaConfig.from_dict(_config_dict(format_version=OPCUA_CONFIG_MIN_FORMAT_VERSION - 1))


def test_leaf_missing_size_is_rejected():
    # A v2 config whose leaf omits the canonical size is malformed.
    with pytest.raises(ValueError):
        OpcuaConfig.from_dict(_config_dict(with_size=False))


def test_array_and_struct_leaf_sizes_round_trip():
    cfg_dict = _config_dict()
    cfg_dict["address_space"]["arrays"] = [
        {
            "node_id": "A",
            "browse_name": "A",
            "display_name": "A",
            "datatype": "REAL",
            "length": 3,
            "arr": 0,
            "elem": 10,
            "size": 4,
            "permissions": {"viewer": "r", "operator": "r", "engineer": "rw"},
        }
    ]
    cfg_dict["address_space"]["structures"] = [
        {
            "node_id": "S",
            "browse_name": "S",
            "display_name": "S",
            "description": "",
            "fields": [
                {
                    "name": "F",
                    "datatype": "LREAL",
                    "arr": 0,
                    "elem": 20,
                    "size": 8,
                    "permissions": {"viewer": "r", "operator": "r", "engineer": "rw"},
                }
            ],
        }
    ]
    cfg = OpcuaConfig.from_dict(cfg_dict)
    assert cfg.address_space.arrays[0].size == 4
    assert cfg.address_space.structures[0].fields[0].size == 8
