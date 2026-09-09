"""`image.conf` has three implementations. This is the only one CI can check.

The file format is written in three places and read in a fourth:

  * the OpenPLC editor emits it (``generate-image-conf.ts``, DOPE-615);
  * ``webserver/image_config.py`` validates and installs it;
  * ``core/src/plc_app/image_tables.cpp`` parses it in the PLC application;
  * ``core/src/plc_app/image_tables.h`` declares the tables it names.

A key added on one side and forgotten on another does not fail anything. The
core simply never sees that table's size, falls back to the floor derived from
the program, and the image comes out smaller than the project asked for --
silently, on a device, with no diagnostic anywhere. The C++ side has a
``static_assert`` for the count; the ORDER, and the agreement between C and
Python, have nowhere else to be checked.

So it is checked here, by reading the C sources as text. That is unusual and
deliberate: pytest is the only suite this repository runs in CI (the Ceedling
project in ``project.yml`` is wired to nothing), so a Python test is the only
guard that will actually run. It parses rather than imports because there is no
binding between the two languages to import through.

The editor lives in another repository and cannot be reached from here. Its
half of the contract is pinned by its own tests, and by the fact that all three
lists are in the same order for the same reason: they follow the declaration
order of ``image_tables.h``.
"""

import re
from pathlib import Path

import pytest

from webserver import image_config

REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGE_TABLES_H = REPO_ROOT / "core" / "src" / "plc_app" / "image_tables.h"
IMAGE_TABLES_CPP = REPO_ROOT / "core" / "src" / "plc_app" / "image_tables.cpp"


def _enum_ids() -> list[str]:
    """`image_table_id_t` members, in declaration order, lowercased."""
    body = re.search(
        r"typedef enum\s*\{(.*?)\}\s*image_table_id_t", IMAGE_TABLES_H.read_text(), re.S
    )
    assert body, "image_table_id_t not found — has the header been restructured?"
    return [m.lower() for m in re.findall(r"IMAGE_TABLE_([A-Z_]+)", body.group(1)) if m != "COUNT"]


def _c_keys() -> list[str]:
    """The strings `kImageTableKeys` maps those ids to, in order."""
    body = re.search(
        r"kImageTableKeys\[IMAGE_TABLE_COUNT\] = \{(.*?)\};", IMAGE_TABLES_CPP.read_text(), re.S
    )
    assert body, "kImageTableKeys not found — has the parser been restructured?"
    return re.findall(r'"([a-z_]+)"', body.group(1))


def _struct_fields() -> list[str]:
    """The tables `image_tables_t` actually declares, in order.

    Matches the member NAME rather than any particular declarator, because the
    tables have already changed shape once: they were `IEC_BYTE *x[N]` and
    `IEC_BOOL *x[N][8]` inline arrays, and are now `IEC_BYTE **x` and
    `IEC_BOOL *(*x)[8]` heap pointers. This test exists to catch a table being
    added, removed or reordered, not to have an opinion on how it is spelled.
    """
    body = re.search(
        r"typedef struct\s*\{(.*?)\}\s*image_tables_t", IMAGE_TABLES_H.read_text(), re.S
    )
    assert body, "image_tables_t not found — has the header been restructured?"
    lines = [line for line in body.group(1).splitlines() if line.strip().startswith(("IEC_",))]
    return [re.search(r"\*(\w+)\)?(?:\[\d+\])?;", line).group(1) for line in lines]


@pytest.mark.parametrize(
    "name,reader",
    [("enum", _enum_ids), ("key array", _c_keys), ("struct", _struct_fields)],
)
def test_the_c_side_lists_agree_with_python_exactly(name, reader):
    # Order matters as much as membership: the key array is indexed BY the enum,
    # so a reordering of either one silently maps a table to another table's
    # name. Nothing would fail; the sizes would just land in the wrong places.
    assert reader() == list(image_config.IMAGE_TABLE_KEYS), (
        f"the C {name} and webserver/image_config.IMAGE_TABLE_KEYS have drifted"
    )


def test_there_are_fourteen_tables():
    # Pinned as a number rather than derived, so adding a table to one side and
    # not the others fails here rather than passing by agreeing with itself.
    assert len(image_config.IMAGE_TABLE_KEYS) == 14


def test_memory_has_no_byte_table():
    # Not an oversight to be tidied up: image_tables.h declares byte_input and
    # byte_output but no byte_memory, so `%MB` has no storage on this runtime.
    # The editor refuses such a declaration before the build, and the core's
    # floor derivation counts it as unstorable and says so once. Anyone
    # "fixing" this list would break that agreement.
    keys = image_config.IMAGE_TABLE_KEYS
    assert "byte_input" in keys and "byte_output" in keys
    assert "byte_memory" not in keys


def test_the_abi_limit_matches_the_index_width():
    # A located variable's table index is a uint16_t in strucpp_abi.hpp, which
    # is where this number comes from. It is a fact of the ABI, not a policy
    # ceiling, so it moves only if that field does.
    assert image_config.MAX_TABLE_ELEMENTS == 1 << 16
