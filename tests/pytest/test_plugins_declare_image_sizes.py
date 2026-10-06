"""Every plugin the runtime ships has to declare per-table image sizes.

The runtime keeps the image SQUARE for any run in which even one loaded plugin
lacks `set_image_sizes` -- correctly, because a plugin bounding a byte index
and a word index with one `buffer_size` is only right while the tables are
equal. The consequence is that ONE plugin without it disables the feature for
the whole device.

That is exactly what shipped: `simple_modbus.py` had it and
`modbus_master_plugin.py` and `opcua/plugin.py` did not, so per-table sizing
never activated on a stock `plugins_default.conf` and every fix beneath it ran
in the degenerate case where it could not differ from the old behaviour.

Nothing failed. The image was simply square, which is also what a correct
square run looks like. This test is the only thing that tells the two apart.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_CONF = REPO_ROOT / "plugins_default.conf"
PYTHON_PLUGINS = REPO_ROOT / "core/src/drivers/plugins/python"


def shipped_entries() -> list[tuple[str, str]]:
    """`(name, path)` for every plugin the default config loads."""
    out = []
    for raw in PLUGINS_CONF.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(",")
        if len(fields) >= 2:
            out.append((fields[0].strip(), fields[1].strip()))
    return out


def test_the_default_config_is_readable():
    # A guard on the guard: a rename or a move that makes the parse return
    # nothing would otherwise turn this whole suite into a silent pass.
    assert len(shipped_entries()) >= 5


@pytest.mark.parametrize(
    "name,path",
    [(n, p) for n, p in shipped_entries() if p.endswith(".py")],
)
def test_every_shipped_python_plugin_declares_set_image_sizes(name, path):
    source = (REPO_ROOT / path.lstrip("./")).read_text()
    # The NAME has to be in this module's namespace, which is where
    # PyObject_GetAttrString looks. Importing it from shared.image_sizes is how
    # that is done; defining it directly would also work.
    assert re.search(
        r"^\s*(from .*import .*\bset_image_sizes\b|def set_image_sizes\b)", source, re.MULTILINE
    ), (
        f"plugin '{name}' ({path}) does not declare set_image_sizes, so every run "
        f"it is loaded in keeps the image square"
    )


def test_every_shipped_native_plugin_links_the_helper():
    # The native side declares it by linking plugin_image_sizes.c, which
    # defines and exports the symbol. Checked through the build files, because
    # the .so is not in the tree.
    native = [p for _, p in shipped_entries() if p.endswith(".so")]
    assert native, "no native plugin in the default config — has it been restructured?"

    for cmake in (REPO_ROOT / "core/src/drivers/plugins/native").glob("*/CMakeLists.txt"):
        assert "plugin_image_sizes.c" in cmake.read_text(), (
            f"{cmake.parent.name} does not link plugin_image_sizes.c, so it exports no "
            f"set_image_sizes and every run it is loaded in keeps the image square"
        )


def test_the_shared_module_is_what_they_import():
    # One implementation rather than one per plugin: three copies of a
    # fourteen-element cache is three places for the indexing to drift.
    assert (PYTHON_PLUGINS / "shared/image_sizes.py").exists()
