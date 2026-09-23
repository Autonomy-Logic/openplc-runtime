"""
Parse-time behaviour of SecurityProfile.anonymous_role.

An anonymous session's role decides what an unauthenticated client may do, so
the value is validated where every other config field is — at parse — instead
of degrading silently per session. Case and whitespace are normalized so an
editor/hand-edited "Engineer" is accepted; anything not a known role is rejected.
"""

import os
import sys

import pytest

_PLUGINS_PYTHON = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "core", "src", "drivers", "plugins", "python")
)
if _PLUGINS_PYTHON not in sys.path:
    sys.path.insert(0, _PLUGINS_PYTHON)

from shared.plugin_config_decode.opcua_config_model import (  # noqa: E402
    SecurityProfile,
    VALID_ROLES,
    normalize_role,
)


def _profile(anon):
    d = {
        "name": "insecure",
        "enabled": True,
        "security_policy": "None",
        "security_mode": "None",
        "auth_methods": ["Anonymous"],
    }
    if anon is not _MISSING:
        d["anonymous_role"] = anon
    return SecurityProfile.from_dict(d)


_MISSING = object()


def test_defaults_to_viewer_when_absent():
    assert _profile(_MISSING).anonymous_role == "viewer"


def test_none_and_empty_default_to_viewer():
    assert _profile(None).anonymous_role == "viewer"
    assert _profile("").anonymous_role == "viewer"


@pytest.mark.parametrize("value,expected", [
    ("viewer", "viewer"),
    ("operator", "operator"),
    ("engineer", "engineer"),
    ("Engineer", "engineer"),
    ("  ENGINEER  ", "engineer"),
    ("Operator", "operator"),
])
def test_valid_roles_are_normalized(value, expected):
    assert _profile(value).anonymous_role == expected


@pytest.mark.parametrize("bad", ["admin", "root", "enginer", "engineer2", 3, True])
def test_invalid_role_raises_at_parse(bad):
    with pytest.raises(ValueError, match="[Ii]nvalid anonymous_role"):
        _profile(bad)


def test_normalize_role_helper():
    assert normalize_role("  Engineer ") == "engineer"
    assert normalize_role("nonsense") == "viewer"  # least privilege fallback
    assert set(VALID_ROLES) == {"viewer", "operator", "engineer"}
