"""The fourteen image table lengths, for a Python plugin (RTOP-284).

Importing ``set_image_sizes`` from here into a plugin module does two things at
once: it puts the name in that module's namespace, which is where the runtime
looks for it (``PyObject_GetAttrString(pModule, "set_image_sizes")``), and its
PRESENCE is how the plugin declares it understands per-table sizes.

    from shared.image_sizes import set_image_sizes, table_capacity

A plugin that does not import it is not broken. The runtime keeps the image
square for that run and logs which plugin forced it -- which is the honest
outcome, because a plugin bounding a byte index and a word index with one
``buffer_size`` is correct exactly while the tables are equal.

One implementation rather than one per plugin, for the same reason the native
side links one file: three copies of a fourteen-element cache is three places
for the indexing to drift.
"""

# The order the runtime sends them in, which is the declaration order of
# image_tables_t. Names rather than indices on this side, because the Python
# buffer names already are these names -- see shared/buffer_types.py -- so
# nothing has to know the numbering.
#
# NOTE the trap this avoids: journal_buffer.h and the s7comm plugin each have
# their own fourteen in a DIFFERENT order. Going by name cannot pick up the
# wrong table; going by index can, and silently.
IMAGE_TABLE_ORDER: list[str] = [
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
]

_sizes: dict[str, int] = {}


def set_image_sizes(sizes) -> int:
    """Receive the table lengths, before ``init`` runs, once per program load.

    Deliberately not remembered across loads: the runtime calls this on every
    load precisely so a plugin never acts on the previous program's shape.

    A list shorter than the fourteen leaves the rest unknown rather than
    guessed, and a longer one is truncated, so a plugin and a runtime built
    against different table lists still agree about the tables they share.

    Returns 0 on success, which is what the runtime requires; non-zero fails
    the plugin exactly as a failed ``init`` does.
    """
    _sizes.clear()
    try:
        values = list(sizes)
    except TypeError:
        return -1

    for name, count in zip(IMAGE_TABLE_ORDER, values):
        try:
            _sizes[name] = int(count)
        except (TypeError, ValueError):
            return -1
    return 0


def sizes_known() -> bool:
    """Whether the runtime has delivered the sizes for this load."""
    return bool(_sizes)


def table_capacity(name: str, default: int | None = None) -> int | None:
    """How long one table is, by the name the buffer accessors already use.

    ``default`` is what to answer when the sizes have not been delivered --
    normally the caller's own ``buffer_size``, which on a square run is the
    length every table has anyway.
    """
    return _sizes.get(name, default)
