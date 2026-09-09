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

# The runtime's working directory (systemd `WorkingDirectory=$OPENPLC_DIR`), so
# image.conf lands beside retain.conf where the core looks for it.
RUNTIME_ROOT = Path(os.path.abspath(os.path.dirname(__file__))).parent
IMAGE_CONF_PATH = RUNTIME_ROOT / "image.conf"

# The fourteen tables of core/src/plc_app/image_tables.h, in the order it
# declares them. Fixed order so the installed file is byte-stable for the same
# program, which the editor also guarantees on its side.
#
# Note the gap this list makes visible: there is byte_input and byte_output but
# no byte_memory, so `%MB` has no storage on this runtime at all.
IMAGE_TABLE_KEYS = (
    "bool_input",
    "bool_output",
    "byte_input",
    "byte_output",
    "int_input",
    "int_output",
    "dint_input",
    "dint_output",
    "lint_input",
    "lint_output",
    "int_memory",
    "dint_memory",
    "lint_memory",
    "bool_memory",
)

# A located variable's table index is carried as a uint16_t in the STruC++ ABI
# (`LocatedVar.byte_index`, core/src/lib/strucpp_abi.hpp), so no table can be
# addressed beyond this many elements.
#
# NOT a policy ceiling -- the demand has none, and the limit on image size is
# the memory actually available, which the allocation itself discovers. This is
# a fact of the ABI: roughly 65 times the old fixed value, not expected to bind,
# and refused here rather than silently truncated at bind time.
MAX_TABLE_ELEMENTS = 65536


class ImageConfigError(ValueError):
    """Raised for a size the runtime would not be able to honour."""


def read_image_conf_file(path: str | os.PathLike) -> dict[str, int]:
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
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key not in sizes:
                    continue
                try:
                    sizes[key] = int(value)
                except ValueError:
                    # Left as a parse failure rather than an exception: the
                    # value is validated separately, and a garbled line should
                    # produce the same clear refusal as an out-of-range one
                    # rather than a traceback from the parser.
                    sizes[key] = -1
    except FileNotFoundError:
        pass
    return sizes


def validate_table_elements(key: str, value: object) -> int:
    """Check one table's element count.

    Refused at INSTALL, with a line in the build log the user is already
    watching, for the same reason the retain settings are: a size the core
    cannot honour would otherwise be discovered at bind time, per located
    variable, with nothing but a log line on a device nobody is looking at.
    """
    try:
        elements = int(value)
    except (TypeError, ValueError) as exc:
        raise ImageConfigError(f"{key} must be a whole number of elements.") from exc
    if elements < 0:
        raise ImageConfigError(f"{key} cannot be negative (got {elements}).")
    if elements > MAX_TABLE_ELEMENTS:
        raise ImageConfigError(
            f"{key} asks for {elements} elements; the located-variable ABI "
            f"addresses at most {MAX_TABLE_ELEMENTS}."
        )
    return elements


def validate_image_conf(sizes: dict[str, int]) -> dict[str, int]:
    """Validate every table, returning the normalised sizes.

    All fourteen or none: the tables size interlocking storage that one
    allocation hands out together, and a half-applied image is worse than a
    refused one.
    """
    return {key: validate_table_elements(key, sizes.get(key, 0)) for key in IMAGE_TABLE_KEYS}


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
        "# One key per table in core/src/plc_app/image_tables.h. Each value is a",
        "# count of ELEMENTS in that table, so the three BOOL tables are in bytes",
        "# (they are declared [N][8]) while every other table is in its own width.",
        "# Zero means the program addresses nothing there and the runtime should",
        "# allocate nothing for it.",
    ]
    lines += [f"{key}={sizes[key]}" for key in IMAGE_TABLE_KEYS]

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
    used = [f"{key}={sizes[key]}" for key in IMAGE_TABLE_KEYS if sizes[key] > 0]
    return ", ".join(used) if used else "every table zero"
