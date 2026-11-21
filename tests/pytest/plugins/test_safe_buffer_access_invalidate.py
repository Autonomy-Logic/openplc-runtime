# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

import os
import sys
from typing import List

import pytest

_PLUGINS_PYTHON = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "core", "src", "drivers", "plugins", "python"
    )
)
if _PLUGINS_PYTHON not in sys.path:
    sys.path.insert(0, _PLUGINS_PYTHON)

from shared.safe_buffer_access_refactored import SafeBufferAccess  # noqa: E402


class _Touches:
    """Attribute names read on a spy runtime args while recording is on."""

    def __init__(self):
        self.recording = False
        self.names: List[str] = []


def _answer_zero(*args: object) -> int:
    """Runtime function stand-in that accepts any call and returns 0."""
    return 0


class _SpyRuntimeArgs:
    """Runtime args stand-in that records every attribute read while its recorder is on."""

    buffer_size = 8
    bits_per_buffer = 8
    plugin_specific_config_file_path = b"/nonexistent/plugin_config.json"

    def __init__(self, touches: _Touches):
        object.__setattr__(self, "_touches", touches)

    def __getattribute__(self, name: str) -> object:
        touches = object.__getattribute__(self, "_touches")
        if touches.recording and not name.startswith("__"):
            touches.names.append(name)
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> object:
        return _answer_zero

    def validate_pointers(self) -> tuple:
        return (True, "")

    def image_lock(self) -> None:
        return None

    def image_unlock(self) -> None:
        return None


OPERATIONS = [
    pytest.param(lambda a: a.read_bool_input(0, 0), id="read_bool_input"),
    pytest.param(lambda a: a.read_int_output(0), id="read_int_output"),
    pytest.param(lambda a: a.read_lint_memory(0), id="read_lint_memory"),
    pytest.param(lambda a: a.write_bool_output(0, 0, True), id="write_bool_output"),
    pytest.param(lambda a: a.write_dint_memory(0, 7), id="write_dint_memory"),
    pytest.param(lambda a: a.acquire_mutex(), id="acquire_mutex"),
    pytest.param(lambda a: a.release_mutex(), id="release_mutex"),
    pytest.param(lambda a: a.batch_read_values([("int_input", 0)]), id="batch_read_values"),
    pytest.param(lambda a: a.batch_write_values([("int_output", 0, 7)]), id="batch_write_values"),
    pytest.param(
        lambda a: a.batch_mixed_operations([("int_input", 0)], [("int_output", 0, 7)]),
        id="batch_mixed_operations",
    ),
    pytest.param(lambda a: a.get_config_path(), id="get_config_path"),
    pytest.param(lambda a: a.get_config_file_args_as_map(), id="get_config_file_args_as_map"),
]


@pytest.fixture
def touches():
    return _Touches()


@pytest.fixture
def accessor(touches):
    return SafeBufferAccess(_SpyRuntimeArgs(touches))


def test_invalidate_marks_the_accessor_invalid(accessor):
    accessor.invalidate()

    assert accessor.is_valid is False


def test_invalidate_says_why_the_accessor_is_invalid(accessor):
    accessor.invalidate()

    assert accessor.error_msg


@pytest.mark.parametrize("operation", OPERATIONS)
def test_an_operation_after_invalidate_does_not_reach_the_runtime_args(
    accessor, touches, operation
):
    touches.recording = True
    operation(accessor)
    assert touches.names, "the spy does not observe this operation on a valid accessor"

    accessor.invalidate()
    touches.names.clear()
    operation(accessor)

    assert touches.names == []


@pytest.mark.parametrize("operation", OPERATIONS)
def test_an_operation_after_invalidate_returns_a_failure(accessor, operation):
    accessor.invalidate()

    outcome, message = operation(accessor)

    assert not outcome
    assert message and message != "Success"


def test_a_second_invalidate_is_harmless(accessor, touches):
    accessor.invalidate()
    accessor.invalidate()

    touches.recording = True
    accessor.read_int_output(0)

    assert accessor.is_valid is False
    assert touches.names == []
