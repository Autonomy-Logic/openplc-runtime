# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Binding leaves to the loaded program and sampling them under the image lock."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import replace

import pytest
from contract.runtime_config import ConfigError, Leaf, decode_runtime_config
from mqtt_reader import reader as reader_module
from mqtt_reader.reader import LOCK_WINDOW_LEAVES, LeafReader, bind_leaves
from program_fakes import FIXTURES, FakeProgram

ALL_TYPES = FIXTURES / "valid" / "sparkplug-client-certificate-all-types.json"


def _leaves() -> tuple[Leaf, ...]:
    doc = json.loads(ALL_TYPES.read_text(encoding="utf-8"))
    doc["auth"] = {"method": "anonymous"}
    return decode_runtime_config(json.dumps(doc).encode(), None).leaves


def _bound(program: FakeProgram, window: int = LOCK_WINDOW_LEAVES) -> LeafReader:
    reader = bind_leaves(program, _leaves(), window)
    assert isinstance(reader, LeafReader)
    return reader


def _many_leaves(count: int) -> tuple[Leaf, ...]:
    base = _leaves()[5]
    return tuple(replace(base, arr=1, elem=i, debug_path=f"P{i}") for i in range(count))


def test_bind_checks_every_leaf_under_the_lock() -> None:
    program = FakeProgram.from_debug_map()
    reader = _bound(program)
    assert reader.leaves == _leaves()
    assert program.calls_outside_lock == 0 and program.depth == 0


@pytest.mark.parametrize(
    ("mutate", "code", "pointer"),
    [
        (lambda p: setattr(p, "loaded", False), "config_program_mismatch", "/program/md5"),
        (lambda p: p.counts.clear(), "config_program_mismatch", "/program/md5"),
        (lambda p: p.counts.__setitem__(0, 0), "config_address_invalid", "/leaves/0/address"),
        (lambda p: p.counts.__setitem__(0, 20), "config_address_invalid", "/leaves/20/address"),
        (lambda p: p.sizes.__setitem__((0, 19), 126), "config_address_invalid", "/leaves/19/size"),
        (lambda p: p.sizes.__setitem__((0, 7), 4), "config_address_invalid", "/leaves/7/size"),
        (lambda p: p.sizes.pop((0, 0)), "config_address_invalid", "/leaves/0/size"),
    ],
)
def test_bind_rejects_any_mismatch_with_the_loaded_program(
    mutate: object, code: str, pointer: str
) -> None:
    program = FakeProgram.from_debug_map()
    assert callable(mutate)
    mutate(program)
    result = bind_leaves(program, _leaves())
    assert isinstance(result, ConfigError)
    assert (result.code, result.pointer, result.layer) == (code, pointer, "semantic")
    assert program.depth == 0
    assert program.windows == 1


def test_bind_with_arr_beyond_the_program_never_asks_its_element_count() -> None:
    program = FakeProgram.from_debug_map()
    leaves = (replace(_leaves()[0], arr=7),)
    result = bind_leaves(program, leaves)
    assert isinstance(result, ConfigError) and result.pointer == "/leaves/0/address"


def test_bind_releases_the_lock_when_the_tables_fail() -> None:
    program = FakeProgram.from_debug_map()

    def broken(_arr: int) -> int:
        raise OSError("table read failed")

    program.elem_count = broken  # type: ignore[method-assign]
    result = bind_leaves(program, _leaves())
    assert isinstance(result, ConfigError) and result.code == "config_program_mismatch"
    assert program.depth == 0


def test_bind_refuses_empty_and_oversized_leaf_sets() -> None:
    program = FakeProgram.from_debug_map()
    empty = bind_leaves(program, ())
    assert isinstance(empty, ConfigError) and empty.code == "config_no_leaves"
    huge = bind_leaves(program, (replace(_leaves()[0], size=257),))
    assert isinstance(huge, ConfigError) and huge.code == "config_size_mismatch"
    assert program.windows == 0


def test_lock_window_must_hold_a_leaf() -> None:
    with pytest.raises(ValueError):
        LeafReader(FakeProgram.from_debug_map(), _leaves(), 0)


def test_sample_reads_every_leaf_in_bounded_lock_windows() -> None:
    program = FakeProgram.from_debug_map()
    leaves = _many_leaves(1000)
    reader = bind_leaves(program, leaves)
    assert isinstance(reader, LeafReader)
    program.windows = 0
    result = reader.sample()
    assert len(result.values) == 1000 and result.read_errors == 0
    assert program.windows == math.ceil(1000 / LOCK_WINDOW_LEAVES)
    assert program.max_reads_per_window == LOCK_WINDOW_LEAVES
    assert program.reads_outside_lock == 0 and program.depth == 0


def test_conversion_runs_only_after_the_lock_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    program = FakeProgram.from_debug_map()
    reader = _bound(program, window=4)
    depths: list[int] = []
    original = reader_module.convert_debug_read

    def spy(iec: str, raw: bytes) -> object:
        depths.append(program.depth)
        return original(iec, raw)

    monkeypatch.setattr(reader_module, "convert_debug_read", spy)
    reader.sample()
    assert depths and set(depths) == {0}


def test_sample_converts_real_values_exactly() -> None:
    program = FakeProgram.from_debug_map()
    program.values[(0, 7)] = struct.pack("<q", -(2**63))
    program.values[(0, 8)] = struct.pack("<Q", 2**64 - 1)
    program.values[(0, 15)] = struct.pack("<q", -1)
    program.values[(0, 20)] = bytes([2]) + struct.pack("<2H", 0xD800, 0x0041) + bytes(248)
    result = _bound(program).sample()
    by_type = {value.iec_type: value for value in result.values}
    assert by_type["LINT"].json_text == '"-9223372036854775808"'
    assert by_type["ULINT"].native == 2**64 - 1
    assert by_type["TIME"].json_text == '"-1"'
    assert by_type["WSTRING"].native == struct.pack("<2H", 0xD800, 0x0041)
    assert by_type["BOOL"].json_text == "false" and by_type["STRING"].json_text == '""'
    assert result.read_errors == 0


@pytest.mark.parametrize(
    ("inject", "detail"),
    [("raise_on", "thunk_error"), ("empty_on", "no_value"), ("short_on", "size_mismatch")],
)
def test_read_failure_is_a_diagnosed_null_and_other_leaves_still_read(
    inject: str, detail: str
) -> None:
    program = FakeProgram.from_debug_map()
    getattr(program, inject).add((0, 5))
    program.values[(0, 6)] = struct.pack("<I", 7)
    result = _bound(program).sample()
    assert result.read_errors == 1
    (issue,) = result.issues
    assert issue.index == 5
    assert (issue.issue.code, issue.issue.detail) == ("read_failed", detail)
    assert result.values[5].is_null and result.values[5].native is None
    assert result.values[6].native == 7
    assert program.depth == 0


def test_full_string_window_is_reported_not_published() -> None:
    program = FakeProgram.from_debug_map()
    program.values[(0, 19)] = bytes([126]) + b"x" * 126
    result = _bound(program).sample()
    assert result.values[19].is_null
    assert [i.issue.code for i in result.issues] == ["string_exceeds_read_window"]


def test_recovery_after_null_produces_a_changed_valid_value() -> None:
    program = FakeProgram.from_debug_map()
    reader = _bound(program)
    program.values[(0, 9)] = struct.pack("<f", float("nan"))
    failed = reader.sample().values[9]
    program.values[(0, 9)] = struct.pack("<f", 2.5)
    recovered = reader.sample().values[9]
    steady = reader.sample().values[9]
    assert failed.is_null and failed.issue is not None
    assert failed.issue.code == "conversion_failed"
    assert recovered.json_text == "2.5" and not recovered.is_null
    assert recovered.identity != failed.identity
    assert steady.identity == recovered.identity


def test_program_unloaded_after_bind_yields_nulls_not_stale_values() -> None:
    program = FakeProgram.from_debug_map()
    reader = _bound(program)
    program.values[(0, 5)] = struct.pack("<i", 42)
    assert reader.sample().values[5].native == 42
    program.loaded = False
    result = reader.sample()
    assert result.read_errors == len(result.values)
    assert all(v.is_null and v.native is None for v in result.values)


def test_issue_messages_never_carry_process_values() -> None:
    program = FakeProgram.from_debug_map()
    program.values[(0, 19)] = bytes([1, 0xFF]) + bytes(125)
    program.values[(0, 0)] = b"\x07"
    result = _bound(program).sample()
    for item in result.issues:
        assert "ff" not in item.issue.message.lower() and "7" not in item.issue.message
    assert {i.issue.detail for i in result.issues} == {"utf8_invalid", "bool_out_of_range"}
