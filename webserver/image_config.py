"""I/O image sizes for the program now installed.

WHO OWNS THESE SIZES
--------------------
The PROJECT does, and nobody chooses them. They are DERIVED by the editor from
what the project actually contains -- the addresses its producers claim and the
located variables it declares -- and emitted as ``image.conf`` into the program
upload, the same way ``retain.conf`` and the VPP plugin configuration travel.
The webserver's only job is to install what arrives (``apply_image_conf`` in
``plcapp_management``) and to refuse a stanza the core could not honour. There
is no endpoint to change them on a running device: a device sized out of band
would disagree with the program running on it, and the program is the thing a
user can see.

Before this existed the image was ``BUFFER_SIZE 1024`` per table, compiled in,
identical for every program that ever ran on the device. That is wrong in both
directions: a project needing more could not have it, and a project needing
less paid for the rest anyway, out of the memory its own program wanted
(RTOP-284, editor side DOPE-615).

Flat ``key=value`` rather than JSON, for the reason recorded for
``retain.conf``: the core parses it in C++ during startup, before any plugin
exists, and a dependency-free parser for fourteen integers is a better trade
there than pulling a JSON library into the PLC application.

WHAT AN ABSENT FILE MEANS, AND WHY IT IS NOT THE SAME AS RETAIN'S
-----------------------------------------------------------------
For ``retain.conf``, absence is an instruction: switch the built-in store off.
Here absence carries no instruction at all -- it means "nothing to tell you,
size the image from the program you just loaded", which the runtime can always
do by walking the located variables in the ``.so``.

That distinction matters for what ``apply_image_conf`` does, and the answer is
NOT "leave the device's copy alone". A stale copy is worse than no copy: a
project needing 4096 output words leaves ``int_output=4096`` behind, the next
program needs eight, and ``max(configured, derived)`` keeps 4096 words reserved
for a program that is no longer on the device -- silently, and for as long as
nobody notices. So an upload without the file REMOVES the device's copy, and
the runtime falls back to what the program itself requires.

THE UNIT IS EACH TABLE'S OWN, WHICH IS NOT ALWAYS THE ADDRESS'S
--------------------------------------------------------------
Every value is a count of that table's elements, matching
``core/src/plc_app/image_tables.h``. For eleven tables that is the number of
addresses (``int_memory[N]`` holds N ``%MW``s). For the three BOOL tables it is
not: they are declared ``IEC_BOOL *table[N][8]``, so N counts BYTES while
``%QX`` addresses bits. The editor does that conversion once, on its side, and
what arrives here is already in table elements. Nothing in this module divides
or multiplies by eight, deliberately -- a second conversion is how the two
sides end up disagreeing by a factor of eight with no diagnostic anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

from webserver.logger import get_logger

# The runtime's working directory (systemd `WorkingDirectory=$OPENPLC_DIR`), so
# image.conf lands beside retain.conf where the core looks for it.
logger, _ = get_logger("runtime", use_buffer=True)

RUNTIME_ROOT = Path(os.path.abspath(os.path.dirname(__file__))).parent
IMAGE_CONF_PATH = RUNTIME_ROOT / "image.conf"

# The fourteen tables of core/src/plc_app/image_tables.h, in the order it
# declares them. Fixed order so the installed file is byte-stable for the same
# program, which the editor also guarantees on its side.
#
# Note the gap this list makes visible: there is byte_input and byte_output but
# no byte_memory, so `%MB` has no storage on this runtime at all.
IMAGE_TABLE_UNITS = {
    "bool_input": "bits",
    "bool_output": "bits",
    "byte_input": "bytes",
    "byte_output": "bytes",
    "int_input": "words",
    "int_output": "words",
    "dint_input": "dwords",
    "dint_output": "dwords",
    "lint_input": "lwords",
    "lint_output": "lwords",
    "int_memory": "words",
    "dint_memory": "dwords",
    "lint_memory": "lwords",
    "bool_memory": "bits",
}
IMAGE_TABLE_KEYS = tuple(IMAGE_TABLE_UNITS)

# The wire format this runtime reads. A file declaring any other version, or
# none, is refused whole rather than read by today's rules: guessing is how a
# unit change becomes a silent factor of eight. There is no version 1 to be
# compatible with -- it was written but never merged, so no device has ever
# read this file in that form.
IMAGE_CONF_FORMAT_VERSION = 2

# A located variable's table index is carried as a uint16_t in the STruC++ ABI
# (`LocatedVar.byte_index`, core/src/lib/strucpp_abi.hpp), so no table can be
# addressed beyond this many elements.
#
# NOT a policy ceiling -- the demand has none, and the limit on image size is
# the memory actually available, which the allocation itself discovers. This is
# a fact of the ABI: roughly 65 times the old fixed value, not expected to bind,
# and refused here rather than silently truncated at bind time.
MAX_TABLE_ELEMENTS = 65536


def max_in_file_unit(key: str) -> int:
    """The ceiling for one table, expressed in the unit its file value uses.

    THE CEILING IS IN ELEMENTS AND THE FILE IS NOT, which is exactly the kind
    of mismatch the unit word exists to prevent -- so it must not be
    reintroduced here. A BOOL table's value is in bits and its storage is
    ``IEC_BOOL *[N][8]``, so 65536 elements is 524288 bits. Comparing the raw
    bit count against the element ceiling would refuse every legal image above
    8192 bytes, eight times early.
    """
    return MAX_TABLE_ELEMENTS * 8 if IMAGE_TABLE_UNITS[key] == "bits" else MAX_TABLE_ELEMENTS


class ImageConfigError(ValueError):
    """Raised for a size the runtime would not be able to honour."""


def read_image_conf_file(
    path: str | os.PathLike,
) -> tuple[int, dict[str, int | None], dict[str, str]]:
    """Parse an ``image.conf``, with every unset table read as zero.

    Takes a path rather than assuming the runtime root, because the file worth
    checking is the one that just arrived in the upload -- validating the
    installed copy would be validating it after the point where a refusal could
    still help.

    ZERO IS A REAL ANSWER, not a missing one. A program with no ``%QX`` has no
    reason to carry a ``bool_output`` image, and the memory it does not reserve
    goes back to the program. So an absent key and ``key=0`` mean the same
    thing, and a missing file yields all zeros -- which the caller reads as
    "size this from the program instead".

    Unknown keys are ignored rather than refused: a newer editor emitting a
    table this runtime does not have must not fail the upload, and the core
    would ignore it anyway.
    """
    sizes = {key: 0 for key in IMAGE_TABLE_KEYS}
    units = {key: IMAGE_TABLE_UNITS[key] for key in IMAGE_TABLE_KEYS}
    version = 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key == "format_version":
                    try:
                        version = int(value)
                    except ValueError:
                        version = -1
                    continue
                if key not in sizes:
                    continue
                # "<count> <unit>": the unit is checked in validation, where a
                # wrong one produces the same clear refusal as an out-of-range
                # count. A missing unit reads as an empty string and fails
                # there rather than being guessed at here.
                count, _, unit = value.partition(" ")
                units[key] = unit.strip()
                try:
                    sizes[key] = int(count)
                except ValueError:
                    # None, not -1, and the difference is the message. A -1 fell
                    # into the "cannot be negative" branch below, so `4.5 words`
                    # was refused with "int_output cannot be negative (got -1)"
                    # and the reader went looking for a minus sign that is not
                    # there. int(None) raises TypeError, which reaches the
                    # handler that says what actually happened -- and refusing
                    # it here, where someone is watching the build log, is this
                    # module's whole reason for existing.
                    sizes[key] = None
    except FileNotFoundError:
        pass
    except (UnicodeDecodeError, IsADirectoryError, PermissionError, OSError) as exc:
        # The file arrives from an upload, so its bytes are attacker-shaped in
        # the ordinary sense: a non-UTF-8 image.conf raises UnicodeDecodeError
        # and a directory entry of that name raises IsADirectoryError. Both used
        # to escape to app.py, which answers `Unexpected error: {e}` to the
        # client. Unreadable means "no sizes delivered", which the caller
        # already knows how to handle -- it refuses the stanza and falls back to
        # the floor derived from the program.
        logger.warning("Image: could not read %s (%s); treating as no sizes", path, exc)
        return -1, {key: None for key in IMAGE_TABLE_KEYS}, units
    return version, sizes, units


def validate_table_count(key: str, value: object, unit: object) -> int:
    """Check one table's count, in the unit its own addresses use.

    Refused at INSTALL, with a line in the build log the user is already
    watching, for the same reason the retain settings are: a size the core
    cannot honour would otherwise be discovered at bind time, per located
    variable, with nothing but a log line on a device nobody is looking at.

    The unit is checked too, and that is not pedantry. A ``bool_output`` value
    written in bytes rather than bits is a perfectly plausible number that
    allocates an image eight times too small, with no diagnostic on either
    side. Refusing it here is the only place a person sees it.
    """
    expected = IMAGE_TABLE_UNITS[key]
    if unit != expected:
        got = unit if unit else "no unit"
        raise ImageConfigError(f"{key} must be given in {expected} (got {got}).")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ImageConfigError(f"{key} must be a whole number of {expected}.") from exc
    if count < 0:
        raise ImageConfigError(f"{key} cannot be negative (got {count}).")
    ceiling = max_in_file_unit(key)
    if count > ceiling:
        raise ImageConfigError(
            f"{key} asks for {count} {expected}; the located-variable ABI "
            f"addresses at most {MAX_TABLE_ELEMENTS} elements, which is "
            f"{ceiling} {expected}."
        )
    return count


def validate_image_conf(
    version: int, sizes: dict[str, int | None], units: dict[str, str]
) -> dict[str, int]:
    """Validate the version and every table, returning the normalised counts.

    The version is checked FIRST and refuses the whole file, because every
    check below reads the values by this version's rules. A file from a format
    we do not know is not a file with fourteen suspicious numbers in it -- it
    is a file we cannot claim to have understood.

    Then all fourteen or none: the tables size interlocking storage that one
    allocation hands out together, and a half-applied image is worse than a
    refused one.
    """
    if version != IMAGE_CONF_FORMAT_VERSION:
        raise ImageConfigError(
            f"image.conf declares format_version {version}; this runtime reads "
            f"format_version {IMAGE_CONF_FORMAT_VERSION}. Re-upload the project "
            f"from a current editor."
        )
    return {
        key: validate_table_count(key, sizes.get(key, 0), units.get(key))
        for key in IMAGE_TABLE_KEYS
    }


def write_image_conf_file(path: str | os.PathLike, sizes: dict[str, int]) -> None:
    """Write an ``image.conf`` the core can read.

    Writes the VALIDATED stanza rather than byte-copying the upload, so what
    the core reads back is exactly what was checked here -- and so unknown keys
    and absent ones both land as the explicit zeros the core expects, instead of
    leaving it to infer them.

    Write-and-rename, like the retain store does it: a torn ``image.conf`` read
    at the next program load would size the image from half a file.
    """
    lines = [
        "# I/O image sizes for this program.",
        "# Installed from the program upload; the project is the source, and the",
        "# editor derives these from what the project actually contains.",
        "# Read by the PLC application at program load.",
        "# Edits here are overwritten on the next upload.",
        "#",
        "# One key per table in core/src/plc_app/image_tables.h. Every value",
        "# carries the unit of the ADDRESS it stores: the three BOOL tables are",
        "# in bits, because %QX addresses bits, and the core converts to the",
        "# [N][8] shape its storage actually has. Zero means the program",
        "# addresses nothing there and the runtime allocates nothing for it.",
        f"format_version={IMAGE_CONF_FORMAT_VERSION}",
    ]
    lines += [f"{key}={sizes[key]} {IMAGE_TABLE_UNITS[key]}" for key in IMAGE_TABLE_KEYS]

    target = Path(path)
    tmp = target.with_suffix(".conf.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def describe_image_conf(sizes: dict[str, int]) -> str:
    """One log line naming the tables that are actually sized.

    Only the non-zero ones: fourteen keys of which eleven are usually zero
    reads as noise, and the point of the line is to let someone watching the
    build see that the image followed their project.
    """
    used = [
        f"{key}={sizes[key]} {IMAGE_TABLE_UNITS[key]}" for key in IMAGE_TABLE_KEYS if sizes[key] > 0
    ]
    return ", ".join(used) if used else "every table zero"
