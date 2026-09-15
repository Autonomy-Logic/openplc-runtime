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

# parents[2] because this file sits at tests/pytest/, not tests/pytest/plugins/.
# It was moved out of plugins/ because .github/workflows/tests.yml passes
# --ignore=tests/pytest/plugins for pre-existing failures there, so a guard
# living in that directory would never fire in CI -- which is the one thing
# this test was written to be.
REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_TABLES_H = REPO_ROOT / "core" / "src" / "plc_app" / "image_tables.h"
# The enum moved out of image_tables.h so plugin_types.h could reach it without
# pulling the runtime internals in (RTOP-284, B2). Publishing a type costs no
# ABI, and a plugin receiving an array indexed by it has to be able to name the
# entries -- the alternative being a second copy of the enum, which is the
# drift this whole file exists to catch.
IMAGE_TABLE_ID_H = REPO_ROOT / "core" / "src" / "plc_app" / "image_table_id.h"
IMAGE_TABLES_CPP = REPO_ROOT / "core" / "src" / "plc_app" / "image_tables.cpp"
JOURNAL_H = REPO_ROOT / "core" / "src" / "plc_app" / "journal_buffer.h"
JOURNAL_C = REPO_ROOT / "core" / "src" / "plc_app" / "journal_buffer.c"
S7COMM_C = REPO_ROOT / "core/src/drivers/plugins/native/s7comm/s7comm_plugin.cpp"
ETHERCAT_C = REPO_ROOT / "core/src/drivers/plugins/native/ethercat/ethercat_io.c"
MODBUS_PY = REPO_ROOT / "core/src/drivers/plugins/python/modbus_slave/simple_modbus.py"


def _enum_ids() -> list[str]:
    """`image_table_id_t` members, in declaration order, lowercased."""
    body = re.search(
        r"typedef enum\s*\{(.*?)\}\s*image_table_id_t", IMAGE_TABLE_ID_H.read_text(), re.DOTALL
    )
    assert body, "image_table_id_t not found — has image_table_id.h been restructured?"
    return [m.lower() for m in re.findall(r"IMAGE_TABLE_([A-Z_]+)", body.group(1)) if m != "COUNT"]


def _c_keys() -> list[str]:
    """The strings `kImageTableKeys` maps those ids to, in order."""
    body = re.search(
        r"kImageTableKeys\[IMAGE_TABLE_COUNT\] = \{(.*?)\};", IMAGE_TABLES_CPP.read_text(), re.DOTALL
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
        r"typedef struct\s*\{(.*?)\}\s*image_tables_t", IMAGE_TABLES_H.read_text(), re.DOTALL
    )
    assert body, "image_tables_t not found — has the header been restructured?"
    lines = [line for line in body.group(1).splitlines() if line.strip().startswith(("IEC_",))]
    return [re.search(r"\*(\w+)\)?(?:\[\d+\])?;", line).group(1) for line in lines]


def _c_units() -> list[str]:
    """The units `kImageTableUnits` pairs with those keys, in order."""
    body = re.search(
        r"kImageTableUnits\[IMAGE_TABLE_COUNT\] = \{(.*?)\};",
        IMAGE_TABLES_CPP.read_text(),
        re.DOTALL,
    )
    assert body, "kImageTableUnits not found — has the parser been restructured?"
    return re.findall(r'"([a-z]+)"', body.group(1))


def _python_plugin_order() -> list[str]:
    """`IMAGE_TABLE_ORDER` in the shared Python module plugins import.

    The FOURTH copy of the order, and the one nothing pinned. It is what turns
    the runtime's positional `sizes` array into the names every Python buffer
    accessor uses, so a reorder of `image_table_id_t` would leave CI green
    while every Python plugin silently read one table's length as another's.
    Read as text rather than imported, like the C readers, because importing
    the plugin package needs its virtualenv.
    """
    src = (REPO_ROOT / "core/src/drivers/plugins/python/shared/image_sizes.py").read_text()
    body = re.search(r"IMAGE_TABLE_ORDER: list\[str\] = \[(.*?)\]", src, re.DOTALL)
    assert body, "IMAGE_TABLE_ORDER not found — has the module been restructured?"
    return re.findall(r'"([a-z_]+)"', body.group(1))


@pytest.mark.parametrize(
    "name,reader",
    [
        ("enum", _enum_ids),
        ("key array", _c_keys),
        ("struct", _struct_fields),
        ("python plugin order", _python_plugin_order),
    ],
)
def test_the_c_side_lists_agree_with_python_exactly(name, reader):
    # Order matters as much as membership: the key array is indexed BY the enum,
    # so a reordering of either one silently maps a table to another table's
    # name. Nothing would fail; the sizes would just land in the wrong places.
    assert reader() == list(
        image_config.IMAGE_TABLE_KEYS
    ), f"the C {name} and webserver/image_config.IMAGE_TABLE_KEYS have drifted"


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


def test_the_c_and_python_units_agree_table_for_table():
    # THE UNIT IS THE HALF THAT COSTS A FACTOR OF EIGHT. A table whose unit
    # disagrees across the implementations is not a parse error anywhere: the
    # editor writes bits, a reader takes them for elements, and the image comes
    # out eight times too small with every located address above the first
    # eighth silently refused at bind time.
    assert _c_units() == [image_config.IMAGE_TABLE_UNITS[k] for k in image_config.IMAGE_TABLE_KEYS]


def test_only_the_bool_tables_are_in_bits():
    # The three that are declared IEC_BOOL *[N][8]: their storage is in bytes
    # while their addresses are in bits, which is the whole reason the file
    # carries a unit at all. Every other table stores what it addresses.
    in_bits = [k for k, u in image_config.IMAGE_TABLE_UNITS.items() if u == "bits"]
    assert in_bits == ["bool_input", "bool_output", "bool_memory"]


def test_the_units_are_ones_both_sides_know():
    assert set(image_config.IMAGE_TABLE_UNITS.values()) == {
        "bits",
        "bytes",
        "words",
        "dwords",
        "lwords",
    }


def test_the_format_version_agrees_across_the_implementations():
    # The core refuses a file whose version it does not know, so a bump on one
    # side and not the other stops every upload rather than mis-reading one.
    header = (REPO_ROOT / "core" / "src" / "plc_app" / "image_tables.h").read_text()
    match = re.search(r"#define IMAGE_CONF_FORMAT_VERSION (\d+)", header)
    assert match, "IMAGE_CONF_FORMAT_VERSION not found in image_tables.h"
    assert int(match.group(1)) == image_config.IMAGE_CONF_FORMAT_VERSION


def test_the_abi_limit_matches_the_index_width():
    # A located variable's table index is a uint16_t in strucpp_abi.hpp, which
    # is where this number comes from. It is a fact of the ABI, not a policy
    # ceiling, so it moves only if that field does.
    assert image_config.MAX_TABLE_ELEMENTS == 1 << 16


class TestJournalMapping:
    """The journal's type enum and the image table enum are NOT the same order.

    Both have fourteen members and journal_buffer.h says this enum "matches the
    OpenPLC image table types" -- it matches the concepts, not the indices.
    The journal groups each width's memory table beside its input and output;
    image_tables.h puts every memory table at the end. JOURNAL_INT_MEMORY is 7
    and IMAGE_TABLE_INT_MEMORY is 10.

    A cast between them therefore corrupts silently: a write lands under
    another table's bounds, and an area is refused or admitted wrongly. The
    runtime maps them explicitly (kJournalToImageTable); this checks that the
    map is complete and that it is still needed.
    """

    @staticmethod
    def _journal_ids() -> list[str]:
        body = re.search(
            r"typedef enum\s*\{(.*?)\}\s*journal_buffer_type_t", JOURNAL_H.read_text(), re.DOTALL
        )
        assert body, "journal_buffer_type_t not found"
        return [
            m.lower() for m in re.findall(r"JOURNAL_([A-Z_]+)", body.group(1)) if m != "TYPE_COUNT"
        ]

    @staticmethod
    def _mapping() -> dict[str, str]:
        body = re.search(
            r"kJournalToImageTable\[JOURNAL_TYPE_COUNT\] = \{(.*?)\};",
            JOURNAL_C.read_text(),
            re.DOTALL,
        )
        assert body, "kJournalToImageTable not found — has the journal been restructured?"
        return {
            journal.lower(): image.lower()
            for journal, image in re.findall(
                r"\[JOURNAL_([A-Z_]+)\]\s*=\s*IMAGE_TABLE_([A-Z_]+)", body.group(1)
            )
        }

    def test_every_journal_type_maps_to_a_table(self):
        assert sorted(self._mapping()) == sorted(self._journal_ids())

    def test_each_one_maps_to_the_table_of_the_same_name(self):
        # The map is about ORDER, not renaming: JOURNAL_INT_MEMORY must reach
        # IMAGE_TABLE_INT_MEMORY, whatever index either one sits at.
        for journal, image in self._mapping().items():
            assert journal == image, f"JOURNAL_{journal.upper()} maps to the wrong table"

    def test_the_two_enums_really_do_disagree_on_order(self):
        # If they were ever made identical the map could go -- but silently
        # assuming they are identical is the bug. This fails if someone
        # reorders one to match, which is the moment to revisit the map on
        # purpose rather than discover it by corruption.
        assert self._journal_ids() != list(image_config.IMAGE_TABLE_KEYS)

    def test_the_journal_covers_every_table_the_image_has(self):
        assert sorted(self._journal_ids()) == sorted(image_config.IMAGE_TABLE_KEYS)


class TestTheOtherTableMappings:
    """Three more name-to-name maps over the same fourteen tables.

    `kJournalToImageTable` is pinned by TestJournalMapping. These three are the
    same shape and were pinned by nothing, which matters because Ceedling does
    not run in CI -- nothing compile-checks the two C ones either. A single
    wrong entry writes or reads under another table's bounds with no
    diagnostic, which is the failure the whole enum-order finding was about.
    """

    @staticmethod
    def _pairs(path, pattern) -> dict[str, str]:
        return {a.lower(): b.lower() for a, b in re.findall(pattern, path.read_text())}

    def test_s7comm_maps_every_buffer_type_to_the_table_of_the_same_name(self):
        pairs = self._pairs(
            S7COMM_C, r"case BUFFER_TYPE_([A-Z_]+):\s*return IMAGE_TABLE_([A-Z_]+);"
        )
        assert pairs, "s7_image_table not found — has the plugin been restructured?"
        assert sorted(pairs) == sorted(image_config.IMAGE_TABLE_KEYS)
        for buffer_type, table in pairs.items():
            assert (
                buffer_type == table
            ), f"BUFFER_TYPE_{buffer_type.upper()} maps to the wrong table"

    def test_ethercat_maps_each_direction_and_width_to_the_right_pair(self):
        src = ETHERCAT_C.read_text()
        body = re.search(r"ecat_table_for\(.*?\n\}", src, re.DOTALL)
        assert body, "ecat_table_for not found — has the plugin been restructured?"

        # EtherCAT only ever emits %I and %Q, so there is no memory case.
        expected = {
            "BIT": ("BOOL_INPUT", "BOOL_OUTPUT"),
            "BYTE": ("BYTE_INPUT", "BYTE_OUTPUT"),
            "WORD": ("INT_INPUT", "INT_OUTPUT"),
            "DWORD": ("DINT_INPUT", "DINT_OUTPUT"),
            "LWORD": ("LINT_INPUT", "LINT_OUTPUT"),
        }
        for size, (in_table, out_table) in expected.items():
            # The case label and its return sit on separate lines after
            # clang-format, so the match has to span them.
            arm = re.search(
                rf"case IEC_SIZE_{size}:\s*return in \? IMAGE_TABLE_(\w+) : IMAGE_TABLE_(\w+);",
                body.group(0),
            )
            assert arm, f"IEC_SIZE_{size} is not mapped"
            assert arm.group(1) == in_table, f"IEC_SIZE_{size} input side is wrong"
            assert arm.group(2) == out_table, f"IEC_SIZE_{size} output side is wrong"

    def test_the_modbus_segments_name_the_tables_they_live_in(self):
        body = re.search(r"SEGMENT_TABLES = \{(.*?)\}", MODBUS_PY.read_text(), re.DOTALL)
        assert body, "SEGMENT_TABLES not found"
        segments = dict(re.findall(r'"(\w+)":\s*"(\w+)"', body.group(1)))

        # All eight, not the three the behavioural tests happen to exercise.
        assert segments == {
            "qw_count": "int_output",
            "mw_count": "int_memory",
            "md_count": "dint_memory",
            "ml_count": "lint_memory",
            "qx_bits": "bool_output",
            "mx_bits": "bool_memory",
            "ix_bits": "bool_input",
            "iw_count": "int_input",
        }
        for table in segments.values():
            assert table in image_config.IMAGE_TABLE_KEYS
