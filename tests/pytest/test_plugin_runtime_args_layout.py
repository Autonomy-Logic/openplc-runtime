# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""The ctypes mirror of plugin_runtime_args_t matches the C struct, program_md5 included."""

import ctypes
import shutil
import subprocess
from pathlib import Path

import pytest

from core.src.drivers.plugins.python.shared.plugin_runtime_args import PluginRuntimeArgs

ROOT = Path(__file__).resolve().parents[2]

PROBE = r"""
#include <stddef.h>
#include <stdio.h>
#include "plugin_types.h"
int main(void)
{
    printf("%zu %zu %zu %zu\n", sizeof(plugin_runtime_args_t),
           offsetof(plugin_runtime_args_t, get_plc_state),
           offsetof(plugin_runtime_args_t, program_md5),
           sizeof(((plugin_runtime_args_t *)0)->program_md5));
    return 0;
}
"""


def test_program_md5_follows_get_plc_state() -> None:
    names = [name for name, _ in PluginRuntimeArgs._fields_]
    assert names[-2:] == ["get_plc_state", "program_md5"]
    assert PluginRuntimeArgs.program_md5.size == 33


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_layout_matches_the_c_struct(tmp_path: Path) -> None:
    src = tmp_path / "probe.c"
    exe = tmp_path / "probe"
    src.write_text(PROBE)
    subprocess.run(
        ["cc", "-std=c11", "-I", str(ROOT / "core/src/drivers"), str(src), "-o", str(exe)],
        check=True,
    )
    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout.split()
    size, plc_state_off, md5_off, md5_size = (int(v) for v in out)
    assert ctypes.sizeof(PluginRuntimeArgs) == size
    assert PluginRuntimeArgs.get_plc_state.offset == plc_state_off
    assert PluginRuntimeArgs.program_md5.offset == md5_off
    assert md5_size == 33
