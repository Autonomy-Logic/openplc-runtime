"""
Tests for the Modbus master's "set to zero" error handling.

Regression coverage for openplc-editor#691: a read that fails used to be
skipped outright, so the point's last good value stayed in the IEC buffer for
as long as the device was unreachable — the editor's per-group "Set to zero"
option had no effect anywhere in the stack.

The behaviour lives in two pure helpers plus one queueing function, so these
tests need no threads, no sockets and no PLC: they assert the payload that
gets handed to the (already covered) buffer-write path.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_plugin_dir = Path(__file__).parent.parent.parent.parent / "core" / "src" / "drivers" / "plugins" / "python"
sys.path.insert(0, str(_plugin_dir / "modbus_master"))
sys.path.insert(0, str(_plugin_dir))

from modbus_master_utils import (
    get_read_count_for_io_point,
    get_zero_payload_for_io_point,
)
from shared.plugin_config_decode.modbus_master_config_model import (
    ERROR_HANDLING_KEEP_LAST,
    ERROR_HANDLING_SET_TO_ZERO,
)


def make_point(fc: int, length: int = 4, iec_size: str = "W", error_handling: str = ERROR_HANDLING_KEEP_LAST):
    """Minimal stand-in for ModbusIoPointConfig — only what the helpers read."""
    return SimpleNamespace(
        fc=fc,
        length=length,
        iec_location=SimpleNamespace(size=iec_size, area="M", byte=0, bit=None),
        error_handling=error_handling,
    )


class TestReadCount:
    """The request size and the zero-fill size must come from one place."""

    @pytest.mark.parametrize("fc", [1, 2])
    def test_coils_map_one_to_one(self, fc):
        assert get_read_count_for_io_point(make_point(fc, length=6, iec_size="X")) == 6

    @pytest.mark.parametrize(
        "iec_size,registers_per_element",
        [("B", 1), ("W", 1), ("D", 2), ("L", 4)],
    )
    def test_registers_scale_with_element_width(self, iec_size, registers_per_element):
        point = make_point(3, length=3, iec_size=iec_size)
        assert get_read_count_for_io_point(point) == 3 * registers_per_element


class TestZeroPayload:
    """The zero-fill has to look like the response it replaces."""

    @pytest.mark.parametrize("fc", [1, 2])
    def test_boolean_reads_get_false(self, fc):
        payload = get_zero_payload_for_io_point(make_point(fc, length=3, iec_size="X"))
        assert payload == [False, False, False]

    def test_register_reads_get_integer_zeros(self):
        payload = get_zero_payload_for_io_point(make_point(3, length=2, iec_size="W"))
        assert payload == [0, 0]

    def test_wide_registers_get_one_zero_per_register(self):
        # A %MD point is two registers per element, so two elements need four.
        payload = get_zero_payload_for_io_point(make_point(4, length=2, iec_size="D"))
        assert payload == [0, 0, 0, 0]


class TestQueueZeroFillOnFailure:
    """
    What the read loop does with a failed read.

    Imported lazily: the plugin module pulls in pymodbus, and these are the
    only tests here that need it.
    """

    @staticmethod
    def _queue():
        from modbus_master_plugin import queue_zero_fill_on_failure

        return queue_zero_fill_on_failure

    def test_set_to_zero_queues_a_zeroed_write(self):
        point = make_point(3, length=2, iec_size="W", error_handling=ERROR_HANDLING_SET_TO_ZERO)
        queued = []

        assert self._queue()(point, queued) is True
        assert len(queued) == 1
        iec_location, payload, length = queued[0]
        assert iec_location is point.iec_location
        assert payload == [0, 0]
        assert length == 2

    def test_keep_last_value_queues_nothing(self):
        point = make_point(3, error_handling=ERROR_HANDLING_KEEP_LAST)
        queued = []

        assert self._queue()(point, queued) is False
        assert queued == []

    def test_point_without_the_field_keeps_last_value(self):
        # Defensive: a point object from an older config path has no attribute
        # at all, and must not start zeroing buffers.
        point = SimpleNamespace(
            fc=3, length=2, iec_location=SimpleNamespace(size="W", area="M", byte=0, bit=None)
        )
        queued = []

        assert self._queue()(point, queued) is False
        assert queued == []

    def test_boolean_point_queues_false_values(self):
        point = make_point(1, length=2, iec_size="X", error_handling=ERROR_HANDLING_SET_TO_ZERO)
        queued = []

        self._queue()(point, queued)
        assert queued[0][1] == [False, False]
