# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Reader over real C function pointers, and the C/ctypes layout it depends on."""

from __future__ import annotations

import ctypes
import shutil
import struct
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from contract.runtime_config import Leaf
from mqtt_reader.reader import LeafReader, bind_leaves
from mqtt_reader.runtime_access import RuntimeDebugAccess
from shared.plugin_runtime_args import PluginRuntimeArgs

DRIVERS = Path(__file__).resolve().parents[4] / "core" / "src" / "drivers"
COMPILER = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
needs_compiler = pytest.mark.skipif(COMPILER is None, reason="no C compiler")

SHIM = r"""
#include <stdint.h>
#include <string.h>
static int depth, outside, windows, loaded = 1;
static uint8_t store[5][256];
static const uint16_t sizes[5] = {8, 127, 253, 4, 300};
void shim_lock(void) { depth++; windows++; }
void shim_unlock(void) { depth--; }
static void guard(void) { if (!depth) outside++; }
uint8_t shim_array_count(void) { guard(); return loaded ? 1 : 0; }
uint16_t shim_elem_count(uint8_t arr) { guard(); return (loaded && arr == 0) ? 5 : 0; }
uint16_t shim_size(uint8_t arr, uint16_t elem)
{
    guard();
    return (loaded && arr == 0 && elem < 5) ? sizes[elem] : 0;
}
uint16_t shim_read(uint8_t arr, uint16_t elem, uint8_t *dest)
{
    guard();
    if (!loaded || arr != 0 || elem >= 5) return 0;
    if (sizes[elem] <= 256) memcpy(dest, store[elem], sizes[elem]);
    return sizes[elem];
}
void shim_store(uint16_t elem, const uint8_t *bytes, uint16_t len) { memcpy(store[elem], bytes, len); }
void shim_set_loaded(int value) { loaded = value; }
int shim_outside(void) { return outside; }
int shim_windows(void) { return windows; }
int shim_depth(void) { return depth; }
"""


def _compile(tmp: Path, source: str, name: str, shared: bool) -> Path:
    assert COMPILER is not None
    src = tmp / f"{name}.c"
    src.write_text(source, encoding="utf-8")
    out = tmp / (f"{name}.so" if shared else name)
    flags = ["-shared", "-fPIC"] if shared else []
    subprocess.run(
        [COMPILER, *flags, f"-I{DRIVERS}", str(src), "-o", str(out)],
        check=True,
        capture_output=True,
    )
    return out


def _field_type(name: str) -> type:
    return dict(PluginRuntimeArgs._fields_)[name]  # pylint: disable=protected-access


@pytest.fixture
def shim(tmp_path: Path) -> Iterator[ctypes.CDLL]:
    lib = ctypes.CDLL(str(_compile(tmp_path, SHIM, "shim", shared=True)))
    lib.shim_store.argtypes = [ctypes.c_uint16, ctypes.c_char_p, ctypes.c_uint16]
    yield lib


def _args(lib: ctypes.CDLL) -> PluginRuntimeArgs:
    args = PluginRuntimeArgs()
    for field, symbol in (
        ("image_lock", "shim_lock"),
        ("image_unlock", "shim_unlock"),
        ("debug_array_count", "shim_array_count"),
        ("debug_elem_count", "shim_elem_count"),
        ("debug_size", "shim_size"),
        ("debug_read", "shim_read"),
    ):
        address = ctypes.cast(getattr(lib, symbol), ctypes.c_void_p).value
        setattr(args, field, _field_type(field)(address))
    return args


def _leaf(elem: int, iec_type: str, size: int) -> Leaf:
    return Leaf((), f"V{elem}", 0, elem, iec_type, size, f"t/{elem}")


LEAVES = (
    _leaf(0, "LINT", 8),
    _leaf(1, "STRING", 127),
    _leaf(2, "WSTRING", 253),
    _leaf(3, "REAL", 4),
)


@needs_compiler
def test_reader_samples_through_c_function_pointers(shim: ctypes.CDLL) -> None:
    lint = struct.pack("<q", -(2**63))
    text = bytes([4]) + "Olá".encode()
    units = struct.pack("<3H", 0xD83D, 0xDE00, 0xDC00)
    for elem, payload in ((0, lint), (1, text), (2, bytes([3]) + units), (3, b"\x00\x00\xc0\x7f")):
        shim.shim_store(elem, payload, len(payload))
    access = RuntimeDebugAccess(_args(shim))
    reader = bind_leaves(access, LEAVES, window=2)
    assert isinstance(reader, LeafReader)
    result = reader.sample()
    lint_value, string_value, wstring_value, real_value = result.values
    assert lint_value.json_text == '"-9223372036854775808"'
    assert string_value.native == "Olá" and string_value.json_text == '"Olá"'
    assert wstring_value.native == units
    assert real_value.is_null and real_value.issue is not None
    assert real_value.issue.detail == "float_not_finite"
    assert shim.shim_outside() == 0 and shim.shim_depth() == 0
    assert shim.shim_windows() == 1 + 2


@needs_compiler
def test_size_mismatch_and_oversized_reports_are_refused(shim: ctypes.CDLL) -> None:
    access = RuntimeDebugAccess(_args(shim))
    wrong = bind_leaves(access, (_leaf(3, "LREAL", 8),))
    assert not isinstance(wrong, LeafReader) and wrong.pointer == "/leaves/0/size"
    oversized = LeafReader(access, (_leaf(4, "WSTRING", 253),), 1).sample()
    assert oversized.issues[0].issue.detail == "thunk_error"
    shim.shim_set_loaded(0)
    unloaded = LeafReader(access, LEAVES, 4).sample()
    assert {item.issue.detail for item in unloaded.issues} == {"no_value"}
    assert shim.shim_depth() == 0


def test_missing_thunks_are_refused() -> None:
    with pytest.raises(ValueError, match="debug_read"):
        RuntimeDebugAccess(PluginRuntimeArgs())


@needs_compiler
def test_ctypes_mirror_matches_every_c_field_offset(tmp_path: Path) -> None:
    names = [name for name, _ in PluginRuntimeArgs._fields_]  # pylint: disable=protected-access
    lines = "".join(
        f'    printf("%s %zu\\n", "{name}", offsetof(plugin_runtime_args_t, {name}));\n'
        for name in names
    )
    probe = (
        '#include <stddef.h>\n#include <stdio.h>\n#include "plugin_types.h"\n'
        "int main(void)\n{\n"
        f"{lines}"
        '    printf("sizeof %zu\\n", sizeof(plugin_runtime_args_t));\n    return 0;\n}\n'
    )
    binary = _compile(tmp_path, probe, "layout", shared=False)
    output = subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout
    c_layout = dict(line.split() for line in output.splitlines())
    py_layout = {name: str(getattr(PluginRuntimeArgs, name).offset) for name in names}
    py_layout["sizeof"] = str(ctypes.sizeof(PluginRuntimeArgs))
    assert c_layout == py_layout


GIL_SHIM = r"""
#include <stdint.h>
#include <string.h>
extern int PyGILState_Check(void);
static int counts[4];
void gil_lock(void) { counts[0] += PyGILState_Check(); counts[3]++; }
void gil_unlock(void) { counts[2] += PyGILState_Check(); }
uint8_t gil_array_count(void) { return 1; }
uint16_t gil_elem_count(uint8_t arr) { return 16; }
uint16_t gil_size(uint8_t arr, uint16_t elem) { return 4; }
uint16_t gil_read(uint8_t arr, uint16_t elem, uint8_t *dest)
{
    counts[1] += PyGILState_Check();
    memset(dest, 0, 4);
    return 4;
}
int gil_count(int which) { return counts[which]; }
"""


@needs_compiler
def test_reads_and_unlock_keep_the_gil_while_the_image_lock_is_held(tmp_path: Path) -> None:
    src = tmp_path / "gil.c"
    src.write_text(GIL_SHIM, encoding="utf-8")
    out = tmp_path / "gil.so"
    assert COMPILER is not None
    lookup = ["-undefined", "dynamic_lookup"] if sys.platform == "darwin" else []
    subprocess.run(
        [COMPILER, "-shared", "-fPIC", *lookup, str(src), "-o", str(out)],
        check=True,
        capture_output=True,
    )
    lib = ctypes.CDLL(str(out))
    args = PluginRuntimeArgs()
    for field in ("lock", "unlock", "array_count", "elem_count", "size", "read"):
        target = f"image_{field}" if field in ("lock", "unlock") else f"debug_{field}"
        address = ctypes.cast(getattr(lib, f"gil_{field}"), ctypes.c_void_p).value
        setattr(args, target, _field_type(target)(address))
    leaves = tuple(_leaf(i, "DINT", 4) for i in range(8))
    reader = LeafReader(RuntimeDebugAccess(args), leaves, 4)
    assert len(reader.sample().values) == 8
    held_lock, held_read, held_unlock, locks = (lib.gil_count(i) for i in range(4))
    # image_lock may block on another plugin that waits for the GIL, so it must release it.
    assert (locks, held_lock) == (2, 0)
    assert held_read == 8 and held_unlock == 2
